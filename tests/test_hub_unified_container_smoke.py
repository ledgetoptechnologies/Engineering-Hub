"""Planning contracts only; actual container cutover is an explicit separate run."""
import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


IMAGE = 'ghcr.io/ledgetoptechnologies/engineering-hub-control:build-unified-test'
COMMIT = 'a' * 40
IMAGE_ID = 'sha256:' + 'c' * 64


def load(monkeypatch):
    scripts = Path(__file__).parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('unified_container_under_test', scripts / 'hub_unified_container_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('image,commit', [
    ('busybox:latest', COMMIT),
    ('ghcr.io/ledgetoptechnologies/internal-mcp:stable', COMMIT),
    ('ghcr.io/ledgetoptechnologies/engineering-hub-control:latest', COMMIT),
    (IMAGE, 'not-a-commit'),
    (IMAGE + '\n', COMMIT),
    ('ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:' + 'g' * 64, COMMIT),
])
def test_invalid_artifact_never_calls_docker(monkeypatch, image, commit):
    module = load(monkeypatch)
    monkeypatch.setattr(module, 'run', lambda *a, **kw: pytest.fail('No Docker action authorized'))
    with pytest.raises(ValueError):
        module.smoke(image, commit)


def test_synthetic_three_stage_plan_profile_graceful_cleanup_and_no_live_environment(monkeypatch):
    module = load(monkeypatch)
    owner = 'b' * 32
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=owner))
    monkeypatch.setenv('CODEX_TOKEN', 'sentinel-live-secret-never-forward')
    calls, containers, volumes = [], {}, set()

    def run(*args, **kwargs):
        calls.append(args)
        if args[1:3] == ('image', 'inspect'):
            return IMAGE_ID
        elif args[1:3] == ('volume', 'create'):
            volumes.add(args[-1])
        elif args[1] == 'run':
            containers[args[args.index('--name') + 1]] = True
        elif args[1:3] == ('container', 'inspect'):
            return str(containers[args[-1]]).lower()
        elif args[1] == 'kill':
            assert args[2:4] == ('--signal', 'SIGTERM')
            containers[args[-1]] = False
        elif args[1:3] == ('container', 'rm'):
            assert containers[args[-1]] is False
            assert '-f' not in args and '--force' not in args
            del containers[args[-1]]
        elif args[1:3] == ('volume', 'rm'):
            assert not containers
            volumes.remove(args[-1])
        elif args[1] == 'exec':
            assert kwargs['timeout'] == 55
            stage = args[-2]
            return json.dumps({'stage': stage, 'preservation': 'passed', 'tools': 24 if stage == 'seed' else 34,
                               'agent_ports': [8770] if stage == 'seed' else [8770,8765],
                               'shared_session_across_agent_ports': 'not_applicable' if stage == 'seed' else 'passed',
                               'native_notification_route': 'not_applicable' if stage == 'seed' else 'alias_to_primary'})
        return ''

    def exists(kind, name, actual_owner):
        assert actual_owner == owner
        return name in (containers if kind == 'container' else volumes)

    monkeypatch.setattr(module, 'run', run)
    monkeypatch.setattr(module, 'owned_exists', exists)
    report = module.smoke(IMAGE, COMMIT)
    assert not containers and not volumes
    assert report['synthetic_legacy_cutover'] == 'passed'
    assert report['resolved_image_id'] == IMAGE_ID
    assert report['compatibility_alias'] == {'agent_ports': [8770,8765], 'shared_stateful_session': 'passed',
                                              'native_notification_route': 'alias_to_primary'}
    assert report['production_state'] == 'never_opened'
    assert report['legacy_version'] == 'candidate_bus_not_deployed_old_image'
    assert report['truenas'] == 'not_deployed' and report['reboot'] == report['agent_wake'] == 'not_tested'
    runs = [call for call in calls if call[1] == 'run']
    assert len(runs) == 3
    mounted = set()
    for call in runs:
        for option, value in (('--network', 'none'), ('--pull', 'never'), ('--user', '568:568'),
                              ('--cap-drop', 'ALL'), ('--memory', '512m'), ('--memory-swap', '512m'),
                              ('--cpus', '1'), ('--pids-limit', '128'), ('--security-opt', 'no-new-privileges:true')):
            assert call[call.index(option) + 1] == value
        assert '--read-only' in call
        assert not {'--privileged', '--device', '-v', '-p', '--publish', '--env-file'} & set(call)
        assert call[call.index('--entrypoint') + 1] == 'python'
        mounted.add(call[call.index('--mount') + 1])
        assert call.count('--mount') == 1
        assert IMAGE_ID in call and IMAGE not in call
        assert 'docker.sock' not in str(call) and 'sentinel-live-secret-never-forward' not in str(call)
        env = dict(call[index + 1].split('=', 1) for index, value in enumerate(call) if value == '-e')
        assert env['AGENT_BUS_DB'] != env['HUB_DB']
        assert all(env[key].startswith('synthetic-unified-') for key in ('HERMES_TOKEN', 'CODEX_TOKEN', 'CLAUDE_TOKEN'))
    assert len(mounted) == 1
    assert 'agent_message_bus.app:app' in runs[0]
    assert '--agent-alias-port' not in runs[0]
    assert all('--split-ingress' in call and '--unified-mcp' in call for call in runs[1:])
    assert all(call[call.index('--agent-alias-port') + 1] == '8765' for call in runs[1:])
    assert len([call for call in calls if call[1] == 'kill']) == 3
    removals = [call for call in calls if call[1:3] == ('container', 'rm')]
    assert len(removals) == 3 and all(len(call) == 4 for call in removals)
    assert len([call for call in calls if call[1:3] == ('image', 'inspect')]) == 1


def test_ownership_failure_never_stops_or_removes(monkeypatch):
    module = load(monkeypatch)
    def denied(*args):
        raise RuntimeError('Unowned fixture')
    monkeypatch.setattr(module, 'owned_exists', denied)
    monkeypatch.setattr(module, 'run', lambda *a, **kw: pytest.fail('No stop allowed'))
    with pytest.raises(RuntimeError):
        module.stop_owned('specific-fixture', 'expected-owner')


def test_drain_timeout_retains_container_and_volume(monkeypatch):
    module = load(monkeypatch)
    monkeypatch.setattr(module, 'owned_exists', lambda *args: True)
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        return 'true' if args[1:3] == ('container', 'inspect') else ''
    monkeypatch.setattr(module, 'run', run)
    clock = iter([0, 36])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(clock))
    with pytest.raises(RuntimeError, match='retained without force-removal'):
        module.stop_owned('specific-fixture', 'owner')
    assert all('-f' not in call and 'SIGKILL' not in call for call in calls)
    assert not any(call[1:3] == ('container', 'rm') for call in calls)


@pytest.mark.parametrize('identity', ['', 'mutable:tag', 'sha256:' + 'x' * 64])
def test_unresolved_immutable_identity_fails_before_volume_creation(monkeypatch, identity):
    module = load(monkeypatch)
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        assert args[1:3] == ('image', 'inspect')
        return identity
    monkeypatch.setattr(module, 'run', run)
    with pytest.raises(RuntimeError, match='immutable image identity'):
        module.smoke(IMAGE, COMMIT)
    assert len(calls) == 1


def test_probe_compiles_and_records_all_tables_files_audit_before_canaries(monkeypatch):
    module = load(monkeypatch)
    parsed = ast.parse(module.PROBE)
    assert parsed
    assert 'SELECT name FROM sqlite_master' in module.PROBE
    assert 'audit_bytes' in module.PROBE and 'audit_sha256' in module.PROBE
    assert 'mode=ro' in module.PROBE
    assert module.PROBE.index("assert snapshot() == saved['snapshot']") < module.PROBE.index("async with client('hermes', endpoint=hermes_endpoint)")
    assert 'get_send_receipt' in module.PROBE and 'file_history' in module.PROBE
    assert 'subscribe_resource' in module.PROBE and "events.get(),3" in module.PROBE
    assert 'expected' in module.PROBE and "'acknowledged'" in module.PROBE
    assert "alias_health['build_commit'] == health['build_commit'] == commit" in module.PROBE
    assert "alias_tools = await http.post(alias_base+'/mcp',headers=same_session_headers,json=rpc)" in module.PROBE
    assert "hermes_endpoint = base if stage == 'seed' else alias_base" in module.PROBE
    assert "client('codex')" in module.PROBE
