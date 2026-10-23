"""Explicit colocated TEST pilot coordinator; not the deployed unified MCP.

Import and validation create no providers, credentials, sockets or databases.
The operator separately approves the immutable config, current-UID private local
state, socket mounts, TEST slots, Access application and resource/drain budget.
Neither canonical JSON nor file ownership supplies that approval. This process
must not receive an Engine socket, issuer key, or production bus state/tokens.
Only the existing queue/workflow can authorize reviewed tasks; this module's
policy is an additional exact-target veto, never an approval callback.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings
from engineering_hub.auth import AuthConfig
from engineering_hub.container_pilot_client import ContainerPilotClient
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.pilot_service import REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
from engineering_hub.queued_pilot_service import serve_owned_queued_pilot
from engineering_hub.repository import AGENTS
from engineering_hub.work_queue import WorkTarget
from .local_providers import ImmutableTestSecretResolver

SCHEMA = 'engineering-hub.installed-coordinator.v1'
ACTION = 'engineering-hub:container-pilot'
MAX_CONFIG_BYTES = 256 * 1024
_ERROR = 'Installed pilot coordinator unavailable'
_FIELDS = {'schema', 'manifest', 'database_path', 'coordinator_socket_path',
           'executor_socket_path', 'agent_port', 'machine_id', 'action_key',
           'allowed_actors', 'public_auth', 'secret_root', 'secret_bindings',
           'supervisor_stop_grace_seconds'}
_AUTH_FIELDS = {'base_url', 'cloudflare_issuer', 'cloudflare_audience',
                'cloudflare_allowed_emails'}


class InstalledCoordinatorError(RuntimeError):
    def __init__(self):
        super().__init__(_ERROR)


def _require(value):
    if not value:
        raise InstalledCoordinatorError()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _object(value, fields):
    _require(type(value) is dict and set(value) == set(fields))
    return value


def _path(value, *, socket=False):
    _require(type(value) is str and value.startswith('/') and value != '/'
             and not value.startswith('//') and '\\' not in value
             and not any(ord(char) < 32 or ord(char) == 127 for char in value)
             and os.path.normpath(value) == value)
    if socket:
        _require(len(os.fsencode(value)) < 108)
    return value


def _integer(value, low, high):
    _require(type(value) is int and low <= value <= high)
    return value


def _process():
    _require(sys.platform == 'linux' and os.getuid() == os.geteuid() == 10001
             and os.getgid() == os.getegid() == 10001 and os.getpid() == 1)


@dataclass(frozen=True)
class CoordinatorConfiguration:
    manifest: PilotManifest
    database: str
    coordinator_socket: str
    executor_socket: str
    port: int
    actors: frozenset[str]
    auth: AuthConfig
    secret_root: str
    bindings: tuple
    roles: object
    stop_grace: int
    process_id: int


def parse_configuration(raw):
    """Pure parsing and target validation, with no filesystem/provider effects."""
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= MAX_CONFIG_BYTES)

        def unique(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result)
                result[key] = value
            return result

        def reject(_):
            raise InstalledCoordinatorError()

        value = json.loads(raw, object_pairs_hook=unique, parse_float=reject,
                           parse_constant=reject)
        _object(value, _FIELDS)
        _require(value['schema'] == SCHEMA and _canonical(value) == raw)
        manifest = PilotManifest.from_bytes(_canonical(value['manifest']))
        _require(manifest.target_id == 'engineering-hub:staging'
                 and value['machine_id'] == 'staging-pc' and value['action_key'] == ACTION)
        paths = [_path(value[name], socket='socket' in name) for name in
                 ('database_path', 'coordinator_socket_path', 'executor_socket_path', 'secret_root')]
        _require(len(set(paths)) == len(paths))
        roots = [Path(path).parent for path in paths[:3]] + [Path(paths[3])]
        # Independent mounts cannot alias through a writable controller tree.
        # These are lexical constraints; installed mount/ownership proof remains
        # external and authoritative filesystem guards run when serving.
        _require(all(left != right and left not in right.parents and right not in left.parents
                     for index, left in enumerate(roots) for right in roots[index + 1:]))
        port = _integer(value['agent_port'], 1024, 65535)
        _require(port not in {8765, 8770, 8771})
        actors = value['allowed_actors']
        _require(type(actors) is list and 1 <= len(actors) <= 3
                 and all(type(actor) is str and actor in AGENTS for actor in actors)
                 and len(set(actors)) == len(actors))
        public = dict(_object(value['public_auth'], _AUTH_FIELDS))
        _require(all(type(public[name]) is str and 0 < len(public[name]) <= 512
                     for name in _AUTH_FIELDS - {'cloudflare_allowed_emails'}))
        emails = public['cloudflare_allowed_emails']
        _require(type(emails) is list and 1 <= len(emails) <= 16
                 and all(type(email) is str and email == email.lower() for email in emails))
        public['cloudflare_allowed_emails'] = tuple(emails)
        auth = AuthConfig(**public, human_auth_mode='cloudflare_access')
        _require(auth.cloudflare_config is not None)
        secrets = _object(value['secret_bindings'], {'codex', 'hermes', 'claude', 'executor'})
        roles, bindings = {}, []
        for name in ('codex', 'hermes', 'claude', 'executor'):
            item = _object(secrets[name], {'slot_id', 'version', 'digest'})
            slot = item['slot_id']
            _require(type(slot) is str and 6 <= len(slot) <= 96
                     and re.fullmatch(r'test-[a-z0-9][a-z0-9-]*', slot))
            version = _integer(item['version'], 1, 2**63 - 1)
            digest = item['digest']
            _require(type(digest) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', digest))
            roles[name] = (slot, version)
            bindings.append((slot, version, digest))
        _require(len(set(roles.values())) == 4 and len({item[2] for item in bindings}) == 4)
        return CoordinatorConfiguration(manifest, *paths[:3], port, frozenset(actors), auth,
            paths[3], tuple(bindings), MappingProxyType(roles),
            _integer(value['supervisor_stop_grace_seconds'],
                     REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS, 2**31 - 1), os.getpid())
    except Exception:
        raise InstalledCoordinatorError() from None


def _identity(info):
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
             and 0 < info.st_size <= MAX_CONFIG_BYTES)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def load_configuration(path):
    """Read one bounded protected file; no state creation or path repair."""
    fd = None
    try:
        _process()
        path = Path(_path(path))
        _require(path.resolve(strict=True) == path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        identity = _identity(os.fstat(fd))
        _require(_identity(os.lstat(path)) == identity and os.fstatvfs(fd).f_flag & os.ST_RDONLY)
        raw = bytearray()
        while len(raw) <= MAX_CONFIG_BYTES:
            part = os.read(fd, min(65536, MAX_CONFIG_BYTES + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        _require(len(raw) == identity[2] and _identity(os.fstat(fd)) == identity
                 and _identity(os.lstat(path)) == identity and path.resolve(strict=True) == path
                 and os.fstatvfs(fd).f_flag & os.ST_RDONLY)
        config = parse_configuration(bytes(raw))
        _require(path.parent != Path(config.database).parent
                 and Path(config.database).parent not in path.parents)
        return config
    except Exception:
        raise InstalledCoordinatorError() from None
    finally:
        if fd is not None:
            os.close(fd)


def target_allowed(config, actor, action_key, environment, target):
    """Additional veto only; repository task ownership/review remain mandatory."""
    return (type(config) is CoordinatorConfiguration and type(actor) is str and actor in config.actors
        and action_key == ACTION and environment == 'staging' and type(target) is WorkTarget
        and target.source_commit == config.manifest.candidate.source_commit
        and target.artifact_digest == config.manifest.candidate.image_config_id)


async def run_installed_coordinator(config, *, reconciliation_factory=None):
    try:
        _process()
        _require(type(config) is CoordinatorConfiguration and config.process_id == os.getpid())
        from engineering_hub.queued_pilot_service import _synchronous
        # Fixed JSON/CLI input cannot impersonate the missing trusted operator
        # channel. An explicit Python composition must supply that dependency.
        _require(_synchronous(reconciliation_factory))
        # The optional wheel can coexist with an older core having the same
        # distribution version. Deny that unsupported composition before even
        # reading TEST secrets. API presence is compatibility, not attestation
        # or permission to substitute an unapproved core artifact.
        _require(callable(getattr(ContainerPilotClient, 'for_private_unix', None)))
        with ImmutableTestSecretResolver(config.secret_root, bindings=config.bindings) as secrets:
            tokens = {name: secrets.resolve_test_secret(*slot) for name, slot in config.roles.items()}
        _require(len(set(tokens.values())) == 4 and all(
            type(token) is str and token and all(33 <= ord(char) <= 126 for char in token)
            for token in tokens.values()))
        settings = Settings(Path(config.database), AuthSettings(
            hermes_token=tokens['hermes'], codex_token=tokens['codex'], claude_token=tokens['claude'],
            allowed_ips=frozenset({'127.0.0.1'})), config.auth, work_queue_enabled=True,
            staging_machine_id='staging-pc',
            agent_allowed_hosts=('127.0.0.1', f'127.0.0.1:{config.port}'))

        def fresh_client():
            return ContainerPilotClient.for_private_unix(config.executor_socket, tokens['executor'])

        def policy(actor, action_key, environment, target):
            return target_allowed(config, actor, action_key, environment, target)

        await serve_owned_queued_pilot(settings, config.manifest, fresh_client,
            action_key=ACTION, machine_id='staging-pc', target_verifier=policy,
            reconciliation_factory=reconciliation_factory,
            socket_path=config.coordinator_socket, supervisor_stop_grace_seconds=config.stop_grace,
            agent_host='127.0.0.1', agent_port=config.port)
    except Exception:
        raise InstalledCoordinatorError() from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InstalledCoordinatorError()


def main(argv=None):
    try:
        parser = _Parser(description='Explicit colocated TEST pilot coordinator', allow_abbrev=False)
        parser.add_argument('operation', choices=('validate', 'serve'))
        parser.add_argument('--config', required=True, action='append')
        args = parser.parse_args(argv)
        _require(len(args.config) == 1)
        config = load_configuration(args.config[0])
        if args.operation == 'serve':
            asyncio.run(run_installed_coordinator(config))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(_ERROR, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
