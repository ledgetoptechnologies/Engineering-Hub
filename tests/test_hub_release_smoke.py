"""Release helper planning tests; actual Docker acceptance runs in Hub CI."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import json
import pytest


def load(monkeypatch):
    scripts = Path(__file__).parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('release_smoke_under_test', scripts / 'hub_release_smoke.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('image,commit', [
    ('busybox:latest', 'a' * 40),
    ('ghcr.io/ledgetoptechnologies/internal-mcp:stable', 'a' * 40),
    ('ghcr.io/ledgetoptechnologies/engineering-hub-control:latest', 'a' * 40),
    ('ghcr.io/ledgetoptechnologies/engineering-hub-control:build-test', 'not-a-sha'),
])
def test_rejects_out_of_scope_artifact_before_docker(monkeypatch, image, commit):
    module = load(monkeypatch)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: pytest.fail('Docker must not run'))
    with pytest.raises(ValueError):
        module.smoke(image, commit)


def test_isolated_568_profile_and_owned_recreation_plan(monkeypatch):
    module = load(monkeypatch)
    calls = []
    owner = 'b' * 32
    commit = 'a' * 40
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=owner))
    containers, volumes = set(), set()
    def fake(args, **kwargs):
        args = list(args)
        calls.append(args)
        stdout = ''
        if args[1] == 'run':
            containers.add(args[args.index('--name') + 1])
        if args[1:3] == ['volume', 'create']:
            volumes.add(args[-1])
        if args[1:3] in (['container', 'inspect'], ['volume', 'inspect']):
            objects = containers if args[1] == 'container' else volumes
            if args[-1] not in objects:
                return SimpleNamespace(returncode=1, stdout='', stderr='No such ' + args[1] + ': ' + args[-1])
            stdout = owner
        if args[1:3] == ['container', 'rm']:
            containers.discard(args[-1])
        if args[1:3] == ['volume', 'rm']:
            volumes.discard(args[-1])
        if args[1] == 'exec' and 'image_smoke' in args[-2]:
            stdout = json.dumps({'image_smoke': 'passed', 'build_commit': commit})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
    monkeypatch.setattr(module.subprocess, 'run', fake)
    # This test covers the existing normal-startup plan. Standby is tested
    # independently below; real Docker acceptance is the CI smoke itself.
    monkeypatch.setattr(module, 'standby_pid1_acceptance', lambda *args: {'mock_only': True})
    result = module.smoke('ghcr.io/ledgetoptechnologies/engineering-hub-control:build-test', commit)
    runs = [c for c in calls if c[1] == 'run']
    assert len(runs) == 2
    for c in runs:
        assert c[c.index('--user') + 1] == '568:568'
        assert c[c.index('--network') + 1] == 'none'
        assert c[c.index('--pull') + 1] == 'never'
        assert not {'-p', '--privileged', '--device', '--entrypoint'} & set(c)
        assert 'target=/var/lib/hub' in c[c.index('--mount') + 1]
    probes = [c for c in calls if c[1] == 'exec' and 'image_smoke' in c[-2]]
    assert len(probes) == 2 and all("'Host': expected_host" in c[-2] for c in probes)
    assert result['named_volume_recreation'] == 'passed'
    assert result['truenas'] == 'not_deployed'
    assert not containers and not volumes
    assert calls[-1][1:3] == ['volume', 'inspect']


@pytest.mark.parametrize('kind', ['container', 'volume'])
@pytest.mark.parametrize('code,stdout,stderr', [
    (1, '', 'Docker daemon unavailable'),
    (0, 'another-owner', ''),
])
def test_cleanup_inspection_errors_and_owner_mismatch_fail_closed(monkeypatch, kind, code, stdout, stderr):
    module = load(monkeypatch)
    def fake(args, **kwargs):
        assert args[2] == 'inspect', 'Unverified object must never be removed'
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(module.subprocess, 'run', fake)
    with pytest.raises(RuntimeError):
        module.remove_owned(kind, 'test-owned-fixture', 'expected-owner')


@pytest.mark.parametrize('kind', ['container', 'volume'])
def test_only_explicit_missing_object_is_benign(monkeypatch, kind):
    module = load(monkeypatch)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout='', stderr='Error: No such ' + kind + ': test-owned-fixture'))
    assert module.owned_exists(kind, 'test-owned-fixture', 'expected-owner') is False


@pytest.mark.parametrize('method', ['get', 'rpc'])
def test_probe_sets_canonical_host_on_each_request_not_opener_defaults(monkeypatch, method):
    import ast
    import time
    import urllib.request
    import urllib.error
    module = load(monkeypatch)
    class RequestVerified(Exception):
        pass
    class Opener:
        def open(self, request, **kwargs):
            assert isinstance(request, urllib.request.Request)
            assert request.get_header('Host') == 'hub.ledgetoptechnologies.com'
            assert request.full_url.startswith('http://127.0.0.1:8770/')
            raise RequestVerified
    definitions = [node for node in ast.parse(module.PROBE).body
                   if isinstance(node, ast.FunctionDef) and node.name in ('get', 'rpc')]
    namespace = dict(opener=Opener(), base='http://127.0.0.1:8770',
                     expected_host='hub.ledgetoptechnologies.com', time=time,
                     deadline=time.monotonic() + 20, urllib=__import__('urllib'), json=json)
    exec(compile(ast.Module(body=definitions, type_ignores=[]), '<probe-unit>', 'exec'), namespace)
    with pytest.raises(RequestVerified):
        if method == 'get':
            namespace[method]('/health')
        else:
            namespace[method]('tools/list', {}, 'synthetic-fixture')


@pytest.mark.parametrize('failure', [None, 'forced_exit', 'changed_bytes', 'wrong_generation',
                                    'init_wrapper', 'wrong_pid1'])
def test_standby_owned_pid1_stop_and_raw_preservation(monkeypatch, failure):
    module = load(monkeypatch)
    def observed(ended=False):
        return {'Id': 'other' if ended and failure == 'wrong_generation' else 'own-id',
            'Image': 'same-image', 'State': {'Running': not ended, 'Pid': 0 if ended else 1234,
                'ExitCode': 137 if failure == 'forced_exit' else 0, 'OOMKilled': False},
            'Config': {'User': '568:568', 'Entrypoint': ['python'], 'Cmd': module.STANDBY_ARGS,
                'Labels': {'hub.release-owner': 'owner'}, 'Healthcheck': {'Test': ['NONE']},
                'StopSignal': 'SIGTERM'},
            'HostConfig': {'NetworkMode': 'none', 'RestartPolicy': {'Name': 'no'},
                'PidMode': '', 'Init': failure == 'init_wrapper'}}
    inspections = [observed(), observed(True)]
    logs = [[module.STARTED], [module.STARTED, module.STOPPED]]
    calls, removed, names = [], [], []
    def run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([inspections.pop(0)])
        if args[:2] == ('docker', 'logs'):
            return '\n'.join(json.dumps(item) for item in logs.pop(0))
        if args[:2] == ('docker', 'exec'):
            return json.dumps(['tini'] if failure == 'wrong_pid1' else ['python', *module.STANDBY_ARGS])
        return 'own-id'
    snapshots = iter([{'bytes': 'same'},
                      {'bytes': 'changed' if failure == 'changed_bytes' else 'same'},
                      {'bytes': 'same'}])
    monkeypatch.setattr(module, 'run', run)
    monkeypatch.setattr(module, 'snapshot_volume', lambda *args: next(snapshots))
    monkeypatch.setattr(module, 'remove_owned', lambda *args: removed.append(args))
    if failure:
        with pytest.raises(AssertionError):
            module.standby_pid1_acceptance('exact-image', 'owner', 'volume', names)
        assert names == ['hub-release-owner-standby']  # retained for outer owned cleanup
        assert removed == []
    else:
        result = module.standby_pid1_acceptance('exact-image', 'owner', 'volume', names)
        assert result['maintenance_pid1'] == 'passed'
        assert result['all_writers_stopped'] == 'not_established'
        assert names == [] and len(removed) == 1
        assert calls.count(('docker', 'stop', '--time', '10', 'hub-release-owner-standby')) == 1
    creation = calls[0]
    assert '--no-healthcheck' in creation and '--restart=no' in creation
    assert creation[creation.index('--network') + 1] == 'none'
    assert creation[creation.index('--user') + 1] == '568:568'
    assert creation[creation.index('--entrypoint') + 1] == 'python'


def test_raw_snapshot_has_no_database_import(monkeypatch):
    import ast
    module = load(monkeypatch)
    tree = ast.parse(module.RAW_SNAPSHOT)
    imported = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names]
    assert 'sqlite3' not in imported
    assert 'connect(' not in module.RAW_SNAPSHOT
    compile(tree, '<raw-fixture-snapshot>', 'exec')
