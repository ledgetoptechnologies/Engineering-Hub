"""Harness command/cleanup tests; mocks are not actual container evidence."""
import importlib.util
import json
from pathlib import Path
import subprocess
import uuid

import pytest


spec = importlib.util.spec_from_file_location('durable_smoke', Path(__file__).resolve().parents[1] / 'scripts/hub_durable_container_smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
IMAGE = 'sha256:' + 'b' * 64
COMMIT = 'a' * 40


class Docker:
    def __init__(self):
        self.calls, self.volumes, self.containers = [], {}, {}
        self.profiles = {}
        self.corrupt_label = False
        self.fail_probe = False
        self.task = {'task_id': str(uuid.uuid4()), 'revision': 2, 'state': 'claimed',
                     'build_commit': COMMIT, 'auth_denials': True}

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        out, code, err = '', 0, ''
        if args[:2] == ['volume', 'create']:
            self.volumes[args[-1]] = args[args.index('--label') + 1].split('=', 1)[1]
        elif args[:2] == ['volume', 'inspect']:
            name = args[-1]
            if name not in self.volumes:
                code, err = 1, 'No such volume'
            else:
                out = name + '|' + self.volumes[name] + '|local|null'
        elif args[0] == 'create':
            name = args[args.index('--name') + 1]
            owner = args[args.index('--label') + 1].split('=', 1)[1]
            self.containers[name] = (f'{len(self.containers) + 1:064x}', owner)
            mounts = []
            for index, arg in enumerate(args):
                if arg == '--mount':
                    parts = args[index + 1].split(',')
                    data = dict(part.split('=', 1) for part in parts if '=' in part)
                    mounts.append({'Type': data['type'], 'Name': data['source'], 'Destination': data['target'],
                                   'Driver': 'local', 'RW': 'readonly' not in parts})
            self.profiles[self.containers[name][0]] = profile(mounts)
        elif args[:2] == ['container', 'inspect']:
            name = args[-1]
            if name in self.profiles:
                out = json.dumps(self.profiles[name])
            elif name not in self.containers:
                code, err = 1, 'No such container'
            else:
                identity, owner = self.containers[name]
                out = identity + '|' + ('foreign-owner' if self.corrupt_label else owner)
        elif args[0] == 'exec':
            script = args[4]
            if script == smoke.PROBE:
                if self.fail_probe:
                    self.corrupt_label = True
                    raise ValueError('synthetic failure')
                out = json.dumps(self.task)
            else:
                assert script == smoke.BACKUP
                out = json.dumps({'sha256': 'c' * 64, 'size_bytes': 32768,
                                  'quick_check': 'ok', 'permissions': '0600'})
        elif args[:2] == ['start', '--attach']:
            out = json.dumps({'sha256': 'c' * 64, 'size_bytes': 32768,
                              'quick_check': 'ok', 'integrity_check': 'ok'})
        elif args[:2] == ['rm', '--force']:
            self.containers = {key: value for key, value in self.containers.items() if value[0] != args[2]}
        elif args[:2] == ['volume', 'rm']:
            del self.volumes[args[2]]
        return subprocess.CompletedProcess(args, code, stdout=out, stderr=err)


def profile(mounts):
    return {'user': '10001:10001', 'network': 'none', 'readonly': True, 'privileged': False,
            'cap_drop': ['ALL'], 'cap_add': None, 'ports': {}, 'binds': None, 'devices': [],
            'tmpfs': {'/tmp': 'rw,noexec,nosuid,size=32m'}, 'security': ['no-new-privileges'],
            'memory': 512 * 1024 ** 2, 'nano_cpus': 1_000_000_000, 'pids_limit': 64,
            'mounts': mounts}


def test_isolated_restart_backup_restore_and_exact_cleanup(monkeypatch):
    docker = Docker()
    monkeypatch.setattr(smoke, '_docker', docker)
    report = smoke.run_smoke(IMAGE, COMMIT)
    assert report['same_container_restart'] == 'passed'
    assert report['restored_app_state'] == 'matched'
    assert report['disposable_containers_removed'] == 3
    assert report['disposable_volumes_removed'] == 2
    assert docker.containers == docker.volumes == {}
    calls = [args for args, kwargs in docker.calls]
    creates = [args for args in calls if args[0] == 'create']
    assert len(creates) == 3
    for args in creates:
        assert args[args.index('--network') + 1] == 'none'
        assert args[args.index('--user') + 1] == '10001:10001'
        assert args[args.index('--pull') + 1] == 'never'
        assert '--read-only' in args and '--cap-drop' in args
        assert IMAGE in args
        assert '--publish' not in args and '-v' not in args
        for index, value in enumerate(args):
            if value == '--mount':
                assert args[index + 1].startswith('type=volume,source=hub-durability-')
    restart = next(args for args in calls if args[0] == 'restart')
    assert len(restart[-1]) == 64
    removed = [args[2] for args in calls if args[:2] == ['rm', '--force']]
    assert len(removed) == 3 and all(len(value) == 64 for value in removed)


@pytest.mark.parametrize('image,commit', [('latest', COMMIT), (IMAGE, None), (IMAGE, 'main'), (IMAGE, 'A' * 40)])
def test_requires_exact_image_and_commit_before_any_docker(monkeypatch, image, commit):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid input must not reach Docker')
    monkeypatch.setattr(smoke, '_docker', forbidden)
    with pytest.raises(ValueError):
        smoke.run_smoke(image, commit)


def test_unknown_container_owner_is_not_removed(monkeypatch):
    docker = Docker()
    docker.fail_probe = True
    monkeypatch.setattr(smoke, '_docker', docker)
    with pytest.raises(RuntimeError, match='cleanup incomplete'):
        smoke.run_smoke(IMAGE, COMMIT)
    assert not any(args[:2] == ['rm', '--force'] for args, _ in docker.calls)
    assert len(docker.containers) == 1


def test_volume_wrong_owner_is_not_removed(monkeypatch):
    def docker(args, **kwargs):
        if args[:2] == ['volume', 'inspect']:
            return subprocess.CompletedProcess(args, 0, stdout=args[-1] + '|foreign|local|null', stderr='')
        pytest.fail('No delete or mutation allowed')
    monkeypatch.setattr(smoke, '_docker', docker)
    with pytest.raises(RuntimeError, match='ownership unverified'):
        smoke._volume('hub-durability-fixture', 'expected')


def test_subprocess_is_bounded_and_never_shell(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    smoke._docker(['version'])
    args, kwargs = calls[0]
    assert args == ['docker', 'version'] and kwargs['timeout'] == 30
    assert 'shell' not in kwargs and kwargs['capture_output'] is True


def test_probe_rejects_drifted_commit(monkeypatch):
    result = {'task_id': str(uuid.uuid4()), 'revision': 2, 'state': 'claimed',
              'build_commit': 'f' * 40, 'auth_denials': True}
    monkeypatch.setattr(smoke, '_docker', lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=json.dumps(result)))
    with pytest.raises(ValueError, match='unverified'):
        smoke._probe('e' * 64, 'verify', COMMIT)


def test_embedded_probe_scripts_compile():
    for name in ('PROBE', 'BACKUP', 'RESTORE'):
        compile(getattr(smoke, name), '<durability-' + name + '>', 'exec')


@pytest.mark.parametrize('change', [
    {'network': 'bridge'}, {'user': '0:0'}, {'privileged': True}, {'readonly': False},
    {'cap_add': ['SYS_ADMIN']}, {'ports': {'8770/tcp': [{'HostPort': '8770'}]}},
    {'binds': ['/var/run/docker.sock:/var/run/docker.sock']}, {'tmpfs': {}},
    {'security': []}, {'mounts': [{'Type': 'bind', 'Destination': '/var/lib/hub'}]},
    {'memory': 0}, {'memory': True}, {'nano_cpus': 0}, {'nano_cpus': 1e9},
    {'pids_limit': -1}, {'pids_limit': True},
])
def test_actual_profile_rejects_isolation_drift(monkeypatch, change):
    value = profile([{'Type': 'volume', 'Name': 'owned', 'Driver': 'local', 'Destination': '/var/lib/hub', 'RW': True}]) | change
    monkeypatch.setattr(smoke, '_docker', lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=json.dumps(value)))
    with pytest.raises(ValueError):
        smoke._profile('e' * 64, {'/var/lib/hub': ('owned', True)})


@pytest.mark.parametrize('restored', [False, True])
@pytest.mark.parametrize('change', [
    {'sha256': None}, {'sha256': 'c' * 63}, {'sha256': 'C' * 64},
    {'size_bytes': None}, {'size_bytes': True}, {'size_bytes': 0}, {'size_bytes': 1.0},
    {'quick_check': 'failed'}, {'unexpected': 'field'},
])
def test_backup_evidence_rejects_invalid_values(restored, change):
    value = {'sha256': 'c' * 64, 'size_bytes': 32768, 'quick_check': 'ok',
             ('integrity_check' if restored else 'permissions'): ('ok' if restored else '0600')}
    with pytest.raises(ValueError, match='Backup evidence unverified'):
        smoke._backup_report(value | change, restored=restored)


@pytest.mark.parametrize('restored', [False, True])
def test_backup_evidence_requires_all_fields_and_exact_role(restored):
    value = {'sha256': 'c' * 64, 'size_bytes': 32768, 'quick_check': 'ok',
             ('integrity_check' if restored else 'permissions'): ('ok' if restored else '0600')}
    assert smoke._backup_report(value, restored=restored) == value
    for key in value:
        missing = dict(value)
        del missing[key]
        with pytest.raises(ValueError):
            smoke._backup_report(missing, restored=restored)
    with pytest.raises(ValueError):
        smoke._backup_report(value, restored=not restored)
    invalid = value | {('integrity_check' if restored else 'permissions'): 'wrong'}
    with pytest.raises(ValueError):
        smoke._backup_report(invalid, restored=restored)
