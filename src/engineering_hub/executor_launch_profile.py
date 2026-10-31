"""Pure, canonical executor launch profile: parser and digest.

Shared by the privileged Linux launch supervisor and by Hub-side verifiers that
need to recognize and audit exact profile bytes. Nothing here touches the host:
no files, sockets, processes or privileges. It validates form and computes the
digest that launch receipts sign; it proves nothing about any running Engine.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
MAX_PROFILE_BYTES = 1024**2
# Read-only: the supervisor aliases this exact mapping, so nothing may change it.
LAUNCH_TARGETS = MappingProxyType({
    'engine_socket': '/run/engineering-hub/docker.sock',
    'issuer_socket': '/run/engineering-hub/issuer.sock',
    'config': '/operator/runner.json',
    'state': '/var/lib/engineering-hub/executor',
    'generations': '/var/lib/engineering-hub/generations',
    'records': '/var/lib/engineering-hub/records',
    'metadata': '/var/lib/engineering-hub/metadata',
    'baseline': '/var/lib/engineering-hub/baseline',
    'secrets': '/var/lib/engineering-hub/secrets',
})
_TARGETS = LAUNCH_TARGETS
_BROKER_SOCKET_TARGET = "/run/engineering-hub/broker.sock"


class LaunchProfileError(RuntimeError):
    """Sanitized refusal; never reflects profile values or paths."""

    def __init__(self):
        super().__init__('executor launch profile invalid')


def _require(value):
    if not value:
        raise LaunchProfileError()


def canonical(value):
    """The exact byte form profiles and receipts are digested in."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


_canonical = canonical


def _path(value):
    _require(type(value) is str and value.startswith('/') and value != '/'
             and posixpath.normpath(value) == value and not value.startswith('//')
             and not any(ord(char) < 32 or ord(char) == 127 for char in value))
    return PurePosixPath(value)


class _Closed(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra='forbid')


class ExecutorLaunchMount(_Closed):
    role: str
    source: str
    target: str
    kind: str
    read_only: bool
    uid: int = Field(ge=0, le=2**32 - 2)
    gid: int = Field(ge=0, le=2**32 - 2)
    mode: int = Field(ge=0, le=0o777)
    content_digest: str | None

    @model_validator(mode='after')
    def closed_role(self):
        _require(self.role in _TARGETS or self.role == 'broker_socket')
        if self.role == 'broker_socket':
            _require(self.target == _BROKER_SOCKET_TARGET)
        elif self.role == 'generations':
            # Engine bind sources are resolved in the daemon's host namespace;
            # the executor must see this exact absolute path, not an alias.
            _require(self.target == self.source)
        else:
            _require(self.target == _TARGETS[self.role])
        _path(self.source)
        kind = ('socket' if self.role.endswith('_socket') else 'file' if self.role == 'config' else 'directory')
        _require(self.kind == kind and self.read_only is (self.role not in {'state', 'generations'}))
        if self.kind == 'file':
            _require(self.uid > 0 and self.gid > 0 and self.mode == 0o600 and type(self.content_digest) is str
                     and _SHA.fullmatch(self.content_digest))
        else:
            _require(self.content_digest is None)
        if self.role == 'issuer_socket':
            _require(self.uid == 0 and self.mode == 0o660)
        if self.role == 'broker_socket':
            _require(self.uid == 0 and self.gid > 0 and self.mode == 0o660)
        return self


_HELD_HOST_IDS = frozenset({0, 10001})
_UNUSABLE_DAEMON_IDS = frozenset({0, 10001, 65534})


def _map_rows(value):
    """Closed (inside, outside, length) rows in ascending inside order, no overlaps."""
    _require(type(value) in {list, tuple} and 2 <= len(value) <= 8)
    rows = []
    for row in value:
        _require(type(row) in {list, tuple} and len(row) == 3 and all(type(item) is int for item in row))
        inside, outside, length = row
        _require(inside >= 0 and outside >= 0 and length > 0
                 and inside + length <= 2**32 and outside + length <= 2**32)
        rows.append((inside, outside, length))
    _require(rows == sorted(rows))
    for left, right in zip(rows, rows[1:]):
        _require(left[0] + left[2] <= right[0])
    by_outside = sorted(rows, key=lambda row: row[1])
    for left, right in zip(by_outside, by_outside[1:]):
        _require(left[1] + left[2] <= right[1])
    return tuple(rows)


def _proc_map_rows(raw):
    """Rows from kernel /proc/<pid>/{uid,gid}_map text, in the same closed form."""
    _require(type(raw) is str and len(raw) <= 65536)
    rows = []
    for line in raw.splitlines():
        fields = line.split()
        _require(len(fields) == 3 and all(re.fullmatch(r'[0-9]{1,10}', value) for value in fields))
        rows.append(tuple(int(value) for value in fields))
    return _map_rows(sorted(rows))


def _covers(rows, host_id):
    return any(outside <= host_id < outside + length for _, outside, length in rows)


def _covers_inside(rows, inside_id):
    return sum(1 for inside, _, length in rows if inside <= inside_id < inside + length) == 1


class DisjointDaemonPolicy(_Closed):
    """A v3 profile's pinned separate daemon: its account and exact live ID maps.

    Being inside the signed launch profile digest makes this verifier-bound.
    It describes what must be observed; it is not itself evidence of disjointness.
    """
    kind: str
    engine_id: str
    daemon_uid: int
    daemon_gid: int
    uid_map: tuple[tuple[int, int, int], ...]
    gid_map: tuple[tuple[int, int, int], ...]
    # SHA-256 of the exact held-range attestation bytes compared against. It
    # identifies which attestation was used; it doesn't make it complete or sourced.
    held_ranges_digest: str

    @field_validator('uid_map', 'gid_map', mode='before')
    @classmethod
    def maps(cls, value):
        return _map_rows(value)

    @model_validator(mode='after')
    def closed_policy(self):
        _require(self.kind == 'disjoint-v1'
                 and re.fullmatch(r'[A-Za-z0-9:_.-]{1,128}', self.engine_id)
                 and _SHA.fullmatch(self.held_ranges_digest)
                 and all(0 < value < 2**32 - 1 and value not in _UNUSABLE_DAEMON_IDS
                         for value in (self.daemon_uid, self.daemon_gid)))
        # Rootless convention: namespace root is exactly the daemon account.
        _require(self.uid_map[0] == (0, self.daemon_uid, 1) and self.gid_map[0] == (0, self.daemon_gid, 1))
        # The executor identity inside must map, and no host range may reach the held account or root.
        _require(_covers_inside(self.uid_map, 10001) and _covers_inside(self.gid_map, 10001))
        _require(not any(_covers(rows, host_id) for rows in (self.uid_map, self.gid_map) for host_id in _HELD_HOST_IDS))
        return self


class ExecutorLaunchProfile(_Closed):
    """Exact independently enrolled executor-only profile, NOT an app profile."""
    user: str
    supplementary_groups: tuple[int, ...]
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    environment: tuple[str, ...]
    fixture_root: str
    memory_bytes: int = Field(ge=64 * 1024**2, le=512 * 1024**2)
    cpu_millicores: int = Field(ge=100, le=1000)
    pids_limit: int = Field(ge=16, le=128)
    tmpfs_bytes: int = Field(ge=1024**2, le=64 * 1024**2)
    mounts: tuple[ExecutorLaunchMount, ...]
    runner_file: str
    runner_digest: str
    # Present only in v3 profiles. Absent keeps a legacy profile's exact digest.
    daemon_policy: DisjointDaemonPolicy | None = None
    # Signed opt-in boundary only; the supervisor refuses it until implemented.
    broker_mode: Literal['broker-v1'] | None = None

    @field_validator('supplementary_groups', 'entrypoint', 'command', 'environment', 'mounts', mode='before')
    @classmethod
    def arrays(cls, value):
        _require(type(value) in {list, tuple})
        return tuple(value)

    @model_validator(mode='before')
    @classmethod
    def no_explicit_null_policy(cls, value):
        # Legacy input never had this key; an explicit null was refused then and still is.
        if isinstance(value, dict):
            for name in ('daemon_policy', 'broker_mode'):
                if name in value:
                    _require(value[name] is not None)
        return value

    @model_validator(mode='after')
    def closed_profile(self):
        _require(self.user == '10001:10001' and len(self.supplementary_groups) <= 2
                 and len(set(self.supplementary_groups)) == len(self.supplementary_groups)
                 and all(type(value) is int and 0 <= value < 2**32 - 1 for value in self.supplementary_groups))
        # The separately implemented, independently built runner must actually
        # exist in the pinned image; merely naming this module is not evidence.
        _require(self.entrypoint == ('/opt/venv/bin/python', '-I', '-m',
                                     'hub_container_executor.installed_runner'))
        _require(self.command == ('--config', _TARGETS['config']))
        _require(len(self.environment) <= 16 and len({item.split('=', 1)[0] for item in self.environment}) == len(self.environment)
                 and all(re.fullmatch(r'(PATH|LANG|LC_ALL|TZ|PYTHONDONTWRITEBYTECODE|PYTHONUNBUFFERED)=[^\x00\r\n]{1,512}', item)
                         for item in self.environment))
        roles = (set(_TARGETS) if self.broker_mode is None else
                 (set(_TARGETS) - {'engine_socket', 'generations'}) | {'broker_socket'})
        _require(self.tmpfs_bytes <= self.memory_bytes and len(self.mounts) == len(roles)
                 and {item.role for item in self.mounts} == roles)
        if self.broker_mode is not None:
            _require(self.daemon_policy is not None)
        fixture = _path(self.fixture_root)
        _require(re.fullmatch(r'hub-pilot-[a-z0-9][a-z0-9-]{0,63}', fixture.name))
        sources = [PurePosixPath(item.source) for item in self.mounts]
        _require(all(fixture in PurePosixPath(item.source).parents for item in self.mounts
                     if item.role not in {'engine_socket', 'issuer_socket', 'broker_socket'}))
        _require(all(not (left == right or left in right.parents or right in left.parents)
                     for index, left in enumerate(sources) for right in sources[index + 1:]))
        _path(self.runner_file)
        _require(self.runner_file.endswith('/hub_container_executor/installed_runner.py')
                 and not any(self.runner_file == item.target or PurePosixPath(item.target) in PurePosixPath(self.runner_file).parents
                         for item in self.mounts) and type(self.runner_digest) is str and _SHA.fullmatch(self.runner_digest))
        return self

    def enrollment_value(self):
        """The exact JSON object this profile is enrolled and digested as."""
        value = self.model_dump(mode='json')
        if value['daemon_policy'] is None:
            # Legacy profiles never carried this key; their bytes must not change.
            del value['daemon_policy']
        if value['broker_mode'] is None:
            del value['broker_mode']
        return value

    def digest(self):
        return 'sha256:' + hashlib.sha256(_canonical(self.enrollment_value())).hexdigest()


def verify_profile_bytes(raw, expected_digest):
    """Parse exact canonical profile bytes and require their digest to equal an independently pinned one.

    The expected digest must come from evidence the caller already trusts; this
    only shows these bytes are that profile, not that the profile was approved.
    It is not admission: a caller still has to require the version it expects
    (for example that a v3 policy is present) and bind the policy's Engine and
    daemon identity to its own independent pins.

    Every failure is the one sanitized LaunchProfileError; validation messages,
    which can quote input, never escape.
    """
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_PROFILE_BYTES
             and type(expected_digest) is str and _SHA.fullmatch(expected_digest))
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result)
            result[key] = value
        return result
    def reject(_):
        raise LaunchProfileError()
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)
        # A JSON escape can decode to a lone surrogate; re-encoding it fails here, closed.
        _require(canonical(data) == raw)
        profile = ExecutorLaunchProfile.model_validate(data)
        _require(canonical(profile.enrollment_value()) == raw and profile.digest() == expected_digest)
    except LaunchProfileError:
        raise
    except (ValueError, TypeError, RecursionError):
        # Includes pydantic's ValidationError and UnicodeEncodeError, both ValueErrors.
        raise LaunchProfileError() from None
    return profile
