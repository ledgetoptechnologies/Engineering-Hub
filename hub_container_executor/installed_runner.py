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
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
from types import MappingProxyType, SimpleNamespace

from engineering_hub.container_pilot_manifest import PilotManifest, Platform
from engineering_hub.pilot_service import REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
from engineering_hub.current_executor_observation import CurrentExecutorObserver, CurrentExecutorReleasePins
from .bound_installed_config import InstalledBoundProfile, parse_bound_profile
from .build_enrollment import BuildEnrollment
from .daemon_backend import DockerEngineBinding
from .launch_transport import UnixLaunchReceiptTransport, UnixCurrentObservationTransport
from .local_providers import ImmutableTestSecretResolver
from .local_resources import local_host_adapter_resources
from .runtime_identity import ExecutorReleaseEnrollment, LaunchReceiptIdentityVerifier
from .service import serve_owned_host_executor_unix


SCHEMA = 'engineering-hub.installed-executor.v1'
SCHEMA_SINGLE_PILOT = 'engineering-hub.installed-executor.v2'
SCHEMA_BOUND_PILOT = 'engineering-hub.installed-executor.v3'
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
    bound_profile: InstalledBoundProfile | None = None


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
        _require(type(value) is dict and value.get('schema') in {SCHEMA, SCHEMA_SINGLE_PILOT, SCHEMA_BOUND_PILOT})
        _object(value, _ROOT_FIELDS | ({'bound_profile'} if value['schema'] == SCHEMA_BOUND_PILOT else set()))
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
        resources = dict(_object(value['resources'], _RESOURCE_FIELDS | ({'single_pilot_protocol'} if value['schema'] in {SCHEMA_SINGLE_PILOT, SCHEMA_BOUND_PILOT} else set())))
        if value['schema'] in {SCHEMA_SINGLE_PILOT, SCHEMA_BOUND_PILOT}:
            _require(resources['single_pilot_protocol'] is True)
        bound = parse_bound_profile(value['bound_profile']) if value['schema'] == SCHEMA_BOUND_PILOT else None
        if bound is not None:
            _require(resources['journal_create'] is False)
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
        return RunnerConfiguration(manifest, binding, MappingProxyType(resources), release,
            MappingProxyType(transport),
            _integer(value['max_receipt_lifetime_seconds'], 1, 3600),
            _integer(value['exchange_timeout_ms'], 100, 10000),
            _integer(value['supervisor_stop_grace_seconds'],
                     REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS, 2**31 - 1),
            socket_path, bearer_slot, os.getpid(), bound)
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


async def _wait_for_approval_with_startup_signals(**kwargs):
    """Cancellation closes the native wait before serving has installed signals."""
    from .postlaunch_delivery import wait_for_postlaunch_approval
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    if task is None:
        raise InstalledRunnerError()
    previous = []
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            old = signal.getsignal(number)
            loop.add_signal_handler(number, task.cancel)
            previous.append((number, old))
        return await wait_for_postlaunch_approval(**kwargs)
    finally:
        for number, old in reversed(previous):
            loop.remove_signal_handler(number)
            signal.signal(number, old)


@contextmanager
def _continuous_bound_signals():
    """Own termination signals continuously from approval through service drain."""
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    if task is None:
        raise InstalledRunnerError()
    state = SimpleNamespace(stop=asyncio.Event(), serving=False)
    previous = []
    def request_stop():
        state.stop.set()
        if not state.serving:
            task.cancel()
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            old = signal.getsignal(number)
            loop.add_signal_handler(number, request_stop)
            previous.append((number, old))
        yield state
    finally:
        for number, old in reversed(previous):
            loop.remove_signal_handler(number)
            signal.signal(number, old)


async def run_installed_executor(config):
    """Obtain direct identity before provider state; drain before closing identity."""
    try:
        _process()
        _require(type(config) is RunnerConfiguration and config.process_id == os.getpid())
        with ExitStack() as owned:
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
            bound_options = {}
            service_signal_options = {}
            if config.bound_profile is not None:
                signal_state = owned.enter_context(_continuous_bound_signals())
                pins = CurrentExecutorReleasePins(**{name: getattr(config.release, name)
                    for name in CurrentExecutorReleasePins.__dataclass_fields__})
                from .postlaunch_delivery import wait_for_postlaunch_approval
                lease = await wait_for_postlaunch_approval(path=config.bound_profile.approval_path,
                    verifier=verifier, release=pins, approver_public_key=config.bound_profile.approver_public_key,
                    expected_engine_id=config.binding.daemon_identity, timeout_ms=config.bound_profile.approval_wait_ms)
                observer = CurrentExecutorObserver(release=pins, launch=lease.approval.launch,
                    transport=observations, exchange_timeout_ms=config.timeout_ms)
                owned.callback(observer.close)
                bound_options = dict(bound_profile=config.bound_profile, bound_observer=observer,
                                     bound_release=pins, bound_approval_lease=lease)
                if getattr(config.bound_profile, 'app_cgroup', None) is not None:
                    from .app_cgroup_observation import AppCgroupObserver
                    app_observer = AppCgroupObserver(transport=observations, release=pins,
                        launch=lease.approval.launch, pins=config.bound_profile.app_cgroup, timeout_ms=config.timeout_ms)
                    owned.callback(app_observer.close)
                    bound_options['bound_app_cgroup_observer'] = app_observer
                # The same handlers now request a graceful service drain. No
                # remove/reinstall window exists between these two phases.
                signal_state.serving = True
                service_signal_options = dict(stop_event=signal_state.stop, install_signals=False)
            with ImmutableTestSecretResolver(config.resources['secret_root'],
                    bindings=config.resources['secret_bindings']) as secrets:
                bearer = secrets.resolve_test_secret(*config.bearer_slot)
            factory = partial(local_host_adapter_resources, manifest=config.manifest,
                binding=config.binding, **config.resources, driver_identity_verifier=verifier, **bound_options)
            await serve_owned_host_executor_unix(factory, bearer_token=bearer,
                supervisor_stop_grace_seconds=config.stop_grace_seconds,
                socket_path=config.socket_path, current_observation_transport=observations,
                current_observation_timeout_ms=config.timeout_ms, **service_signal_options)
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
    except asyncio.CancelledError:
        return 130
    except Exception:
        print(_ERROR, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
