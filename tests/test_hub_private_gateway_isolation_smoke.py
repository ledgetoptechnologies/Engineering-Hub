"""Offline harness contracts; these unit tests never contact a Docker daemon."""
import importlib.util
from pathlib import Path
import subprocess
import json

import pytest


def load():
    source = Path(__file__).parents[1] / 'scripts' / 'hub_private_gateway_isolation_smoke.py'
    spec = importlib.util.spec_from_file_location('gateway_isolation_smoke_under_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('image', [None, '', 'busybox:latest', 'engineering-hub-validation:test',
    'sha256:' + 'A' * 64, 'sha256:' + 'a' * 63, 'sha256:' + 'a' * 65,
    'ghcr.io/ledgetoptechnologies/engineering-hub@sha256:' + 'a' * 64])
def test_image_validation_precedes_every_external_command(monkeypatch, image):
    module = load()
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('No external commands allowed'))
    with pytest.raises(ValueError):
        module.smoke(image)


def test_import_is_inert(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('Import must not invoke Docker'))
    assert callable(load().smoke)


@pytest.mark.parametrize('failure', ['exit', 'timeout'])
def test_command_failures_do_not_echo_child_output(monkeypatch, failure):
    module = load()
    marker = 'synthetic-private-output-must-not-be-reported'

    def fail(*args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args[0], 1, output=marker, stderr=marker)
        raise subprocess.CalledProcessError(1, args[0], output=marker, stderr=marker)

    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(RuntimeError) as caught:
        module.run(['docker', 'version'], timeout=1)
    assert marker not in str(caught.value)


def profile(*, initializer=False, private=False):
    tmpfs = {'/tmp': 'rw,noexec,nosuid,nodev,size=64m,mode=1777'}
    if private:
        tmpfs['/private'] = 'rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700'
    return {'Config': {'User': '0:0' if initializer else '10001:10001', 'Env': ['PATH=/usr/bin']},
        'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Privileged': False,
            'PidMode': '', 'IpcMode': 'private', 'CgroupnsMode': 'private', 'Binds': None,
            'Devices': [], 'DeviceRequests': None, 'PortBindings': {}, 'CapDrop': ['ALL'],
            'CapAdd': ['CHOWN'] if initializer else None, 'SecurityOpt': ['no-new-privileges:true'],
            'Memory': 1024 ** 3, 'MemorySwap': 1024 ** 3, 'NanoCpus': 2_000_000_000,
            'PidsLimit': 128, 'Tmpfs': tmpfs},
        'Mounts': [{'Type': 'volume', 'Name': 'owned-volume', 'Destination': '/socket',
                    'RW': initializer or private}]}


@pytest.mark.parametrize('options', [{}, {'initializer': True}, {'private': True}])
def test_expected_profiles_are_accepted(options):
    load()._profile(profile(**options), 'owned-volume', **options)


@pytest.mark.parametrize('key,value', [
    ('NetworkMode', 'host'), ('ReadonlyRootfs', False), ('Privileged', True),
    ('PidMode', 'host'), ('IpcMode', 'host'), ('CgroupnsMode', 'host'),
    ('Binds', ['/host:/private']), ('Devices', [{'PathOnHost': '/dev/sda'}]),
    ('DeviceRequests', [{'Capabilities': [['gpu']]}]), ('PortBindings', {'8771/tcp': [{}]}),
    ('CapDrop', []), ('CapAdd', ['SYS_ADMIN']), ('SecurityOpt', []),
    ('Memory', 1), ('MemorySwap', -1), ('NanoCpus', 1), ('PidsLimit', 1),
    ('Tmpfs', {'/tmp': 'rw', '/private': 'rw'})])
def test_actual_host_profile_drift_is_rejected(key, value):
    module = load()
    actual = profile()
    actual['HostConfig'][key] = value
    with pytest.raises(module.SmokeError):
        module._profile(actual, 'owned-volume')


@pytest.mark.parametrize('change', ['token', 'root', 'bind', 'other-volume'])
def test_private_env_or_unexpected_mount_is_rejected(change):
    module = load()
    actual = profile()
    if change == 'token':
        actual['Config']['Env'].append('CODEX_TOKEN=synthetic-forbidden')
    elif change == 'root':
        actual['Config']['User'] = '0:0'
    elif change == 'bind':
        actual['Mounts'].append({'Type': 'bind', 'Destination': '/private'})
    else:
        actual['Mounts'][0]['Name'] = 'unrelated-volume'
    with pytest.raises(module.SmokeError):
        module._profile(actual, 'owned-volume')


def test_public_writable_socket_mount_is_rejected():
    module = load()
    actual = profile()
    actual['Mounts'][0]['RW'] = True
    with pytest.raises(module.SmokeError):
        module._profile(actual, 'owned-volume')


@pytest.mark.parametrize('failure', ['label', 'identity', 'inspect'])
def test_unknown_container_preserves_container_and_volume(failure):
    module = load()
    calls = []
    owner, identifier = 'owner', 'a' * 64

    def docker(*args, **kwargs):
        calls.append(args)
        if failure == 'inspect':
            raise module.SmokeError('Unknown inspect result')
        return json.dumps({'Id': 'b' * 64 if failure == 'identity' else identifier,
            'Name': '/fixture', 'Config': {'Labels': {
                module._LABEL: 'not-owner' if failure == 'label' else owner}}})

    with pytest.raises(module.SmokeError):
        module._cleanup(docker, {'fixture': identifier}, 'owned-volume', None, owner)
    assert calls and all(call[:2] == ('container', 'inspect') for call in calls)
