"""The coordinator recipe changes only the independently enrolled role command."""
import json
from pathlib import Path

import pytest

from engineering_hub.executor_launch_profile import ExecutorLaunchProfile, LaunchProfileError
from tests.test_executor_launch_profile import supervised_local_oauth_profile, supervised_local_profile


ROOT = Path(__file__).resolve().parents[1]


def test_recipe_has_only_pinned_parent_argument_and_supervised_command():
    lines = [line.strip() for line in (ROOT / 'docker/Dockerfile.hub-coordinator').read_text().splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    assert len(lines) == 4
    assert lines[:2] == ['ARG EXECUTOR_IMAGE', 'FROM ${EXECUTOR_IMAGE}']
    assert lines[2].startswith('ENTRYPOINT ') and lines[3].startswith('CMD ')
    entrypoint = json.loads(lines[2].removeprefix('ENTRYPOINT '))
    command = json.loads(lines[3].removeprefix('CMD '))
    for data in (supervised_local_profile(), supervised_local_oauth_profile()):
        profile = ExecutorLaunchProfile.model_validate(data)
        assert tuple(entrypoint) == profile.entrypoint
        assert tuple(command) == profile.command
        # The executor recipe supplies the identity and workdir inherited here.
        assert profile.user == '10001:10001'
    executor = (ROOT / 'docker/Dockerfile.hub-executor').read_text()
    assert '\nUSER 10001:10001\n' in executor and '\nWORKDIR /\n' in executor


def test_profile_rejects_executor_command_as_coordinator():
    data = supervised_local_oauth_profile()
    data['entrypoint'][-1] = 'hub_container_executor.installed_runner'
    with pytest.raises(LaunchProfileError):
        ExecutorLaunchProfile.model_validate(data)
