"""Synthetic coordinator config binding, not a running coordinator attestation."""
import hashlib
import json
import os
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux supervisor file checks')
pytest.importorskip('fcntl')

from hub_container_executor.configuration_commitment import coordinator_configuration_commitment
from hub_container_executor.launch_supervisor import LaunchSupervisor, LaunchSupervisorError
from tests.test_hub_container_executor_installed_coordinator import configuration, canonical


def test_coordinator_commitment_pins_exact_closed_config_and_manifest():
    raw = canonical(configuration())
    commitment = coordinator_configuration_commitment(raw)
    assert commitment.raw_digest == 'sha256:' + hashlib.sha256(raw).hexdigest()
    changed = configuration()
    changed['agent_port'] += 1
    assert coordinator_configuration_commitment(canonical(changed)).content_digest != commitment.content_digest
    assert coordinator_configuration_commitment(canonical(changed)).manifest_digest == commitment.manifest_digest
    with pytest.raises(Exception):
        coordinator_configuration_commitment(raw + b' ')


def test_supervisor_pins_coordinator_config_and_refuses_later_change(tmp_path):
    path = tmp_path / 'coordinator.json'
    raw = canonical(configuration())
    path.write_bytes(raw)
    path.chmod(0o600)
    commitment = coordinator_configuration_commitment(raw)
    authority = LaunchSupervisor.__new__(LaunchSupervisor)
    authority.profile = SimpleNamespace(role='coordinator')
    authority._configuration_raw_digest = commitment.raw_digest
    authority._source_pins = {}
    authority._mounts = {'config': SimpleNamespace(source=str(path), kind='file', uid=os.getuid(),
        gid=os.getgid(), mode=0o600, content_digest=commitment.content_digest)}
    authority._pin_sources()
    altered = configuration()
    altered['agent_port'] += 1
    path.write_bytes(canonical(altered))
    with pytest.raises(LaunchSupervisorError):
        authority._pin_sources()


def test_supervisor_coordinator_config_needs_both_independent_pins(tmp_path):
    path = tmp_path / 'coordinator.json'
    raw = canonical(configuration())
    path.write_bytes(raw)
    path.chmod(0o600)
    commitment = coordinator_configuration_commitment(raw)
    for wrong_raw in (False, True):
        authority = LaunchSupervisor.__new__(LaunchSupervisor)
        authority.profile = SimpleNamespace(role='coordinator')
        authority._configuration_raw_digest = ('sha256:' + '0' * 64 if wrong_raw else commitment.raw_digest)
        authority._source_pins = {}
        authority._mounts = {'config': SimpleNamespace(source=str(path), kind='file', uid=os.getuid(),
            gid=os.getgid(), mode=0o600, content_digest=(commitment.content_digest if wrong_raw else 'sha256:' + '0' * 64))}
        with pytest.raises(LaunchSupervisorError):
            authority._pin_sources()


def test_signed_coordinator_profile_commits_config_and_manifest():
    from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
    from tests.test_executor_launch_profile import coordinator_profile
    data = configuration()
    first = coordinator_configuration_commitment(canonical(data))
    profile = coordinator_profile()
    next(mount for mount in profile['mounts'] if mount['role'] == 'config')['content_digest'] = first.content_digest
    digest = ExecutorLaunchProfile.model_validate(profile).digest()
    data['manifest']['candidate']['source_commit'] = 'f' * 40
    changed = coordinator_configuration_commitment(canonical(data))
    assert changed.manifest_digest != first.manifest_digest
    next(mount for mount in profile['mounts'] if mount['role'] == 'config')['content_digest'] = changed.content_digest
    assert ExecutorLaunchProfile.model_validate(profile).digest() != digest


def test_supervisor_derives_coordinator_manifest_from_pinned_config(tmp_path):
    path = tmp_path / 'coordinator.json'
    raw = canonical(configuration())
    path.write_bytes(raw)
    path.chmod(0o600)
    commitment = coordinator_configuration_commitment(raw)
    authority = LaunchSupervisor.__new__(LaunchSupervisor)
    authority.profile = SimpleNamespace(role='coordinator')
    authority._configuration_raw_digest = commitment.raw_digest
    authority._source_pins = {}
    authority._mounts = {'config': SimpleNamespace(source=str(path), kind='file', uid=os.getuid(),
        gid=os.getgid(), mode=0o600, content_digest=commitment.content_digest)}
    authority._pin_sources()
    assert authority._handoff_manifest_digest() == commitment.manifest_digest
    changed = configuration()
    changed['agent_port'] += 1
    path.write_bytes(canonical(changed))
    with pytest.raises(LaunchSupervisorError):
        authority._handoff_manifest_digest()
