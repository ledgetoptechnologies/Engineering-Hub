"""Synthetic Docker CLI responses: not installed-image or host acceptance."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


SPEC = importlib.util.spec_from_file_location('executor_image_smoke',
    Path(__file__).resolve().parents[1] / 'scripts/hub_executor_image_smoke.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
COMMIT = 'a' * 40
IMAGE = 'engineering-hub-executor:build-fixture'
IMAGE_ID = 'sha256:' + 'b' * 64


def defaults():
    return {'Id': IMAGE_ID, 'Config': {'User': '10001:10001', 'WorkingDir': '/',
        'Entrypoint': MODULE.ENTRYPOINT, 'Cmd': MODULE.COMMAND, 'Env': MODULE.ENVIRONMENT}}


def runtime(owner):
    return {'Image': IMAGE_ID, 'Mounts': [], 'Config': {'Labels': {MODULE.OWNER: owner}},
        'State': {'Running': False, 'ExitCode': 0}, 'HostConfig': {
            'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
            'SecurityOpt': ['no-new-privileges:true'], 'Memory': 256 * 1024**2,
            'MemorySwap': 256 * 1024**2, 'NanoCpus': 1_000_000_000, 'PidsLimit': 64}}


def fake_cli(monkeypatch, *, alter=None, fail_start=False):
    calls, cleanup = [], []
    owner = []
    def run(*args, **kwargs):
        calls.append(args)
        if args[1:3] == ('image', 'inspect'):
            return json.dumps([defaults()])
        if args[1:3] == ('container', 'create'):
            owner.append(args[args.index('--label') + 1].split('=', 1)[1])
            return 'container-id'
        if args[1:3] == ('container', 'start'):
            if fail_start:
                raise RuntimeError('fixture start failed')
            return json.dumps({'source_commit': COMMIT, 'installed_artifact': 'passed',
                               'private_unix_client': True})
        if args[1:3] == ('container', 'inspect'):
            info = runtime(owner[0])
            if alter:
                alter(info)
            return json.dumps([info])
        pytest.fail('unexpected CLI operation')
    monkeypatch.setattr(MODULE, 'run', run)
    monkeypatch.setattr(MODULE, 'remove_owned', lambda *args: cleanup.append(args))
    return calls, cleanup


def test_artifact_check_uses_exact_image_fixed_probe_and_no_host_authority(monkeypatch):
    calls, cleanup = fake_cli(monkeypatch)
    result = MODULE.smoke(IMAGE, COMMIT)
    create = next(args for args in calls if args[1:3] == ('container', 'create'))
    assert create[-6:] == (IMAGE_ID, '-I', '-B', '-c', MODULE.PROBE, COMMIT)
    assert create[create.index('--network') + 1] == 'none'
    assert create[create.index('--user') + 1] == '10001:10001'
    assert create[create.index('--pull') + 1] == 'never'
    assert '--read-only' in create
    assert not any(value in create for value in ('--mount', '-v', '--volume', '-e', '--env',
                                                '--pid', '--privileged', '--publish'))
    assert len(cleanup) == 1 and cleanup[0][0].startswith('hub-executor-artifact-')
    assert result['issuer_exchange'] == result['staging_pilot'] == 'not_tested'
    assert result['truenas'] == 'not_touched'
    assert result['host_mounts'] == 0


@pytest.mark.parametrize('image', ['latest', 'engineering-hub-executor:latest',
    'ghcr.io/other/executor@sha256:' + 'a' * 64, '--help', IMAGE + ';echo bad'])
def test_rejects_unbounded_image_selection_before_docker(monkeypatch, image):
    monkeypatch.setattr(MODULE, 'run', lambda *a, **k: pytest.fail('Docker called'))
    with pytest.raises(ValueError):
        MODULE.smoke(image, COMMIT)


@pytest.mark.parametrize('commit', ['HEAD', 'a' * 39, 'A' * 40, '--help'])
def test_rejects_nonexact_commit_before_docker(monkeypatch, commit):
    monkeypatch.setattr(MODULE, 'run', lambda *a, **k: pytest.fail('Docker called'))
    with pytest.raises(ValueError):
        MODULE.smoke(IMAGE, commit)


@pytest.mark.parametrize('field,value', [('User', '0'), ('Entrypoint', ['/bin/sh']),
    ('Cmd', []), ('Env', MODULE.ENVIRONMENT + ['PYTHONPATH=/checkout']),
    ('Volumes', {'/state': {}}), ('Healthcheck', {'Test': ['CMD', 'false']}),
    ('OnBuild', ['RUN false']), ('ExposedPorts', {'8770/tcp': {}}), ('WorkingDir', '/checkout')])
def test_defaults_fail_closed(field, value):
    info = copy.deepcopy(defaults())
    info['Config'][field] = value
    with pytest.raises(ValueError):
        MODULE.verify_defaults(info)


@pytest.mark.parametrize('field,value', [('NetworkMode', 'host'), ('Privileged', True),
    ('ReadonlyRootfs', False), ('Memory', 0), ('MemorySwap', -1), ('PidsLimit', 0),
    ('Binds', ['/tmp:/state']), ('Tmpfs', {'/tmp': 'rw'}), ('PortBindings', {'8770/tcp': []})])
def test_effective_profile_mismatch_fails_and_cleans_owned_fixture(monkeypatch, field, value):
    calls, cleanup = fake_cli(monkeypatch, alter=lambda info: info['HostConfig'].update({field: value}))
    with pytest.raises(RuntimeError):
        MODULE.smoke(IMAGE, COMMIT)
    assert len(cleanup) == 1


def test_start_failure_still_runs_owned_cleanup(monkeypatch):
    _, cleanup = fake_cli(monkeypatch, fail_start=True)
    with pytest.raises(RuntimeError):
        MODULE.smoke(IMAGE, COMMIT)
    assert len(cleanup) == 1


def test_cleanup_refuses_unknown_owner(monkeypatch):
    monkeypatch.setattr(MODULE.subprocess, 'run', lambda *a, **k:
        subprocess.CompletedProcess(a, 0, json.dumps([runtime('someone-else')]), ''))
    monkeypatch.setattr(MODULE, 'run', lambda *a, **k: pytest.fail('remove called'))
    with pytest.raises(RuntimeError, match='ownership mismatch'):
        MODULE.remove_owned('fixture', 'expected')


def test_probe_fixed_installed_paths_and_required_core_api():
    assert 'installed_coordinator' in MODULE.PROBE and 'launch_supervisor' in MODULE.PROBE
    assert 'configuration_commitment' in MODULE.PROBE
    assert 'for_private_unix' in MODULE.PROBE
    assert '/opt/venv/lib/python3.12/site-packages' in MODULE.PROBE
    assert 'run_installed_executor(' not in MODULE.PROBE
    assert 'LaunchSupervisor(' not in MODULE.PROBE
    assert 'run_installed_coordinator(' not in MODULE.PROBE


def test_subprocess_failure_does_not_reflect_output(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr='private-engine-output')
    monkeypatch.setattr(MODULE.subprocess, 'run', fail)
    with pytest.raises(RuntimeError) as error:
        MODULE.run('docker', 'fixture')
    assert 'private-engine-output' not in str(error.value)
