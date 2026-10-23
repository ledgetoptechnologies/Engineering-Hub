"""Explicit installed unified pilot selector; normal Hub startup stays inactive.

validate reads non-secret configuration only. serve requires prior independent
database/host enrollment and protected executor credentials. Neither operation
creates configuration, changes permissions, migrates state or installs services.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import sys
from urllib.parse import urlsplit
from uuid import UUID

from .container_pilot_manifest import PilotManifest
from .deployment_config import load_deployment_config
from .pilot_ownership import PilotOwner
from .pilot_service import REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
from .private_browser_socket import _private_parent, _check_parent, _trusted_ancestor
from .repository import AGENTS


SCHEMA = 'engineering-hub.unified-pilot.v1'
ACTION = 'engineering-hub:container-pilot'
ERROR = 'Unified pilot configuration or startup unavailable'


class UnifiedPilotStartupError(RuntimeError):
    def __init__(self):
        super().__init__(ERROR)


def _require(value):
    if not value:
        raise UnifiedPilotStartupError()


def _path(value):
    _require(type(value) is str and value.startswith('/') and value != '/'
             and not value.startswith('//') and '\\' not in value
             and posixpath.normpath(value) == value
             and not any(ord(c) < 32 or ord(c) == 127 for c in value))
    return Path(value)


def _object(value, keys):
    _require(type(value) is dict and set(value) == set(keys))
    return value


def _digest(value):
    _require(type(value) is str and re.fullmatch('sha256:[0-9a-f]{64}', value))
    return value


@dataclass(frozen=True)
class PilotProfile:
    manifest: PilotManifest = field(repr=False)
    actors: frozenset[str]
    task_ids: frozenset[str]
    machine_id: str
    origin: str
    ca_file: Path
    ca_sha256: str
    credential_file: Path = field(repr=False)
    credential_sha256: str = field(repr=False)
    grace_seconds: int


def parse_profile(raw):
    """Pure bounded closed parsing, not secret resolution or enrollment."""
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= 128 * 1024)
        def unique(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result)
                result[key] = value
            return result
        def reject(_):
            raise UnifiedPilotStartupError()
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=reject, parse_float=reject)
        _object(value, {'schema', 'manifest', 'machine_id', 'action_key', 'allowed_actors',
                        'allowed_task_ids', 'executor', 'supervisor_stop_grace_seconds'})
        _require(value['schema'] == SCHEMA and value['action_key'] == ACTION
                 and value['machine_id'] == 'staging-pc')
        manifest = PilotManifest.from_bytes(json.dumps(value['manifest'], separators=(',', ':')).encode())
        _require(manifest.target_id == 'engineering-hub:staging')
        actors, tasks = value['allowed_actors'], value['allowed_task_ids']
        _require(type(actors) is list and 1 <= len(actors) <= 3
                 and all(type(actor) is str and actor in AGENTS for actor in actors)
                 and len(set(actors)) == len(actors))
        _require(type(tasks) is list and 1 <= len(tasks) <= 32
                 and all(type(task) is str and str(UUID(task)) == task for task in tasks)
                 and len(set(tasks)) == len(tasks))
        executor = _object(value['executor'], {'https_origin', 'ca_file', 'ca_sha256',
                                              'credential_file', 'credential_sha256'})
        origin = executor['https_origin']
        _require(type(origin) is str and origin.isascii() and '\\' not in origin
                 and not any(ord(c) <= 32 or ord(c) == 127 for c in origin))
        parsed = urlsplit(origin)
        _require(parsed.scheme == 'https' and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment
                 and origin == 'https://' + parsed.netloc and parsed.netloc == parsed.netloc.lower())
        _require(parsed.port is None or 1 <= parsed.port <= 65535)
        grace = value['supervisor_stop_grace_seconds']
        _require(type(grace) is int and REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS <= grace <= 86400)
        return PilotProfile(manifest, frozenset(actors), frozenset(tasks), value['machine_id'],
            origin, _path(executor['ca_file']), _digest(executor['ca_sha256']),
            _path(executor['credential_file']), _digest(executor['credential_sha256']), grace)
    except Exception:
        raise UnifiedPilotStartupError() from None


def _file_identity(info, *, private):
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
             and info.st_uid in ({os.getuid()} if private else {0, os.getuid()})
             and (stat.S_IMODE(info.st_mode) == 0o600 if private else not info.st_mode & 0o022))
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_protected_file(path, *, maximum_bytes, private=True):
    """Read stable retained bytes through no-follow FDs, never reopen as TLS input.

    Secret/profile leaves require current-UID0600 and parent0700. Public CA
    bundles may be root/current-owned read-only-to-others under trusted parents.
    This does not sandbox equally privileged root or same-UID administrators.
    """
    directory = fd = None
    try:
        _require(sys.platform == 'linux' and type(private) is bool)
        path = _path(os.fspath(path))
        if private:
            directory, parent_identity = _private_parent(path)
        else:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            directory = os.open('/', flags)
            _trusted_ancestor(os.fstat(directory))
            for component in path.parent.parts[1:]:
                following = os.open(component, flags, dir_fd=directory)
                try:
                    opened = os.fstat(following)
                    _trusted_ancestor(opened)
                    named = os.stat(component, dir_fd=directory, follow_symlinks=False)
                    _require((opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino))
                except BaseException:
                    os.close(following)
                    raise
                os.close(directory)
                directory = following
            parent = os.fstat(directory)
            parent_identity = parent.st_dev, parent.st_ino
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        before = _file_identity(os.fstat(fd), private=private)
        _require(0 < before[2] <= maximum_bytes)
        _require(_file_identity(os.stat(path.name, dir_fd=directory, follow_symlinks=False), private=private) == before)
        pieces = bytearray()
        while len(pieces) <= maximum_bytes:
            piece = os.read(fd, min(65536, maximum_bytes + 1 - len(pieces)))
            if not piece:
                break
            pieces.extend(piece)
        _require(len(pieces) == before[2]
                 and _file_identity(os.fstat(fd), private=private) == before
                 and _file_identity(os.stat(path.name, dir_fd=directory, follow_symlinks=False), private=private) == before)
        if private:
            _check_parent(path, directory, parent_identity)
        else:
            parent = os.stat(path.parent, follow_symlinks=False)
            _trusted_ancestor(parent)
            _require(path.parent.resolve(strict=True) == path.parent
                     and (parent.st_dev, parent.st_ino) == parent_identity)
        return bytes(pieces)
    except Exception:
        raise UnifiedPilotStartupError() from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def load_profile(path):
    return parse_profile(read_protected_file(path, maximum_bytes=128 * 1024))


def _validated_configuration(config_path, profile_path):
    deployment = load_deployment_config(config_path)
    deployment.validate_ledger_boundary()
    if deployment.protected_ledger:
        _require(Path(profile_path) == deployment.hub_database.parent / 'pilot.json')
    profile = load_profile(profile_path)
    if deployment.protected_ledger:
        root = deployment.hub_database.parent
        _require(profile.credential_file == root / 'executor.token'
                 and profile.ca_file == root / 'staging-ca.pem')
    # A private secret child of the existing EH mount is permitted, but never
    # downloadable bus content or an alias/collision with other state/config.
    # Actual reading still rejects symlinks/hardlinks and requires UID0600/0700.
    credential = profile.credential_file.resolve()
    files = deployment.bus_paths['AGENT_BUS_FILE_ROOT'].resolve()
    collisions = {deployment.hub_database.resolve(), profile.ca_file.resolve(),
                  Path(config_path).resolve(), Path(profile_path).resolve(),
                  deployment.bus_paths['AGENT_BUS_DB'].resolve(),
                  deployment.bus_paths['AGENT_BUS_AUDIT'].resolve()}
    _require(credential not in collisions and credential != files and files not in credential.parents)
    return deployment, profile


def validate_configuration(config_path, profile_path):
    try:
        return _validated_configuration(config_path, profile_path)
    except Exception:
        raise UnifiedPilotStartupError() from None


async def serve_configuration(config_path, profile_path, *, reconciliation_factory=None,
                              session_acceptance_factory=None, session_enrollment_path=None):
    """The only activation operation; no new state, credential or policy setup."""
    owner = composition = None
    try:
        from .queued_pilot_service import _synchronous
        # JSON supplies no operator authority. CLI serve has no trusted local
        # acceptance channel and therefore refuses before owner/credential I/O.
        if session_enrollment_path is None:
            _require(_synchronous(reconciliation_factory))
            _require(session_acceptance_factory is None or _synchronous(session_acceptance_factory))
        else:
            # Only the explicit fixed selector supplies this DATA path. No
            # callback/module lookup or fallback is permitted in enrollment.
            _require(reconciliation_factory is None and session_acceptance_factory is None
                     and isinstance(session_enrollment_path, (str, Path))
                     and Path(session_enrollment_path).is_absolute())
        deployment, profile = validate_configuration(config_path, profile_path)
        from .app import Settings
        settings = Settings.from_env(deployment)
        # Must precede credentials, TLS transport allocation and listener binds.
        owner = PilotOwner(settings.database, require_existing=True)
        credential = read_protected_file(profile.credential_file, maximum_bytes=4096)
        _require('sha256:' + hashlib.sha256(credential).hexdigest() == profile.credential_sha256)
        _require(credential and all(33 <= value <= 126 for value in credential))
        _require(settings.agent_auth.identity_for(credential.decode('ascii')) is None)
        ca = read_protected_file(profile.ca_file, maximum_bytes=1024 * 1024, private=False)
        _require('sha256:' + hashlib.sha256(ca).hexdigest() == profile.ca_sha256)
        if session_enrollment_path is not None:
            from .trusted_queued_session import (parse_enrollment, MAX_ENROLLMENT_BYTES,
                                                 TrustedQueuedSessionComposition)
            enrollment = parse_enrollment(read_protected_file(Path(session_enrollment_path),
                                                               maximum_bytes=MAX_ENROLLMENT_BYTES))
            composition = TrustedQueuedSessionComposition(profile=profile, database=settings.database,
                enrollment=enrollment, ca_pem=ca, credential=credential)
            reconciliation_factory = composition.reconciliation_factory
            session_acceptance_factory = composition.session_acceptance_factory
        from .container_pilot_client import ContainerPilotClient
        from .unified_queued_pilot_service import serve_owned_unified_queued_pilot
        from .work_queue import WorkTarget
        def client_factory():
            return ContainerPilotClient.for_verified_https(profile.origin, credential.decode('ascii'), ca)
        def target_verifier(actor, action, environment, target):
            return (actor in profile.actors and action == ACTION and environment == 'staging'
                and type(target) is WorkTarget and target.task_id in profile.task_ids
                and target.source_commit == profile.manifest.candidate.source_commit
                and target.artifact_digest == profile.manifest.candidate.image_config_id)
        await serve_owned_unified_queued_pilot(settings, profile.manifest, client_factory,
            owner=owner, bus_paths=deployment.bus_paths, action_key=ACTION,
            machine_id=profile.machine_id, target_verifier=target_verifier,
            reconciliation_factory=reconciliation_factory,
            session_acceptance_factory=session_acceptance_factory,
            supervisor_stop_grace_seconds=profile.grace_seconds,
            agent_host='0.0.0.0', agent_port=8770, browser_host='0.0.0.0', browser_port=8771)
    except Exception:
        raise UnifiedPilotStartupError() from None
    finally:
        # A bind/pre-transfer failure leaves ownership with this selector.
        # After transfer the wrapper has drained and close is idempotent.
        try:
            if composition is not None:
                composition.close()  # Owned service has drained before returning.
        finally:
            if owner is not None:
                try:
                    owner.close()
                except Exception:
                    raise UnifiedPilotStartupError() from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('operation', choices=('validate', 'serve'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--pilot-config', required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'validate':
            _, profile = validate_configuration(args.config, args.pilot_config)
            print(json.dumps({'schema': SCHEMA, 'state': 'validated_not_activated',
                              'target_id': profile.manifest.target_id,
                              'allowed_tasks': len(profile.task_ids)}))
        else:
            asyncio.run(serve_configuration(args.config, args.pilot_config))
    except (Exception, KeyboardInterrupt):
        parser.exit(2, ERROR + '\n')


if __name__ == '__main__':
    main()
