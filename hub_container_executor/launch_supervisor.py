"""Explicit root-owned, single-launch Docker executor/receipt supervisor.

Nothing runs at import. Independent ROOT-PROTECTED enrollment and an existing
private signing key are mandatory; no key/enrollment is generated here. This is
not a service installer, issuer discovery, retry/recovery worker or permission
provisioner. An actual independently packaged direct-init runner is still needed:
the ordinary Hub image does not contain that runner and is not accepted merely
because its health endpoint works. Same-UID/root administrators remain trusted.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engineering_hub.container_pilot_manifest import Platform
from .build_record import VerifiedImage
from .daemon_backend import _archive_metadata, _archive_image
from .runtime_identity import (ExecutorReleaseEnrollment, PROTOCOL, AUDIENCE,
                               SIGNATURE_DOMAIN, MAX_RECEIPT_BYTES)
from .launch_transport import REQUEST_PROTOCOL, MAX_REQUEST_BYTES


_HEX = re.compile(r'[0-9a-f]{64}\Z')
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_TARGETS = {
    'engine_socket': '/run/engineering-hub/docker.sock',
    'issuer_socket': '/run/engineering-hub/issuer.sock',
    'config': '/operator/runner.json',
    'state': '/var/lib/engineering-hub/executor',
    'generations': '/var/lib/engineering-hub/generations',
    'records': '/var/lib/engineering-hub/records',
    'metadata': '/var/lib/engineering-hub/metadata',
    'baseline': '/var/lib/engineering-hub/baseline',
    'secrets': '/var/lib/engineering-hub/secrets',
}


class LaunchSupervisorError(RuntimeError):
    """Sanitized denial/uncertainty; never reflects key, path, nonce or Engine text."""

    def __init__(self):
        super().__init__('executor launch/issuance unavailable; preserve original evidence')


def _require(value):
    if not value:
        raise LaunchSupervisorError()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _strict(raw, maximum):
    _require(type(raw) is bytes and 0 < len(raw) <= maximum)
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result)
            result[key] = value
        return result
    def reject(_):
        raise LaunchSupervisorError()
    result = json.loads(raw, object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)
    _require(_canonical(result) == raw)
    return result


def _path(value):
    _require(type(value) is str and value.startswith('/') and value != '/'
             and os.path.normpath(value) == value and not value.startswith('//')
             and not any(ord(char) < 32 or ord(char) == 127 for char in value))
    return Path(value)


def _identity(info):
    return info.st_dev, info.st_ino


def _root_parent(path, *, private):
    """No-follow root-owned ancestor walk; private leaves must be mode0700."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for name in path.parent.parts[1:]:
            current = os.fstat(fd)
            _require(current.st_uid == 0 and not current.st_mode & 0o022
                     or current.st_uid == 0 and current.st_mode & stat.S_ISVTX)
            next_fd = os.open(name, flags, dir_fd=fd)
            try:
                named = os.stat(name, dir_fd=fd, follow_symlinks=False)
                _require(_identity(named) == _identity(os.fstat(next_fd)))
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        _require(info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022)
        if private:
            _require(stat.S_IMODE(info.st_mode) == 0o700)
        _require(path.parent.resolve(strict=True) == path.parent)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _root_file(path, maximum):
    parent = _root_parent(path, private=True)
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=parent)
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                 and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= maximum)
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        stable = lambda value: (_identity(value), value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        _require(stable(before) == stable(after) and _identity(before) == _identity(os.stat(
            path.name, dir_fd=parent, follow_symlinks=False)) and len(raw) == before.st_size)
        return raw, (_identity(before), hashlib.sha256(raw).hexdigest())
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _remaining(deadline):
    value = deadline - time.monotonic()
    _require(math.isfinite(value) and value > 0)
    return value


def _boot_id():
    value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    _require(str(UUID(value)) == value)
    return value


def _proc_start(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    # comm may contain spaces and ')'; fields after its final ')' start at #3.
    suffix = raw[raw.rfind(')') + 2:].split()
    _require(len(suffix) >= 20 and suffix[0] not in {'Z', 'X'})
    ticks = int(suffix[19])
    _require(ticks > 0)
    return ticks


class _Process:
    def __init__(self, pid, *, expected_start=None):
        _require(type(pid) is int and pid > 0 and hasattr(os, 'pidfd_open'))
        self.pid, self.fd = pid, None
        self.boot = _boot_id()
        before = _proc_start(pid)
        self.fd = os.pidfd_open(pid, 0)
        try:
            self.start = _proc_start(pid)
            _require(self.start == before and (expected_start is None or expected_start == self.start))
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        _require(self.fd is not None and not select.select([self.fd], [], [], 0)[0]
                 and _boot_id() == self.boot and _proc_start(self.pid) == self.start)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


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
        _require(self.role in _TARGETS)
        if self.role == 'generations':
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

    @field_validator('supplementary_groups', 'entrypoint', 'command', 'environment', 'mounts', mode='before')
    @classmethod
    def arrays(cls, value):
        _require(type(value) in {list, tuple})
        return tuple(value)

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
        _require(self.tmpfs_bytes <= self.memory_bytes and len(self.mounts) == len(_TARGETS)
                 and {item.role for item in self.mounts} == set(_TARGETS))
        fixture = _path(self.fixture_root)
        _require(re.fullmatch(r'hub-pilot-[a-z0-9][a-z0-9-]{0,63}', fixture.name))
        sources = [Path(item.source) for item in self.mounts]
        _require(all(fixture in Path(item.source).parents for item in self.mounts
                     if item.role not in {'engine_socket', 'issuer_socket'}))
        _require(all(not (left == right or left in right.parents or right in left.parents)
                     for index, left in enumerate(sources) for right in sources[index + 1:]))
        _path(self.runner_file)
        _require(self.runner_file.endswith('/hub_container_executor/installed_runner.py')
                 and not any(self.runner_file == item.target or Path(item.target) in Path(self.runner_file).parents
                         for item in self.mounts) and type(self.runner_digest) is str and _SHA.fullmatch(self.runner_digest))
        return self

    def digest(self):
        return 'sha256:' + hashlib.sha256(_canonical(self.model_dump(mode='json'))).hexdigest()


@dataclass(frozen=True)
class _DaemonPin:
    socket_path: str
    pid: int
    start_ticks: int
    uid: int
    gid: int
    daemon_id: str
    api_version: str

    def __post_init__(self):
        _path(self.socket_path)
        _require(all(type(value) is int and value > 0 for value in (self.pid, self.start_ticks))
                 and all(type(value) is int and 0 <= value < 2**32 - 1 for value in (self.uid, self.gid))
                 and type(self.daemon_id) is str and re.fullmatch(r'[A-Za-z0-9:_.-]{1,128}', self.daemon_id)
                 and type(self.api_version) is str and re.fullmatch(r'1\.[0-9]{2,3}', self.api_version)
                 and int(self.api_version.split('.')[1]) >= 41)
        _rootless_daemon_uid(self.uid)


def _rootless_daemon_uid(uid):
    # This deliberately narrow pilot enrolls only the allocated ehpilot daemon.
    _require(uid == 10001)


def _mapped_id(raw, identifier):
    _require(type(raw) is str and len(raw) <= 65536)
    rows = []
    for line in raw.splitlines():
        fields = line.split()
        _require(len(fields) == 3 and all(re.fullmatch(r'[0-9]+', value) for value in fields))
        inside, outside, length = map(int, fields)
        _require(length > 0 and inside + length <= 2**32 and outside + length <= 2**32)
        _require(all(inside + length <= old[0] or old[0] + old[2] <= inside for old in rows))
        rows.append((inside, outside, length))
    matches = [outside + identifier - inside for inside, outside, length in rows
               if inside <= identifier < inside + length]
    _require(len(matches) == 1)
    return matches[0]


def _kernel_profile(profile, status, uid_map, gid_map, mountinfo, consumer_uid, consumer_gid):
    """Validate caller-observed /proc data, not Engine's desired configuration."""
    _require(_mapped_id(uid_map, 10001) == consumer_uid and _mapped_id(gid_map, 10001) == consumer_gid)
    _require([int(value) for value in status['Uid'].split()] == [consumer_uid] * 4
             and [int(value) for value in status['Gid'].split()] == [consumer_gid] * 4
             and status.get('NoNewPrivs') == '1'
             and all(int(status[name], 16) == 0 for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')))
    actual_groups = [int(value) for value in status['Groups'].split()]
    _require(len(actual_groups) == len(set(actual_groups)))
    expected_groups = {_mapped_id(gid_map, int(value)) for value in profile.supplementary_groups}
    # Runtimes differ on including the primary GID in supplementary Groups.
    # No undeclared non-primary group is accepted; all declared groups are required.
    _require(expected_groups <= set(actual_groups) <= expected_groups | {consumer_gid})
    _require(type(mountinfo) is bytes and len(mountinfo) <= 1024**2)
    mounts = {}
    for line in mountinfo.decode('utf-8').splitlines():
        parts = line.split(' - ')
        _require(len(parts) == 2)
        left, right = parts[0].split(), parts[1].split()
        _require(len(left) >= 6 and len(right) >= 3)
        target = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), left[4])
        # Stacked duplicate mountpoints make the effective mount ambiguous.
        _require(target not in mounts)
        mounts[target] = (set(left[5].split(',')), right[0])
    _require('/' in mounts and 'ro' in mounts['/'][0] and 'rw' not in mounts['/'][0])
    for mount in profile.mounts:
        _require(mount.target in mounts)
        flags = mounts[mount.target][0]
        wanted, denied = ('ro', 'rw') if mount.read_only else ('rw', 'ro')
        _require(wanted in flags and denied not in flags)
    _require('/tmp' in mounts and mounts['/tmp'][1] == 'tmpfs'
             and {'rw', 'nosuid', 'nodev', 'noexec'} <= mounts['/tmp'][0]
             and 'ro' not in mounts['/tmp'][0])


class _NoReconnect(http.client.HTTPConnection):
    def connect(self):
        raise LaunchSupervisorError()

    def close(self):
        # One-shot context owns the socket. HTTPConnection otherwise closes it
        # immediately for Connection:close responses, preventing deadline updates
        # while reading the response. Never reuse this connection object.
        pass


class _ArchiveReader:
    def __init__(self, response, peer, deadline):
        self.response, self.peer, self.deadline = response, peer, deadline
        self.total = 0

    def read(self, size):
        _require(type(size) is int and 0 <= size <= 1024**2)
        self.peer.settimeout(_remaining(self.deadline))
        value = self.response.read(min(size, 65536))
        self.total += len(value)
        _require(self.total <= 2 * 1024**3)
        _remaining(self.deadline)
        return value


class _UnixEngine:
    """Each request directly authenticates the pinned Linux daemon; no retries."""
    def __init__(self, pin):
        self.pin = pin
        self.process = _Process(pin.pid, expected_start=pin.start_ticks)
        try:
            self.namespace = _identity(os.stat('/proc/self/ns/pid'))
            _require(_identity(os.stat(f'/proc/{pin.pid}/ns/pid')) == self.namespace)
            self.socket_identity = _identity(os.lstat(pin.socket_path))
            _require(stat.S_ISSOCK(os.lstat(pin.socket_path).st_mode))
        except BaseException:
            self.close()
            raise

    def close(self):
        self.process.close()

    @contextmanager
    def request(self, method, route, deadline, *, body=None, statuses=(200,)):
        connection = _NoReconnect('docker', timeout=_remaining(deadline))
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        response = None
        try:
            self.process.check()
            _require(_identity(os.stat('/proc/self/ns/pid')) == self.namespace
                     and _identity(os.stat(f'/proc/{self.pin.pid}/ns/pid')) == self.namespace
                     and _identity(os.lstat(self.pin.socket_path)) == self.socket_identity)
            peer.settimeout(_remaining(deadline))
            peer.connect(self.pin.socket_path)
            observed = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            _require(observed == (self.pin.pid, self.pin.uid, self.pin.gid)
                     and _identity(os.lstat(self.pin.socket_path)) == self.socket_identity)
            self.process.check()
            connection.sock = peer
            connection.request(method, '/v' + self.pin.api_version + route,
                body=None if body is None else _canonical(body),
                headers={'Host': 'docker', 'Accept-Encoding': 'identity', 'Content-Type': 'application/json',
                         'Connection': 'close'})
            response = connection.getresponse()
            headers = response.getheaders()
            _require(response.status in statuses
                     and len([value for key, value in headers if key.lower() == 'content-length']) <= 1
                     and response.getheader('Content-Encoding', 'identity').lower() == 'identity')
            if response.getheader('Transfer-Encoding') is not None:
                _require(response.getheader('Content-Length') is None
                         and response.getheader('Transfer-Encoding').lower() == 'chunked')
            self.process.check()
            yield response, peer
            _remaining(deadline)
            self.process.check()
        finally:
            if response is not None:
                response.close()
            peer.close()

    def json(self, method, route, deadline, *, body=None, statuses=(200,)):
        with self.request(method, route, deadline, body=body, statuses=statuses) as (response, peer):
            data = []
            total = 0
            while True:
                peer.settimeout(_remaining(deadline))
                part = response.read(min(65536, 1024**2 + 1 - total))
                if not part:
                    break
                total += len(part)
                _require(total <= 1024**2)
                data.append(part)
            raw = b''.join(data)
            _require(len(raw) <= 1024**2)
            if not raw:
                return response.status, None
            # Engine JSON need not use our canonical spelling, but duplicate
            # keys, nonfinite values and response reflection remain forbidden.
            data = json.loads(raw, object_pairs_hook=lambda pairs: _unique(pairs),
                              parse_constant=lambda _: _require(False))
            return response.status, data

    def image(self, image, deadline):
        _, info = self.json('GET', '/info', deadline)
        _require(type(info) is dict and info.get('ID') == self.pin.daemon_id
                 and info.get('OSType') == 'linux'
                 and type(info.get('SecurityOptions')) is list
                 and 'name=rootless' in info['SecurityOptions']
                 and info.get('Architecture') in ({'amd64', 'x86_64'} if image.platform.architecture == 'amd64'
                                                 else {'arm64', 'aarch64'}))
        _, inspected = self.json('GET', '/images/' + image.runnable_reference + '/json', deadline)
        with self.request('GET', '/images/' + image.runnable_reference + '/get', deadline) as (response, peer):
            config, defaults = _archive_image(_archive_metadata(_ArchiveReader(response, peer, deadline)), image)
        _require(type(inspected) is dict and inspected.get('Id') in {
            image.runnable_reference, image.platform_manifest_digest, image.image_config_id})
        _require(config == image.image_config and inspected.get('Config') == defaults)
        return defaults


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


class _Journal:
    """Root-private local durable single-launch/one-issuance reservation.

    Restart can inspect retained rows but never adopts an earlier launch. The
    lock is held until close; no row or lock file is deleted on uncertainty.
    """
    def __init__(self, path, *, create):
        from engineering_hub.pilot_ownership import _local_filesystem
        _require(type(create) is bool)
        self.path, self.pid = _path(path), os.getpid()
        self.parent = self.lock = None
        try:
            self.parent = _root_parent(self.path, private=True)
            _local_filesystem(self.parent)
            lock_name = self.path.name + '.lock'
            self.lock = os.open(lock_name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC |
                                (os.O_CREAT | os.O_EXCL if create else 0), 0o600, dir_fd=self.parent)
            lock = os.fstat(self.lock)
            _require(stat.S_ISREG(lock.st_mode) and lock.st_uid == 0
                     and stat.S_IMODE(lock.st_mode) == 0o600 and lock.st_nlink == 1)
            self.lock_identity = _identity(lock)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if create:
                fd = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=self.parent)
                os.fsync(fd)
                os.close(fd)
                os.fsync(self.parent)
            info = os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
            self.identity = _identity(info)
            with self.transaction() as db:
                if create:
                    db.execute('CREATE TABLE launch (id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL)')
                    db.execute('CREATE TABLE issuance (launch_id TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, '
                               'state TEXT NOT NULL, receipt_digest TEXT)')
                    db.execute('PRAGMA user_version=1')
                _require(db.execute('PRAGMA user_version').fetchone()[0] == 1)
                _require({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {'launch', 'issuance'})
        except BaseException:
            self.close()
            raise

    def _check(self):
        _require(self.pid == os.getpid() and self.parent is not None and self.lock is not None
                 and _identity(os.stat(self.path.parent, follow_symlinks=False)) == _identity(os.fstat(self.parent))
                 and self.path.parent.resolve(strict=True) == self.path.parent
                 and _identity(os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)) == self.identity
                 and _identity(os.stat(self.path.name + '.lock', dir_fd=self.parent, follow_symlinks=False)) == self.lock_identity)

    @contextmanager
    def transaction(self):
        self._check()
        db = sqlite3.connect(str(self.path), timeout=1, isolation_level=None)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            self._check()
            db.commit()
            os.fsync(self.parent)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self, identity, data):
        with self.transaction() as db:
            _require(db.execute('SELECT count(*) FROM launch').fetchone()[0] == 0)
            db.execute('INSERT INTO launch VALUES (?,?,?)', (identity, 'create_reserved', _canonical(data).decode()))

    def state(self, identity, state, data):
        with self.transaction() as db:
            _require(db.execute('UPDATE launch SET state=?,data=? WHERE id=?',
                (state, _canonical(data).decode(), identity)).rowcount == 1)

    def reserve_issuance(self, identity, nonce):
        with self.transaction() as db:
            _require(db.execute('SELECT state FROM launch WHERE id=?', (identity,)).fetchone() == ('running',))
            db.execute('INSERT INTO issuance VALUES (?,?,?,NULL)', (identity, nonce, 'reserved'))

    def issuance_state(self, identity, state, digest):
        with self.transaction() as db:
            _require(db.execute('UPDATE issuance SET state=?,receipt_digest=? WHERE launch_id=?',
                                (state, digest, identity)).rowcount == 1)

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        if self.parent is not None:
            os.close(self.parent)
            self.parent = None


class LaunchSupervisor:
    """Explicit ROOT operator API: one create/start, exact init peer, one receipt.

    Construct with existing independent enrollment/key and a private durable
    journal. launch() creates the exact pinned image once; serve_one_receipt()
    synchronously handles one bounded connection. No service is installed or
    background thread created. close() closes descriptors ONLY: it does not claim
    container quiescence. graceful_stop() is a separate explicit SIGTERM action.
    No automatic name adoption, POST retry, Docker stop/kill timeout, remove,
    cleanup of generations, key creation or restart recovery is implemented.
    """
    def __init__(self, *, enrollment_file, signing_key_file, journal_file, create_journal):
        self._pid = os.getpid()
        self._mutex = threading.Lock()
        self._journal = self._engine = self._consumer = self._listener = None
        self._socket_parent = None
        self._socket_identity = None
        self._launch_id = self._container_id = None
        self._closed = False
        self._issuance_open = False
        self._held = False
        self._source_pins = {}
        try:
            _require(sys.platform == 'linux' and os.geteuid() == os.getuid() == 0
                     and hasattr(socket, 'SO_PEERCRED') and hasattr(os, 'pidfd_open'))
            self._enrollment_path, self._key_path = _path(enrollment_file), _path(signing_key_file)
            raw, self._enrollment_identity = _root_file(self._enrollment_path, 1024**2)
            key, self._key_identity = _root_file(self._key_path, 32)
            _require(len(key) == 32)
            self._signer = Ed25519PrivateKey.from_private_bytes(key)
            data = _strict(raw, 1024**2)
            _require(type(data) is dict and set(data) == {'schema', 'release', 'image', 'profile', 'daemon',
                'socket_path', 'consumer_host_uid', 'consumer_host_gid', 'receipt_lifetime_seconds',
                'request_timeout_ms', 'launch_timeout_ms'})
            _require(data['schema'] == 'executor_launch_supervisor_v1')
            release = dict(data['release'])
            _require(type(release.get('public_key')) is str and _HEX.fullmatch(release['public_key']))
            release['public_key'] = bytes.fromhex(release['public_key'])
            release['platform'] = Platform.model_validate(release['platform'])
            self.release = ExecutorReleaseEnrollment(**release)
            _require(self.release.public_key == self._signer.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw))
            self.profile = ExecutorLaunchProfile.model_validate(data['profile'])
            _require(self.profile.digest() == self.release.launch_profile_digest)
            image = dict(data['image'])
            _require(set(image) == set(VerifiedImage.__dataclass_fields__))
            image['platform'] = Platform.model_validate(image['platform'])
            self.image = VerifiedImage(**image)
            _require(self.image.runnable_reference == self.release.runnable_digest
                     and self.image.platform_manifest_digest == self.release.platform_manifest_digest
                     and self.image.image_config_id == self.release.image_config_id
                     and self.image.platform == self.release.platform
                     and self.image.source_commit == self.release.implementation_commit)
            self._daemon_pin = _DaemonPin(**data['daemon'])
            self._socket_path = _path(data['socket_path'])
            self._consumer_uid, self._consumer_gid = data['consumer_host_uid'], data['consumer_host_gid']
            _require(all(type(value) is int and 0 < value < 2**32 - 1
                         for value in (self._consumer_uid, self._consumer_gid)))
            self._ttl, self._request_ms, self._launch_ms = (data['receipt_lifetime_seconds'],
                data['request_timeout_ms'], data['launch_timeout_ms'])
            _require(type(self._ttl) is int and 1 <= self._ttl <= 3600
                     and type(self._request_ms) is int and 100 <= self._request_ms <= 10000
                     and type(self._launch_ms) is int and 100 <= self._launch_ms <= 120000)
            self._mounts = {item.role: item for item in self.profile.mounts}
            _require(self._mounts['issuer_socket'].source == str(self._socket_path)
                     and self._mounts['issuer_socket'].gid == self._consumer_gid
                     and self._mounts['config'].uid == self._consumer_uid
                     and self._mounts['config'].gid == self._consumer_gid
                     and self._mounts['engine_socket'].source == self._daemon_pin.socket_path)
            protected = (self._key_path.parent, self._enrollment_path.parent, _path(journal_file).parent)
            for mount in self.profile.mounts:
                source = Path(mount.source)
                _require(not any(source == item or source in item.parents or item in source.parents for item in protected))
            _require(self._key_path != self._enrollment_path and self._key_path != _path(journal_file)
                     and self._enrollment_path != _path(journal_file))
            self._journal = _Journal(journal_file, create=create_journal)
            self._engine = _UnixEngine(self._daemon_pin)
        except BaseException:
            self.close()
            raise LaunchSupervisorError() from None

    def _guard(self, *, allow_held=False):
        _require(self._pid == os.getpid() and not self._closed and (allow_held or not self._held))
        _require(_root_file(self._enrollment_path, 1024**2)[1] == self._enrollment_identity
                 and _root_file(self._key_path, 32)[1] == self._key_identity)

    def _hold(self):
        self._held = True
        self._issuance_open = False
        self._hold_persisted = False
        if self._launch_id is not None:
            try:
                self._journal.state(self._launch_id, 'held', self._record())
                self._hold_persisted = True
            except BaseException:
                # Original durable intent remains authoritative. A damaged
                # journal cannot certify that our held update was persisted.
                # Never leak its exception or reopen/retry it automatically.
                pass

    def _record(self):
        return {'container_id': self._container_id, 'manifest': self.release.launch_profile_digest,
                'boot_id': None if self._consumer is None else self._consumer.boot,
                'pid': None if self._consumer is None else self._consumer.pid,
                'start_ticks': None if self._consumer is None else self._consumer.start}

    def _pin_sources(self):
        for role, mount in self._mounts.items():
            path = _path(mount.source)
            _require(path.resolve(strict=True) == path)
            info = path.lstat()
            valid_kind = (stat.S_ISSOCK(info.st_mode) if mount.kind == 'socket' else
                          stat.S_ISREG(info.st_mode) if mount.kind == 'file' else stat.S_ISDIR(info.st_mode))
            _require(valid_kind and info.st_uid == mount.uid and info.st_gid == mount.gid
                     and stat.S_IMODE(info.st_mode) == mount.mode and not stat.S_ISLNK(info.st_mode))
            if mount.kind != 'directory':
                _require(info.st_nlink == 1)
            if mount.kind == 'file':
                _require(info.st_size <= 65536)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
                try:
                    raw = os.read(fd, 65537)
                    _require(len(raw) == info.st_size and _identity(os.fstat(fd)) == _identity(info)
                             and 'sha256:' + hashlib.sha256(raw).hexdigest() == mount.content_digest)
                finally:
                    os.close(fd)
            if role in self._source_pins:
                _require(self._source_pins[role] == _identity(info))
            else:
                self._source_pins[role] = _identity(info)

    def _bind_issuer(self):
        self._socket_parent = _root_parent(self._socket_path, private=False)
        try:
            os.stat(self._socket_path.name, dir_fd=self._socket_parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise LaunchSupervisorError()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.set_inheritable(False)
        pinned = f'/proc/self/fd/{self._socket_parent}/{self._socket_path.name}'
        _require(len(pinned.encode()) <= 107)
        self._listener.bind(pinned)
        info = os.stat(self._socket_path.name, dir_fd=self._socket_parent, follow_symlinks=False)
        self._socket_identity = _identity(info)
        os.chown(pinned, 0, self._consumer_gid, follow_symlinks=False)
        os.chmod(pinned, 0o660, follow_symlinks=False)
        self._listener.listen(1)

    def _create_body(self):
        p = self.profile
        return {'Image': self.image.runnable_reference, 'User': p.user,
            'Entrypoint': list(p.entrypoint), 'Cmd': list(p.command), 'Env': list(p.environment),
            'WorkingDir': '/', 'Hostname': '', 'Domainname': '', 'AttachStdin': False,
            'AttachStdout': False, 'AttachStderr': False, 'OpenStdin': False, 'Tty': False,
            'HostConfig': {'ReadonlyRootfs': True, 'Privileged': False, 'CapDrop': ['ALL'],
                'CapAdd': [], 'SecurityOpt': ['no-new-privileges:true'], 'Init': False,
                'PidMode': '', 'IpcMode': 'private', 'CgroupnsMode': 'private', 'NetworkMode': 'none',
                'PortBindings': {}, 'PublishAllPorts': False, 'Memory': p.memory_bytes,
                'MemorySwap': p.memory_bytes, 'NanoCpus': p.cpu_millicores * 1_000_000,
                'PidsLimit': p.pids_limit, 'GroupAdd': [str(value) for value in p.supplementary_groups],
                'Tmpfs': {'/tmp': f'rw,noexec,nosuid,nodev,size={p.tmpfs_bytes},mode=1777'},
                'Mounts': [{'Type': 'bind', 'Source': item.source, 'Target': item.target,
                            'ReadOnly': item.read_only, 'BindOptions': {'Propagation': 'rprivate'}} for item in p.mounts],
                'RestartPolicy': {'Name': 'no', 'MaximumRetryCount': 0}, 'AutoRemove': False,
                'Devices': [], 'DeviceRequests': [], 'ExtraHosts': [], 'Links': [], 'VolumesFrom': []}}

    def _inspect(self, deadline, *, running):
        _, info = self._engine.json('GET', '/containers/' + self._container_id + '/json', deadline)
        _require(type(info) is dict and info.get('Id') == self._container_id
                 and info.get('Image') in {self.image.image_config_id,
                     self.image.platform_manifest_digest, self.image.runnable_reference})
        expected = self._create_body()
        config, host, state = info.get('Config'), info.get('HostConfig'), info.get('State')
        _require(type(config) is dict and type(host) is dict and type(state) is dict)
        _require(config.get('Image') == self.image.runnable_reference)
        for key in ('User', 'Entrypoint', 'Cmd', 'Env', 'WorkingDir', 'AttachStdin', 'AttachStdout',
                    'AttachStderr', 'OpenStdin', 'Tty'):
            _require(config.get(key) == expected[key])
        _require(config.get('Volumes') in (None, {}) and not config.get('Healthcheck'))
        for key, value in expected['HostConfig'].items():
            if key == 'Mounts':
                continue
            actual = host.get(key)
            if key in {'CapAdd', 'Devices', 'DeviceRequests', 'ExtraHosts', 'Links', 'VolumesFrom'}:
                _require(actual in (None, []))
            else:
                _require(actual == value)
        actual_mounts = info.get('Mounts')
        _require(type(actual_mounts) is list and len(actual_mounts) == len(self.profile.mounts))
        observed = {}
        for item in actual_mounts:
            _require(type(item) is dict and item.get('Destination') not in observed)
            observed[item.get('Destination')] = item
        for mount in self.profile.mounts:
            actual = observed.pop(mount.target, None)
            _require(type(actual) is dict and actual.get('Type') == 'bind' and actual.get('Source') == mount.source
                     and actual.get('RW') is (not mount.read_only) and actual.get('Propagation') == 'rprivate')
        # Docker29 inspect.Mounts lists binds but excludes HostConfig.Tmpfs.
        # Tmpfs configuration is checked above; actual mount flags below.
        _require(not observed)
        _require(not state.get('Restarting') and not state.get('Paused') and not state.get('Dead'))
        _require(state.get('Running') is running)
        if running:
            _require(state.get('Status') == 'running' and type(state.get('Pid')) is int and state['Pid'] > 0
                     and info.get('Path') == self.profile.entrypoint[0]
                     and info.get('Args') == list((*self.profile.entrypoint[1:], *self.profile.command)))
        return state

    def launch(self):
        """One bounded real create/start. Any uncertain effect stays held forever."""
        _require(self._pid == os.getpid())
        with self._mutex:
            try:
                self._guard()
                _require(self._launch_id is None)
                deadline = time.monotonic() + self._launch_ms / 1000
                self._bind_issuer()
                self._pin_sources()
                defaults = self._engine.image(self.image, deadline)
                _require(not defaults.get('Volumes') and not defaults.get('Healthcheck') and not defaults.get('OnBuild')
                         and tuple(defaults.get('Entrypoint') or ()) == self.profile.entrypoint
                         and tuple(defaults.get('Cmd') or ()) == self.profile.command
                         and tuple(defaults.get('Env') or ()) == self.profile.environment)
                launch_id = str(uuid4())
                self._journal.reserve(launch_id, {'profile_digest': self.profile.digest(),
                    'image': self.image.runnable_reference,
                    'request_digest': 'sha256:' + hashlib.sha256(_canonical(self._create_body())).hexdigest()})
                self._launch_id = launch_id
                self._origin = time.monotonic()
                self._expires = self._origin + self._ttl
                _, created = self._engine.json('POST', '/containers/create?name=eh-executor-' + launch_id,
                    deadline, body=self._create_body(), statuses=(201,))
                _require(type(created) is dict and set(created) <= {'Id', 'Warnings'}
                         and type(created.get('Id')) is str and _HEX.fullmatch(created['Id'])
                         and created.get('Warnings', []) in (None, []))
                self._container_id = created['Id']
                self._journal.state(launch_id, 'created', self._record())
                self._inspect(deadline, running=False)
                self._journal.state(launch_id, 'start_reserved', self._record())
                self._engine.json('POST', '/containers/' + self._container_id + '/start', deadline, statuses=(204,))
                state = self._inspect(deadline, running=True)
                self._consumer = _Process(state['Pid'])
                self._validate_live(deadline)
                self._journal.state(launch_id, 'running', self._record())
                self._issuance_open = True
                return self._container_id
            except BaseException:
                self._hold()
                raise LaunchSupervisorError() from None

    def _validate_live(self, deadline, *, allow_held=False):
        self._guard(allow_held=allow_held)
        _require(self._consumer is not None)
        self._consumer.check()
        _require(_identity(os.stat(f'/proc/{self._consumer.pid}/ns/pid')) != self._engine.namespace)
        state = self._inspect(deadline, running=True)
        _require(state['Pid'] == self._consumer.pid)
        self._pin_sources()
        status = {}
        for line in Path(f'/proc/{self._consumer.pid}/status').read_text().splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                status[key] = value.strip()
        _kernel_profile(self.profile, status,
                        Path(f'/proc/{self._consumer.pid}/uid_map').read_text(),
                        Path(f'/proc/{self._consumer.pid}/gid_map').read_text(),
                        Path(f'/proc/{self._consumer.pid}/mountinfo').read_bytes(),
                        self._consumer_uid, self._consumer_gid)
        for namespace in ('ipc', 'cgroup', 'net'):
            _require(_identity(os.stat(f'/proc/{self._consumer.pid}/ns/{namespace}'))
                     != _identity(os.stat('/proc/self/ns/' + namespace)))
        # Config readback is not proof that rootless cgroup limits took effect.
        groups = Path(f'/proc/{self._consumer.pid}/cgroup').read_text().splitlines()
        _require(len(groups) == 1 and groups[0].startswith('0::/'))
        relative = groups[0][3:]
        _require(os.path.normpath(relative) == relative and '..' not in Path(relative).parts)
        cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/')
        _require(cgroup.resolve(strict=True) == cgroup)
        def value(name):
            raw = (cgroup / name).read_bytes()
            _require(len(raw) <= 4096)
            return raw.decode('ascii').strip()
        quota, period = value('cpu.max').split()
        _require(int(value('memory.max')) == self.profile.memory_bytes
                 and int(value('memory.swap.max')) == 0
                 and int(value('pids.max')) == self.profile.pids_limit
                 and quota != 'max' and int(period) > 0
                 and int(quota) * 1000 == self.profile.cpu_millicores * int(period)
                 and str(self._consumer.pid) in value('cgroup.procs').splitlines())
        arguments = Path(f'/proc/{self._consumer.pid}/cmdline').read_bytes().split(b'\0')
        _require(arguments[-1:] == [b''] and [part.decode('utf-8') for part in arguments[:-1]]
                 == [*self.profile.entrypoint, *self.profile.command])
        root = Path(f'/proc/{self._consumer.pid}/root')
        for mount in self.profile.mounts:
            observed = os.stat(str(root) + mount.target, follow_symlinks=False)
            _require(_identity(observed) == self._source_pins[mount.role])
        runner = str(root) + self.profile.runner_file
        fd = os.open(runner, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            _require(stat.S_ISREG(info.st_mode) and info.st_size <= 1024**2 and not info.st_mode & 0o022)
            raw = os.read(fd, 1024**2 + 1)
            _require(len(raw) == info.st_size and 'sha256:' + hashlib.sha256(raw).hexdigest() == self.profile.runner_digest)
        finally:
            os.close(fd)
        self._consumer.check()
        _remaining(deadline)

    @staticmethod
    def _receive(peer, size, deadline):
        parts = []
        while size:
            peer.settimeout(_remaining(deadline))
            part = peer.recv(size)
            _require(bool(part))
            parts.append(part)
            size -= len(part)
        return b''.join(parts)

    def serve_one_receipt(self):
        """One direct init-peer exchange; caller explicitly chooses to serve it."""
        _require(self._pid == os.getpid())
        with self._mutex:
            peer = None
            reserved = False
            try:
                self._guard()
                _require(self._issuance_open and time.monotonic() < self._expires)
                deadline = min(self._expires, time.monotonic() + self._request_ms / 1000)
                self._listener.settimeout(_remaining(deadline))
                peer, _ = self._listener.accept()
                credentials = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                _require(credentials == (self._consumer.pid, self._consumer_uid, self._consumer_gid))
                self._validate_live(deadline)
                size = struct.unpack('!I', self._receive(peer, 4, deadline))[0]
                _require(0 < size <= MAX_REQUEST_BYTES)
                request = _strict(self._receive(peer, size, deadline), MAX_REQUEST_BYTES)
                _require(type(request) is dict and set(request) == {'protocol', 'nonce'}
                         and request['protocol'] == REQUEST_PROTOCOL and type(request['nonce']) is str
                         and _HEX.fullmatch(request['nonce']))
                peer.settimeout(_remaining(deadline))
                _require(peer.recv(1) == b'')  # One frame plus explicit SHUT_WR, no trailing data.
                self._validate_live(deadline)
                self._journal.reserve_issuance(self._launch_id, request['nonce'])
                reserved = True
                self._issuance_open = False
                self._validate_live(deadline)
                now = int(time.time() * 1000)
                remaining = int((self._expires - time.monotonic()) * 1000)
                _require(remaining > 0)
                payload = {'protocol': PROTOCOL, 'audience': AUDIENCE, 'nonce': request['nonce'],
                    'receipt_id': str(uuid4()), 'launch_id': self._launch_id, 'issuer_id': self.release.issuer_id,
                    'boot_id': self._consumer.boot, 'authenticated_host_pid': self._consumer.pid,
                    'process_start_ticks': self._consumer.start, 'container_id': self._container_id,
                    'runnable_digest': self.release.runnable_digest,
                    'platform_manifest_digest': self.release.platform_manifest_digest,
                    'image_config_id': self.release.image_config_id,
                    'platform': self.release.platform.model_dump(mode='json'),
                    'implementation_commit': self.release.implementation_commit,
                    'launch_profile_digest': self.release.launch_profile_digest,
                    'issued_at_ms': now, 'expires_at_ms': now + remaining}
                signature = base64.urlsafe_b64encode(self._signer.sign(SIGNATURE_DOMAIN + _canonical(payload))).rstrip(b'=').decode()
                raw = _canonical({'payload': payload, 'signature': signature})
                _require(len(raw) <= MAX_RECEIPT_BYTES)
                self._validate_live(deadline)
                peer.settimeout(_remaining(deadline))
                peer.sendall(struct.pack('!I', len(raw)) + raw)
                peer.shutdown(socket.SHUT_WR)
                _remaining(deadline)
                self._journal.issuance_state(self._launch_id, 'sent', hashlib.sha256(raw).hexdigest())
                return payload['receipt_id']
            except BaseException:
                if reserved:
                    try:
                        self._journal.issuance_state(self._launch_id, 'held', None)
                    except BaseException:
                        pass
                self._hold()
                raise LaunchSupervisorError() from None
            finally:
                if peer is not None:
                    peer.close()

    def graceful_stop(self):
        """Explicit SIGTERM only. Timeout is held, never permission for SIGKILL."""
        _require(self._pid == os.getpid())
        with self._mutex:
            try:
                self._guard(allow_held=True)
                self._issuance_open = False
                deadline = time.monotonic() + self._launch_ms / 1000
                self._validate_live(deadline, allow_held=True)
                self._journal.state(self._launch_id, 'term_reserved', self._record())
                self._engine.json('POST', '/containers/' + self._container_id + '/kill?signal=SIGTERM',
                                  deadline, statuses=(204,))
                _require(bool(select.select([self._consumer.fd], [], [], _remaining(deadline))[0]))
                self._inspect(deadline, running=False)
                self._journal.state(self._launch_id, 'stopped', self._record())
                self._remove_owned_socket()
            except BaseException:
                self._hold()
                raise LaunchSupervisorError() from None

    def _remove_owned_socket(self):
        _require(self._socket_parent is not None and self._socket_identity is not None
                 and _identity(os.stat(self._socket_path.parent, follow_symlinks=False))
                     == _identity(os.fstat(self._socket_parent)))
        info = os.stat(self._socket_path.name, dir_fd=self._socket_parent, follow_symlinks=False)
        _require(_identity(info) == self._socket_identity and stat.S_ISSOCK(info.st_mode)
                 and info.st_uid == 0 and info.st_gid == self._consumer_gid
                 and stat.S_IMODE(info.st_mode) == 0o660)
        os.unlink(self._socket_path.name, dir_fd=self._socket_parent)
        os.fsync(self._socket_parent)
        self._socket_identity = None

    def close(self):
        """Close owned FDs only; unknown/live containers and socket evidence remain."""
        # Never wait on a mutex inherited across fork. The child can close only
        # its own descriptor copies; no flock unlock, journal update or unlink.
        if self._pid != os.getpid():
            self._close_descriptors()
            return
        with self._mutex:
            self._close_descriptors()

    def _close_descriptors(self):
        self._issuance_open = False
        self._closed = True
        failed = False
        for name in ('_listener', '_socket_parent', '_consumer', '_engine', '_journal'):
            value = getattr(self, name, None)
            if value is not None:
                try:
                    if name == '_socket_parent':
                        os.close(value)
                    else:
                        value.close()
                except BaseException:
                    failed = True
                finally:
                    setattr(self, name, None)
        self._signer = None
        if failed:
            raise LaunchSupervisorError() from None
