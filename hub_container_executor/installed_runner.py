"""Explicit immutable-container entry point; importing this module starts nothing.

The independent launcher must pin this code, configuration bytes/mount, release
and actual PID1 profile. A canonical JSON document, ownership or read-only mount
does not approve its contents or isolate a hostile same-UID/Engine administrator.
This runner neither provisions paths nor installs, enrolls, reconciles or retries
anything. Its only credentials come from independently provisioned TEST slots.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import sys
from types import MappingProxyType

from engineering_hub.container_pilot_manifest import PilotManifest, Platform
from engineering_hub.pilot_service import REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
from .build_enrollment import BuildEnrollment
from .daemon_backend import DockerEngineBinding
from .launch_transport import UnixLaunchReceiptTransport, UnixCurrentObservationTransport
from .local_providers import ImmutableTestSecretResolver
from .local_resources import local_host_adapter_resources
from .runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier
from .service import serve_owned_host_executor_unix
from .broker_seqpacket import PeerAccount
from .broker_service import BrokerCleanupReport, serve_owned_inherited_broker
from .gate_mount_observation import GateMountVerifier
from engineering_hub.server import _signal_stop


SCHEMA = 'engineering-hub.installed-executor.v1'
PAIR_SCHEMA = 'engineering-hub.installed-executor.v2'
GATE_SCHEMA = 'engineering-hub.installed-executor.v3'
GATE_LEDGER_SCHEMA = 'engineering-hub.installed-executor.v5'
BASELINE_GATE_SCHEMA = 'engineering-hub.installed-executor.v6'
MAX_CONFIG_BYTES = 256 * 1024
_ERROR = 'Installed executor unavailable'
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_HEX_KEY = re.compile(r'[0-9a-f]{64}\Z')
_SLOT = re.compile(r'test-[a-z0-9][a-z0-9-]*\Z')
_ROOT_FIELDS = frozenset({
    'schema', 'manifest', 'docker_binding', 'resources', 'release_enrollment',
    'launch_transport', 'max_receipt_lifetime_seconds', 'exchange_timeout_ms',
    'supervisor_stop_grace_seconds', 'executor_socket_path', 'bearer_test_binding',
})
_RESOURCE_PATHS = frozenset({
    'adapter_database', 'journal_database', 'record_root', 'metadata_root',
    'baseline_receipt_root', 'secret_root', 'frozen_backup', 'reservation_root',
})
_RESOURCE_FIELDS = _RESOURCE_PATHS | {
    'journal_create', 'state_reservations_create', 'secret_bindings',
    'build_enrollments', 'baseline_receipt_digest', 'baseline_runtime_profile_digest',
}


class InstalledRunnerError(RuntimeError):
    def __init__(self):
        super().__init__(_ERROR)


def _require(value):
    if not value:
        raise InstalledRunnerError()


def _require_supported_gate_runtime(config):
    """Fail closed until independent installed-host gate acceptance exists."""
    _require(config.gate_socket_directory is None)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _object(value, fields):
    _require(type(value) is dict and set(value) == set(fields))
    return value


def _integer(value, minimum, maximum):
    _require(type(value) is int and minimum <= value <= maximum)
    return value


def _path(value):
    _require(type(value) is str and value.startswith('/') and value != '/'
             and not value.startswith('//') and '\x00' not in value
             and os.path.normpath(value) == value)
    return value


def _digest(value):
    _require(type(value) is str and _SHA.fullmatch(value))
    return value


def _slot(value, version):
    _require(type(value) is str and 6 <= len(value) <= 96 and _SLOT.fullmatch(value))
    return value, _integer(version, 1, 2**63 - 1)


@dataclass(frozen=True)
class RunnerConfiguration:
    """Parsed data, not proof of independent enrollment or launch authority."""
    manifest: PilotManifest
    binding: DockerEngineBinding
    resources: object
    release: ExecutorReleaseEnrollment
    transport: object
    lifetime_seconds: int
    timeout_ms: int
    stop_grace_seconds: int
    socket_path: str
    bearer_slot: tuple
    process_id: int
    pair: object = None
    gate_socket_directory: str | None = None
    gate_ledger_channel: bool = False
    baseline_gate: object = None


def parse_configuration(raw):
    """Pure closed parsing; never reads providers, secrets, sockets or Docker."""
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= MAX_CONFIG_BYTES)

        def unique(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result)
                result[key] = value
            return result

        def reject(_):
            raise InstalledRunnerError()

        value = json.loads(raw, object_pairs_hook=unique, parse_float=reject,
                           parse_constant=reject)
        _require(type(value) is dict and value.get('schema') in (SCHEMA, PAIR_SCHEMA, GATE_SCHEMA, GATE_LEDGER_SCHEMA, BASELINE_GATE_SCHEMA))
        extra = ({'pair'} if value['schema'] == PAIR_SCHEMA else
                 {'gate_socket_directory', 'baseline_gate'} if value['schema'] == BASELINE_GATE_SCHEMA else
                 {'gate_socket_directory'} if value['schema'] in (GATE_SCHEMA, GATE_LEDGER_SCHEMA) else set())
        _object(value, _ROOT_FIELDS | extra)
        _require(_canonical(value) == raw)
        manifest = PilotManifest.from_bytes(_canonical(value['manifest']))
        binding_data = dict(_object(value['docker_binding'], DockerEngineBinding.__dataclass_fields__))
        binding_data['platform'] = Platform.model_validate(binding_data['platform'])
        _path(binding_data['unix_socket_path'])
        _path(binding_data['generation_root'])
        binding = DockerEngineBinding(**binding_data)
        runner = manifest.runner_binding
        _require(binding.runner_id == runner.runner_id
                 and binding.daemon_identity == runner.daemon_identity
                 and binding.platform == runner.platform)
        resources = dict(_object(value['resources'], _RESOURCE_FIELDS))
        for name in _RESOURCE_PATHS:
            _path(resources[name])
        for name in ('journal_create', 'state_reservations_create'):
            _require(type(resources[name]) is bool)
        for name in ('baseline_receipt_digest', 'baseline_runtime_profile_digest'):
            _digest(resources[name])
        bindings = resources['secret_bindings']
        _require(type(bindings) is list and 1 <= len(bindings) <= 1024)
        slots = {}
        for row in bindings:
            _require(type(row) is list and len(row) == 3)
            key = _slot(row[0], row[1])
            _require(key not in slots)
            slots[key] = _digest(row[2])
        resources['secret_bindings'] = tuple((slot, version, digest)
            for (slot, version), digest in slots.items())
        enrollments = resources['build_enrollments']
        _require(type(enrollments) is list and 1 <= len(enrollments) <= 128)
        parsed_enrollments = []
        roles = set()
        for row in enrollments:
            _object(row, BuildEnrollment.__dataclass_fields__)
            item = BuildEnrollment(**row)
            key = item.manifest_digest, item.role
            _require(key not in roles)
            roles.add(key)
            parsed_enrollments.append(item)
        resources['build_enrollments'] = tuple(parsed_enrollments)
        release_data = dict(_object(value['release_enrollment'],
            (set(ExecutorReleaseEnrollment.__dataclass_fields__) - {'public_key'}) | {'public_key_hex'}))
        key = release_data.pop('public_key_hex')
        _require(type(key) is str and _HEX_KEY.fullmatch(key))
        release_data['public_key'] = bytes.fromhex(key)
        release_data['platform'] = Platform.model_validate(release_data['platform'])
        release = ExecutorReleaseEnrollment(**release_data)
        _require(release.implementation_commit == manifest.driver.implementation_commit
                 and release.platform == binding.platform)
        transport = dict(_object(value['launch_transport'], {
            'socket_path', 'expected_issuer_uid', 'expected_issuer_gid'}))
        _path(transport['socket_path'])
        _require(len(os.fsencode(transport['socket_path'])) < 108)
        for name in ('expected_issuer_uid', 'expected_issuer_gid'):
            _integer(transport[name], 0, 2**32 - 2)
        bearer = _object(value['bearer_test_binding'], {'slot_id', 'version'})
        bearer_slot = _slot(bearer['slot_id'], bearer['version'])
        _require(bearer_slot in slots)
        for name in ('codex', 'hermes', 'claude'):
            slot = getattr(manifest.runtime.secret_bindings, name)
            app_slot = slot.slot_id, slot.version
            _require(app_slot in slots and app_slot != bearer_slot
                     and slots[app_slot] != slots[bearer_slot])
        socket_path = _path(value['executor_socket_path'])
        _require(len(os.fsencode(socket_path)) < 108
                 and socket_path != transport['socket_path']
                 and socket_path != binding.unix_socket_path)
        pair = None
        gate_directory = None
        if value['schema'] in (GATE_SCHEMA, GATE_LEDGER_SCHEMA, BASELINE_GATE_SCHEMA):
            gate_directory = _path(value['gate_socket_directory'])
            gate = PurePosixPath(gate_directory)
            others = [PurePosixPath(resources[name]) for name in _RESOURCE_PATHS]
            others.extend((PurePosixPath(binding.unix_socket_path), PurePosixPath(binding.generation_root),
                           PurePosixPath(transport['socket_path']), PurePosixPath(socket_path)))
            _require(all(gate != other and gate not in other.parents and other not in gate.parents for other in others))
        baseline_gate = None
        if value['schema'] == BASELINE_GATE_SCHEMA:
            baseline_gate = dict(_object(value['baseline_gate'], {'issuer_id', 'issuer_public_key_hex',
                'receipt_public_key_hex', 'socket_source_directory', 'launch_transport'}))
            for name in ('issuer_public_key_hex', 'receipt_public_key_hex'):
                _require(type(baseline_gate[name]) is str and _HEX_KEY.fullmatch(baseline_gate[name])
                         and baseline_gate[name] != '0' * 64)
            baseline_release = ExecutorReleaseEnrollment(issuer_id=baseline_gate['issuer_id'],
                public_key=bytes.fromhex(baseline_gate['issuer_public_key_hex']),
                implementation_commit=release.implementation_commit, runnable_digest=release.runnable_digest,
                platform_manifest_digest=release.platform_manifest_digest, image_config_id=release.image_config_id,
                platform=release.platform, launch_profile_digest=release.launch_profile_digest)
            baseline_transport = dict(_object(baseline_gate['launch_transport'],
                {'socket_path', 'expected_issuer_uid', 'expected_issuer_gid'}))
            _path(baseline_transport['socket_path'])
            _require(len(os.fsencode(baseline_transport['socket_path'])) < 108)
            for name in ('expected_issuer_uid', 'expected_issuer_gid'):
                _integer(baseline_transport[name], 0, 2**32 - 2)
            _require(not any(ord(c) < 32 or ord(c) == 127 for c in baseline_gate['socket_source_directory']))
            baseline_dir = PurePosixPath(_path(baseline_gate['socket_source_directory']))
            others = [PurePosixPath(resources[name]) for name in _RESOURCE_PATHS]
            others.extend((PurePosixPath(binding.unix_socket_path), PurePosixPath(binding.generation_root),
                           PurePosixPath(transport['socket_path']), PurePosixPath(socket_path), gate,
                           PurePosixPath(baseline_transport['socket_path'])))
            _require(all(baseline_dir != other and baseline_dir not in other.parents
                         and other not in baseline_dir.parents for other in others))
            _require(baseline_transport['socket_path'] not in (transport['socket_path'], socket_path, binding.unix_socket_path))
            baseline_gate = MappingProxyType({'release': baseline_release,
                'public_key': bytes.fromhex(baseline_gate['receipt_public_key_hex']),
                'source_directory': str(baseline_dir), 'transport': MappingProxyType(baseline_transport)})
        if value['schema'] == PAIR_SCHEMA:
            pair = dict(_object(value['pair'], {'authority_public_key_hex', 'peer_uid', 'peer_gid'}))
            key = pair.pop('authority_public_key_hex')
            _require(type(key) is str and _HEX_KEY.fullmatch(key))
            pair['authority_public_key'] = bytes.fromhex(key)
            for name in ('peer_uid', 'peer_gid'):
                _integer(pair[name], 0, 2**32 - 2)
            pair = MappingProxyType(pair)
        return RunnerConfiguration(manifest, binding, MappingProxyType(resources), release,
            MappingProxyType(transport),
            _integer(value['max_receipt_lifetime_seconds'], 1, 3600),
            _integer(value['exchange_timeout_ms'], 100, 10000),
            _integer(value['supervisor_stop_grace_seconds'],
                     REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS, 2**31 - 1),
            socket_path, bearer_slot, os.getpid(), pair, gate_directory, value['schema'] in (GATE_LEDGER_SCHEMA, BASELINE_GATE_SCHEMA), baseline_gate)
    except Exception:
        raise InstalledRunnerError() from None


def _process():
    _require(sys.platform == 'linux' and os.getuid() == 10001
             and os.geteuid() == 10001 and os.getgid() == 10001
             and os.getegid() == 10001 and os.getpid() == 1)


def _file_identity(info):
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
             and 0 < info.st_size <= MAX_CONFIG_BYTES)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def load_configuration(path):
    """Read one immutable-mount file; parent/launcher authenticity stays external."""
    fd = None
    try:
        _process()
        path = Path(_path(path))
        _require(path.resolve(strict=True) == path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        identity = _file_identity(os.fstat(fd))
        _require(_file_identity(os.lstat(path)) == identity
                 and os.fstatvfs(fd).f_flag & os.ST_RDONLY)
        raw = bytearray()
        while len(raw) <= MAX_CONFIG_BYTES:
            fragment = os.read(fd, min(65536, MAX_CONFIG_BYTES + 1 - len(raw)))
            if not fragment:
                break
            raw.extend(fragment)
        _require(len(raw) == identity[2] and len(raw) <= MAX_CONFIG_BYTES
                 and _file_identity(os.fstat(fd)) == identity
                 and _file_identity(os.lstat(path)) == identity
                 and path.resolve(strict=True) == path
                 and os.fstatvfs(fd).f_flag & os.ST_RDONLY)
        return parse_configuration(bytes(raw))
    except Exception:
        raise InstalledRunnerError() from None
    finally:
        if fd is not None:
            os.close(fd)


async def run_installed_executor(config):
    """Obtain direct identity before provider state; drain before closing identity."""
    try:
        _process()
        _require(type(config) is RunnerConfiguration and config.process_id == os.getpid())
        # Until the supervisor supplies a fresh authenticated gate-mount
        # observation, v3 configuration is parseable but cannot start effects.
        _require_supported_gate_runtime(config)
        with ExitStack() as owned:
            if config.pair is not None:
                stop, report = asyncio.Event(), BrokerCleanupReport()
                owned.enter_context(_signal_stop(stop))
                enrollment_transport = UnixLaunchReceiptTransport(**config.transport)
                owned.callback(enrollment_transport.close)
                enrollment = enrollment_transport.issue_pair_enrollment(role='executor',
                    authority_public_key=config.pair['authority_public_key'], own_enrollment=config.release,
                    manifest_digest=config.manifest.digest(), timeout_ms=config.timeout_ms)
                await asyncio.sleep(0)
                _require(not stop.is_set())
                channel_transport = UnixLaunchReceiptTransport(**config.transport)
                owned.callback(channel_transport.close)
                fd, verifier = channel_transport.issue_enrolled_pair_channel(enrollment=enrollment, role='executor',
                    authority_public_key=config.pair['authority_public_key'], own_enrollment=config.release,
                    manifest_digest=config.manifest.digest(), max_receipt_lifetime_seconds=config.lifetime_seconds,
                    timeout_ms=config.timeout_ms)
                descriptor = [fd]
                def close_untransferred():
                    if descriptor[0] is not None:
                        os.close(descriptor[0])
                        descriptor[0] = None
                owned.callback(close_untransferred)
                owned.callback(verifier.close)
                verifier.claim_channel(fd)
                _require(verifier.implementation_commit() == config.release.implementation_commit
                         == config.manifest.driver.implementation_commit)
                await asyncio.sleep(0)
                _require(not stop.is_set())
                factory = partial(local_host_adapter_resources, manifest=config.manifest,
                    binding=config.binding, **config.resources, driver_identity_verifier=verifier)
                peer = PeerAccount(uid=config.pair['peer_uid'], gid=config.pair['peer_gid'])
                descriptor[0] = None  # The owned service takes custody at invocation.
                await serve_owned_inherited_broker(factory, fd=fd, peer=peer, stop_event=stop,
                                                   cleanup_report=report)
                _require(report.state == 'clean')
                return
            transport = UnixLaunchReceiptTransport(**config.transport)
            owned.callback(transport.close)
            verifier = LaunchReceiptIdentityVerifier(enrollment=config.release,
                issuer_transport=transport, max_receipt_lifetime_seconds=config.lifetime_seconds,
                exchange_timeout_ms=config.timeout_ms)
            owned.callback(verifier.close)
            _require(verifier.implementation_commit() == config.release.implementation_commit
                     == config.manifest.driver.implementation_commit)
            observations = UnixCurrentObservationTransport(**config.transport)
            owned.callback(observations.close)
            gate_options = {}
            if config.gate_socket_directory is not None:
                gate_verifier = GateMountVerifier(release=config.release, launch_verifier=verifier,
                    transport=observations, manifest_digest=config.manifest.digest(),
                    source_directory=config.gate_socket_directory)
                owned.callback(gate_verifier.close)
                gate_options = {'gate_socket_directory': config.gate_socket_directory,
                                'gate_mount_verifier': gate_verifier}
            if config.baseline_gate is not None:
                baseline_transport = UnixLaunchReceiptTransport(**config.baseline_gate['transport'])
                owned.callback(baseline_transport.close)
                baseline_launch = LaunchReceiptIdentityVerifier(enrollment=config.baseline_gate['release'],
                    issuer_transport=baseline_transport, max_receipt_lifetime_seconds=config.lifetime_seconds,
                    exchange_timeout_ms=config.timeout_ms)
                owned.callback(baseline_launch.close)
                baseline_observations = UnixCurrentObservationTransport(**config.baseline_gate['transport'])
                owned.callback(baseline_observations.close)
                baseline_mount = GateMountVerifier(release=config.baseline_gate['release'],
                    launch_verifier=baseline_launch, transport=baseline_observations,
                    manifest_digest=config.manifest.digest(), source_directory=config.baseline_gate['source_directory'])
                owned.callback(baseline_mount.close)
                gate_options.update(baseline_gate_public_key=config.baseline_gate['public_key'],
                                    baseline_gate_mount_verifier=baseline_mount)
            with ImmutableTestSecretResolver(config.resources['secret_root'],
                    bindings=config.resources['secret_bindings']) as secrets:
                bearer = secrets.resolve_test_secret(*config.bearer_slot)
            factory = partial(local_host_adapter_resources, manifest=config.manifest,
                binding=config.binding, **config.resources, driver_identity_verifier=verifier,
                **gate_options)
            gate_worker_factory = None
            if config.gate_ledger_channel:
                from .gate_ledger_channel import OwnedGateLedgerService
                from .gate_ledger_handoff import verify_handoff
                handoff_transport = UnixCurrentObservationTransport(**config.transport)
                owned.callback(handoff_transport.close)
                fd, raw, nonce, wall, started, profile_bytes = handoff_transport.issue_ledger_listener(
                    verifier=verifier, manifest_digest=config.manifest.digest(),
                    source_directory=config.gate_socket_directory, timeout_ms=config.timeout_ms)
                descriptor = [fd]
                def close_listener():
                    if descriptor[0] is not None:
                        os.close(descriptor[0]); descriptor[0] = None
                owned.callback(close_listener)
                def gate_worker_factory(adapter):
                    _require(descriptor[0] is not None)
                    verify_handoff(raw, descriptor[0], verifier=verifier, nonce=nonce,
                        manifest_digest=config.manifest.digest(), source_directory=config.gate_socket_directory,
                        start_wall_ms=wall, start_monotonic=started)
                    listener = socket.socket(fileno=descriptor[0]); descriptor[0] = None
                    try:
                        return OwnedGateLedgerService(listener=listener, ledger=adapter.ledger,
                            supervisor_public_key=verifier.issuer_public_key(), launch_verifier=verifier,
                            manifest_digest=config.manifest.digest(), hold_uncertain=adapter.ledger.hold_gate_uncertain,
                            timeout_seconds=min(5, config.timeout_ms / 1000), completion_profile_bytes=profile_bytes,
                            require_sender_liveness=True)
                    except BaseException:
                        listener.close()
                        raise
            await serve_owned_host_executor_unix(factory, bearer_token=bearer,
                supervisor_stop_grace_seconds=config.stop_grace_seconds,
                socket_path=config.socket_path, current_observation_transport=observations,
                current_observation_timeout_ms=config.timeout_ms,
                **({'gate_worker_factory': gate_worker_factory} if gate_worker_factory is not None else {}))
    except Exception:
        raise InstalledRunnerError() from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InstalledRunnerError()


def main(argv=None):
    try:
        parser = _Parser(description='Explicit immutable Engineering Hub executor', allow_abbrev=False)
        parser.add_argument('--config', required=True, action='append')
        args = parser.parse_args(argv)
        _require(len(args.config) == 1)
        config = load_configuration(args.config[0])
        asyncio.run(run_installed_executor(config))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(_ERROR, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
