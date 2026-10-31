"""Protected input capture only; the supervisor launch guard stays closed."""
import hashlib
import os
import socket
from types import SimpleNamespace

import pytest

pytest.importorskip('fcntl')
from hub_container_executor import launch_supervisor as launch
from engineering_hub import executor_launch_profile as profiles
from engineering_hub.trusted_queued_session import SCHEMA_V3
from tests.test_executor_launch_profile import trusted_local_profile, _swap_mount
from tests.test_hub_container_executor_installed_coordinator import github_configuration, canonical
from tests.test_hub_trusted_queued_session import v2_document, encode
from tests.test_hub_unified_pilot_startup import profile_document


@pytest.mark.skipif(os.geteuid() != 0, reason='Synthetic mapped-owner capture requires isolated root')
def test_pair_capture_pins_content_and_denies_replacement(tmp_path, monkeypatch):
    root = tmp_path / 'hub-pilot-local'; root.mkdir(mode=0o700)
    config = github_configuration(); config['manifest'] = profile_document()['manifest']
    config.update(database_path='/var/lib/engineering-hub/local-coordinator/hub.sqlite',
                  coordinator_socket_path='/run/hub-browser/hub.sock',
                  executor_socket_path='/run/hub-executor/executor.sock',
                  secret_root='/var/lib/engineering-hub/local-secrets')
    enrollment = v2_document(); enrollment.update(schema=SCHEMA_V3, operator_actor='human:123')
    config_raw, enrollment_raw = canonical(config), encode(enrollment)
    data = trusted_local_profile(); data['fixture_root'] = str(root)
    data['daemon_policy']['engine_id'] = enrollment['launch']['engine_id']
    for item in data['mounts']:
        item['source'] = str(root / item['role'])
    for role, raw in [('local_config', config_raw), ('local_enrollment', enrollment_raw)]:
        target = root / role
        target.write_bytes(raw); os.chmod(target, 0o600); os.chown(target, 110001, 110001)
        _swap_mount(data, role, content_digest='sha256:' + hashlib.sha256(raw).hexdigest())
    for role in ('local_state', 'local_secrets', 'local_executor_directory', 'local_browser_directory'):
        directory = root / role; directory.mkdir(mode=0o700); os.chown(directory, 110001, 110001)
    profile = profiles.ExecutorLaunchProfile.model_validate(data)
    subject = SimpleNamespace(profile=profile, _mounts={m.role:m for m in profile.mounts},
        _source_pins={}, _configuration_raw_digest='sha256:'+hashlib.sha256(config_raw).hexdigest())
    subject._pin_trusted_local_inputs = lambda: launch.LaunchSupervisor._pin_trusted_local_inputs(subject)
    launch.LaunchSupervisor._pin_sources(subject)
    assert set(subject._source_pins) == set(profiles.TRUSTED_LOCAL_COORDINATOR_TARGETS)
    launch.LaunchSupervisor._pin_sources(subject)
    real_close = os.close
    real_parent = launch._root_parent
    attempted = []
    def fail_first_close(fd):
        attempted.append(fd)
        real_close(fd)
        if len(attempted) == 1:
            raise OSError('synthetic close failure')
    with monkeypatch.context() as patch:
        def open_parent_then_fault(*args, **kwargs):
            parent = real_parent(*args, **kwargs)
            patch.setattr(launch.os, 'close', fail_first_close)
            return parent
        patch.setattr(launch, '_root_parent', open_parent_then_fault)
        with pytest.raises(OSError): launch.LaunchSupervisor._pin_sources(subject)
    assert len(attempted) >= 2
    # Same-inode changed contents do not pass the independently pinned digest.
    (root / 'local_enrollment').write_bytes(enrollment_raw + b' ')
    with pytest.raises(launch.LaunchSupervisorError): launch.LaunchSupervisor._pin_sources(subject)
    # Restoring bytes does not undo the metadata change to a captured input.
    (root / 'local_enrollment').write_bytes(enrollment_raw)
    with pytest.raises(launch.LaunchSupervisorError): launch.LaunchSupervisor._pin_sources(subject)
    # A new name pointing at the old file is not an acceptable replacement.
    target = root / 'local_enrollment'
    moved = root / 'old-enrollment'; target.rename(moved); target.symlink_to(moved)
    with pytest.raises((launch.LaunchSupervisorError, OSError)): launch.LaunchSupervisor._pin_sources(subject)


@pytest.mark.skipif(os.geteuid() != 0, reason='Synthetic mapped-owner capture requires isolated root')
def test_supervised_capture_binds_root_raw_pin_and_release(tmp_path):
    from hub_container_executor.coordinator_admission import admission_commitment, parse_admission
    from tests.test_executor_launch_profile import supervised_local_profile
    from tests.test_hub_coordinator_admission import document
    root = tmp_path / 'hub-pilot-local'; root.mkdir(mode=0o700)
    config = github_configuration(); config['manifest'] = profile_document()['manifest']
    config.update(database_path='/var/lib/engineering-hub/local-coordinator/hub.sqlite',
                  coordinator_socket_path='/run/hub-browser/hub.sock',
                  executor_socket_path='/run/hub-executor/executor.sock',
                  secret_root='/var/lib/engineering-hub/local-secrets')
    evidence = v2_document(); evidence.update(schema=SCHEMA_V3, operator_actor='human:123')
    config_raw, evidence_raw = canonical(config), encode(evidence)
    data = supervised_local_profile(); data['fixture_root'] = str(root)
    data['daemon_policy']['engine_id'] = evidence['launch']['engine_id']
    for item in data['mounts']: item['source'] = str(root / item['role'])
    for role, raw in [('local_config', config_raw), ('local_enrollment', evidence_raw)]:
        target = root / role; target.write_bytes(raw); os.chmod(target, 0o600); os.chown(target, 110001, 110001)
        _swap_mount(data, role, content_digest='sha256:' + hashlib.sha256(raw).hexdigest())
    admission = document(config_raw, evidence_raw)
    normalized, _ = admission_commitment(canonical(admission))
    _swap_mount(data, 'local_admission', content_digest=normalized)
    profile = profiles.ExecutorLaunchProfile.model_validate(data)
    admission['release']['launch_profile_digest'] = profile.digest()
    raw = canonical(admission); _, raw_pin = admission_commitment(raw)
    target = root / 'local_admission'; target.write_bytes(raw); os.chmod(target, 0o600); os.chown(target, 110001, 110001)
    for role in ('local_state', 'local_secrets', 'local_executor_directory', 'local_browser_directory'):
        directory = root / role; directory.mkdir(mode=0o700); os.chown(directory, 110001, 110001)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(str(root / 'local_issuer_socket'))
        os.chmod(root / 'local_issuer_socket', 0o660); os.chown(root / 'local_issuer_socket', 0, 110001)
        subject = SimpleNamespace(profile=profile, release=parse_admission(raw).release,
            _mounts={m.role:m for m in profile.mounts}, _source_pins={},
            _configuration_raw_digest='sha256:'+hashlib.sha256(config_raw).hexdigest(),
            _admission_raw_digest=raw_pin, _ttl=30, _request_ms=2000)
        subject._pin_trusted_local_inputs = lambda: launch.LaunchSupervisor._pin_trusted_local_inputs(subject)
        launch.LaunchSupervisor._pin_sources(subject)
        assert set(subject._source_pins) == set(profiles.SUPERVISED_LOCAL_COORDINATOR_TARGETS)
        subject._ttl = 31
        with pytest.raises(launch.LaunchSupervisorError): launch.LaunchSupervisor._pin_sources(subject)
        subject._ttl = 30; subject._request_ms = 2001
        with pytest.raises(launch.LaunchSupervisorError): launch.LaunchSupervisor._pin_sources(subject)
        subject._request_ms = 2000
        subject._admission_raw_digest = 'sha256:'+'0'*64
        with pytest.raises(ValueError): launch.LaunchSupervisor._pin_sources(subject)
