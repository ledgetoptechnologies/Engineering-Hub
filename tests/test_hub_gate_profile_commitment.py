"""Pure synthetic profile/config binding; no app launch or host custody."""
import pytest

from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
from hub_container_executor import installed_runner
from hub_container_executor.launch_supervisor import LaunchSupervisor, LaunchSupervisorError
from tests.test_executor_launch_profile import gate_profile
from tests.test_hub_container_executor_installed_runner import configuration, canonical


def test_signed_profile_and_runner_gate_path_must_match():
    profile = ExecutorLaunchProfile.model_validate(gate_profile())
    supervisor = object.__new__(LaunchSupervisor)
    supervisor.profile = profile
    supervisor._mounts = {item.role: item for item in profile.mounts}
    data = configuration(); data['schema'] = installed_runner.GATE_SCHEMA
    data['gate_socket_directory'] = supervisor._mounts['gate_channel_directory'].source
    parsed = supervisor._runner_configuration(canonical(data))
    assert parsed.gate_socket_directory == data['gate_socket_directory']
    data['gate_socket_directory'] = '/fixture/other-gate'
    with pytest.raises(LaunchSupervisorError): supervisor._runner_configuration(canonical(data))


def test_v3_config_cannot_be_silently_used_with_legacy_profile():
    profile = ExecutorLaunchProfile.model_validate(gate_profile())
    data = configuration(); data['schema'] = installed_runner.GATE_SCHEMA
    data['gate_socket_directory'] = '/fixture/gate-control'
    legacy = profile.enrollment_value()
    legacy.pop('gate_channel'); legacy['mounts'].pop()
    supervisor = object.__new__(LaunchSupervisor)
    supervisor.profile = ExecutorLaunchProfile.model_validate(legacy)
    supervisor._mounts = {item.role: item for item in supervisor.profile.mounts}
    with pytest.raises(LaunchSupervisorError): supervisor._runner_configuration(canonical(data))


def test_ledger_config_requires_matching_signed_profile():
    data = gate_profile()
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    supervisor = object.__new__(LaunchSupervisor)
    supervisor.profile = ExecutorLaunchProfile.model_validate(data)
    supervisor._mounts = {item.role: item for item in supervisor.profile.mounts}
    config = configuration(); config['schema'] = installed_runner.GATE_LEDGER_SCHEMA
    config['gate_socket_directory'] = supervisor._mounts['gate_channel_directory'].source
    assert supervisor._runner_configuration(canonical(config)).gate_ledger_channel is True
    config['schema'] = installed_runner.GATE_SCHEMA
    with pytest.raises(LaunchSupervisorError): supervisor._runner_configuration(canonical(config))
    data.pop('gate_ledger_channel')
    supervisor.profile = ExecutorLaunchProfile.model_validate(data)
    config['schema'] = installed_runner.GATE_LEDGER_SCHEMA
    with pytest.raises(LaunchSupervisorError): supervisor._runner_configuration(canonical(config))
