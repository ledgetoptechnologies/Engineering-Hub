"""Offline publication boundary checks; no registry, Docker or credential use."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import types
import builtins
import inspect
import io

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import hub_executor_image_smoke as SMOKE
SPEC = importlib.util.spec_from_file_location('executor_publication', ROOT / 'scripts/publish_hub_executor.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
CONFIG = 'sha256:' + 'd' * 64
PRIVATE_PACKAGE_CHECK = P.private_package
INSTALLED_INGRESS_CHECK = P.installed_ingress
ABSENCE_CHECK = P.absent


def environment():
    return dict(SOURCE_COMMIT='a' * 40, CORE_COMMIT='a' * 40, CORE_DIGEST='b' * 64,
                CORE_CONFIG='c' * 64, WHEEL_SHA256='e' * 64, SOURCE_DATE_EPOCH='1792972800',
                GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch',
                GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1')


@pytest.mark.parametrize('key,value', [('GITHUB_REF', 'refs/heads/dev'),
    ('GITHUB_EVENT_NAME', 'push'), ('GITHUB_RUN_ID', '123;echo secret'),
    ('GITHUB_RUN_ATTEMPT', '0'), ('GITHUB_RUN_ID', '1' * 21), ('WHEEL_SHA256', 'latest')])
def test_rejects_nonmanual_or_mutable_invocation(key, value):
    env = environment()
    env[key] = value
    with pytest.raises(ValueError):
        P.invocation(env)


def test_fixed_intent_destination():
    _, local, target = P.invocation(environment())
    assert local == 'engineering-hub-executor:build-compatibility-123-1'
    assert target == P.PACKAGE + ':staging-executor-123-1'


def registry_access(monkeypatch, changes=None, core_bytes=None):
    env = environment()
    env.update(GITHUB_ACTOR='fixture-actor', GH_TOKEN='job-token-fixture')
    core = (core_bytes if core_bytes is not None else
            json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                        'config': {'digest': 'sha256:' + env['CORE_CONFIG']}}).encode())
    env['CORE_DIGEST'] = hashlib.sha256(core).hexdigest()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    values, _, target = P.invocation(env)
    urls = [P.REGISTRY_TOKEN, P.REGISTRY_MANIFEST + 'sha256:' + values['CORE_DIGEST'],
            P.REGISTRY_MANIFEST + 'staging-executor-123-1']
    responses = [(200, b'{"token":"pull-only-fixture"}'), (200, core),
                 (404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')]
    for index, response in (changes or {}).items():
        responses[index] = response
    calls = []
    class Response:
        def __init__(self, status, raw, url):
            self.status, self.raw, self.url = status, raw, url
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def geturl(self):
            return self.url
        def read(self, size):
            assert size == (1048577 if len(calls) == 2 else 65537)
            return self.raw
    class Opener:
        def open(self, request, timeout):
            index = len(calls)
            assert request.full_url == urls[index] and timeout == 20
            auth = request.get_header('Authorization')
            if index == 0:
                assert auth == 'Basic ' + P.base64.b64encode(b'fixture-actor:job-token-fixture').decode()
                assert request.full_url.endswith(':pull') and ':push' not in request.full_url
            else:
                assert auth == 'Bearer pull-only-fixture'
            calls.append(request.full_url)
            response = responses[index]
            if isinstance(response, Exception):
                raise response
            status, raw = response
            if status != 200:
                raise P.urllib.error.HTTPError(request.full_url, status, 'private diagnostic', {}, io.BytesIO(raw))
            return Response(status, raw, request.full_url)
    def opener(*handlers):
        assert handlers[0].proxies == {} and isinstance(handlers[1], P.NoRedirect)
        return Opener()
    monkeypatch.setattr(P.urllib.request, 'build_opener', opener)
    return values, target, calls


def test_authenticated_absence_checks_known_core_first_with_same_pull_token(monkeypatch):
    values, target, calls = registry_access(monkeypatch)
    evidence = {}
    P.absent(target, values, evidence)
    assert calls == [P.REGISTRY_TOKEN, P.REGISTRY_MANIFEST + 'sha256:' + values['CORE_DIGEST'],
                     P.REGISTRY_MANIFEST + 'staging-executor-123-1']
    assert evidence == {'registry_absence_phase': 'complete', 'registry_absence_http_status': 404,
                        'registry_private_core_verified': True}


@pytest.mark.parametrize('stage,status,raw', [
    (0, 401, b'{"secret":"denied"}'), (0, 403, b'private'), (0, 302, b'redirect'),
    (0, 200, b'{}'), (0, 200, b'x' * 65537),
    (1, 401, b'private'), (1, 403, b'private'), (1, 404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}'),
    (1, 200, b'{}'), (1, 200, b'x' * 1048577),
    (2, 200, b'{}'), (2, 401, b'private'), (2, 403, b'private'),
    (2, 404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}'),
    (2, 404, b'{"errors":"MANIFEST_UNKNOWN"}'),
    (2, 404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"},{"code":"DENIED"}]}'),
    (2, 404, b'not-json')], ids=['token401', 'token403', 'token-redirect', 'missing-token',
    'oversize-token', 'core401', 'core403', 'core-missing', 'core-hash-mismatch',
    'oversize-core', 'tag-exists', 'tag401', 'tag403', 'name-unknown', 'bad-errors-type',
    'multiple-errors', 'bad-json'])
def test_unknown_registry_outcomes_fail_closed_without_leaking(monkeypatch, stage, status, raw):
    values, target, calls = registry_access(monkeypatch, {stage: (status, raw)})
    with pytest.raises(RuntimeError) as failure:
        P.absent(target, values)
    assert len(calls) == stage + 1
    assert str(failure.value) == 'Authenticated registry absence not established; refusing upload'


def test_known_core_config_mismatch_prevents_tag_check(monkeypatch):
    values, target, calls = registry_access(monkeypatch)
    values['CORE_CONFIG'] = '0' * 64
    monkeypatch.setenv('CORE_CONFIG', values['CORE_CONFIG'])
    with pytest.raises(RuntimeError):
        P.absent(target, values)
    assert len(calls) == 2


@pytest.mark.parametrize('stage,body', [
    (0, b'{"token":"denied","token":"pull-only-fixture"}'),
    (0, b'{"token":"pull-only-fixture","expires_in":1,"expires_in":1}'),
    (2, b'{"errors":[{"code":"DENIED"}],"errors":[{"code":"MANIFEST_UNKNOWN"}]}'),
    (2, b'{"errors":[{"code":"DENIED","code":"MANIFEST_UNKNOWN"}]}')],
    ids=['duplicate-token', 'duplicate-token-metadata', 'duplicate-errors', 'duplicate-code'])
def test_duplicate_registry_keys_never_authorize_absence(monkeypatch, stage, body):
    values, target, calls = registry_access(monkeypatch, {stage: (200 if stage == 0 else 404, body)})
    with pytest.raises(RuntimeError, match='refusing upload'):
        P.absent(target, values)
    assert len(calls) == stage + 1


def test_duplicate_core_keys_rejected_even_with_exact_approved_raw_hash(monkeypatch):
    core = ('{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",'
            '"config":{"digest":"sha256:' + 'c' * 64 + '"},'
            '"config":{"digest":"sha256:' + 'c' * 64 + '"}}').encode()
    values, target, calls = registry_access(monkeypatch, core_bytes=core)
    assert hashlib.sha256(core).hexdigest() == values['CORE_DIGEST']
    with pytest.raises(RuntimeError, match='refusing upload'):
        P.absent(target, values)
    assert len(calls) == 2


@pytest.mark.parametrize('phase,index', [('pull_token', 0), ('known_private_core', 1), ('intent_tag', 2)])
def test_timeout_status_never_borrows_previous_phase_200(monkeypatch, phase, index):
    values, target, calls = registry_access(monkeypatch, {index: TimeoutError('private diagnostic')})
    evidence = {'registry_absence_http_status': 200, 'registry_private_core_verified': True}
    with pytest.raises(RuntimeError, match='refusing upload'):
        P.absent(target, values, evidence)
    assert len(calls) == index + 1
    assert evidence['registry_absence_phase'] == phase
    assert 'registry_absence_http_status' not in evidence
    assert evidence.get('registry_private_core_verified', False) is (index == 2)


def test_registry_timeout_is_sanitized_and_not_retried(monkeypatch):
    values, target, calls = registry_access(monkeypatch)
    class Unavailable:
        def open(self, *args, **kwargs):
            calls.append('attempt')
            raise TimeoutError('private token diagnostic')
    monkeypatch.setattr(P.urllib.request, 'build_opener', lambda *a: Unavailable())
    evidence = {}
    with pytest.raises(RuntimeError) as failure:
        P.absent(target, values, evidence)
    assert calls == ['attempt'] and evidence == {'registry_absence_phase': 'pull_token'}
    assert 'private' not in str(failure.value)


@pytest.mark.parametrize('case', ['target', 'inputs', 'token', 'actor'])
def test_invalid_registry_identity_fails_before_any_http(monkeypatch, case):
    values, target, calls = registry_access(monkeypatch)
    if case == 'target':
        target = P.PACKAGE + ':latest'
    elif case == 'inputs':
        values['CORE_DIGEST'] = '0' * 64
    elif case == 'token':
        monkeypatch.delenv('GH_TOKEN')
    else:
        monkeypatch.setenv('GITHUB_ACTOR', 'invalid:actor')
    with pytest.raises(RuntimeError):
        P.absent(target, values)
    assert calls == []


def registry(monkeypatch, *, config=CONFIG, descriptor=None, size=123):
    raw = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                      'config': {'digest': config, 'size': 17},
                      'layers': [{'digest': 'sha256:' + 'f' * 64, 'size': size}]}).encode()
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(P, 'run', lambda *a, **k: raw if a[-1] == '--raw'
                        else json.dumps({'digest': descriptor or digest}).encode())
    return digest


def test_registry_raw_hash_config_and_sizes(monkeypatch):
    digest = registry(monkeypatch)
    result = P.registry_manifest('fixture', CONFIG)
    assert result['manifest_digest'] == digest
    assert result['compressed_layer_bytes'] == 123
    assert result['config_bytes'] == 17


@pytest.mark.parametrize('changes', [{'config': 'sha256:' + '0' * 64},
    {'descriptor': 'sha256:' + '0' * 64}, {'size': True}, {'size': -1}, {'size': 2**64}])
def test_registry_mismatch_rejected(monkeypatch, changes):
    registry(monkeypatch, **changes)
    with pytest.raises(ValueError):
        P.registry_manifest('fixture', CONFIG)


@pytest.mark.parametrize('field,value', [('Id', 'sha256:' + '0' * 64),
    ('Os', 'windows'), ('Architecture', 'arm64'), ('Size', True), ('Size', 0)])
def test_local_identity_or_size_mismatch_rejected(monkeypatch, field, value):
    info = {'Id': CONFIG, 'Os': 'linux', 'Architecture': 'amd64', 'Size': 999}
    info[field] = value
    monkeypatch.setattr(P, 'verify_defaults', lambda info: None)
    monkeypatch.setattr(P, 'run', lambda *a, **k: json.dumps([info]).encode())
    with pytest.raises(ValueError):
        P.image('fixture', CONFIG)


@pytest.mark.parametrize('change', [{'visibility': 'public'}, {'name': 'other'},
    {'owner': {'login': 'other'}}, {'package_type': 'npm'}, {'owner': {}}, {}])
def test_private_package_metadata_fails_closed(monkeypatch, change):
    package = {'visibility': 'private', 'name': 'engineering-hub-control',
               'package_type': 'container', 'owner': {'login': 'LedgeTopTechnologies'}}
    package.update(change)
    if not change:
        package = {}
    package_response(monkeypatch, json.dumps(package).encode())
    with pytest.raises(RuntimeError, match='refusing upload'):
        P.private_package()


def package_response(monkeypatch, raw, *, url=P.PACKAGE_API, status=200):
    monkeypatch.setenv('GH_TOKEN', 'job-token-fixture')
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def geturl(self):
            return url
        def read(self, limit):
            assert limit == 65537
            return raw
    Response.status = status
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == P.PACKAGE_API and timeout == 20
            assert request.get_header('Authorization') == 'Bearer job-token-fixture'
            return Response()
    def build(*handlers):
        assert isinstance(handlers[1], P.NoRedirect)
        assert handlers[1].redirect_request(None, None, 302, None, None, 'https://other.invalid') is None
        assert handlers[0].proxies == {}
        return Opener()
    monkeypatch.setattr(P.urllib.request, 'build_opener', build)


def test_private_package_exact_bounded_read(monkeypatch):
    package_response(monkeypatch, json.dumps({'visibility': 'private', 'name': 'engineering-hub-control',
                     'package_type': 'container', 'owner': {'login': 'LedgeTopTechnologies'}}).encode())
    P.private_package()


@pytest.mark.parametrize('raw,url,status', [(b'not-json-secret', P.PACKAGE_API, 200),
    (b'x' * 65537, P.PACKAGE_API, 200), (b'{}', 'https://other.invalid', 200),
    (b'{}', P.PACKAGE_API, 404)], ids=['invalid-json', 'oversize', 'redirect', 'missing'])
def test_private_package_unknown_or_redirect_does_not_leak(monkeypatch, raw, url, status):
    package_response(monkeypatch, raw, url=url, status=status)
    with pytest.raises(RuntimeError) as failure:
        P.private_package()
    assert 'secret' not in str(failure.value) and 'job-token-fixture' not in str(failure.value)


def execute_ingress_probe(expected, *, actual=None, seam=True):
    def supported(*, access_verifier=None):
        pass
    def missing():
        pass
    modules = {'inspect': inspect,
        'os': types.SimpleNamespace(getuid=lambda: 10001, geteuid=lambda: 10001,
                                    getgid=lambda: 10001, getegid=lambda: 10001, getpid=lambda: 1),
        'sys': types.SimpleNamespace(argv=['probe', expected], flags=types.SimpleNamespace(isolated=True, dont_write_bytecode=True)),
        'engineering_hub.build_info': types.SimpleNamespace(read_build_commit=lambda: actual or expected),
        'engineering_hub.frontends': types.SimpleNamespace(HubFrontends=supported if seam else missing),
        'engineering_hub.ingress': types.SimpleNamespace(HubIngressMiddleware=supported),
        'engineering_hub.private_gateway': types.SimpleNamespace(PrivateBrowserGateway=supported),
        'engineering_hub.queued_pilot_service': types.SimpleNamespace(serve_owned_queued_pilot=supported)}
    emitted = []
    scope = dict(vars(builtins))
    scope.update(__import__=lambda name, *args, **kwargs: modules[name], print=lambda value: emitted.append(value))
    exec(P.INGRESS_PROBE, {'__builtins__': scope})
    assert emitted == ['installed-ingress-seam-passed']


@pytest.mark.parametrize('actual,seam', [('b' * 40, True), (None, False)])
def test_actual_probe_body_rejects_wrong_commit_or_missing_seam(actual, seam):
    with pytest.raises((AssertionError, KeyError)):
        execute_ingress_probe('a' * 40, actual=actual, seam=seam)


def test_actual_probe_body_checks_signatures_without_construction():
    execute_ingress_probe('a' * 40)


def terminal_probe(owner='f' * 32):
    return {'Config': {'Labels': {P.OWNER: owner}, 'User': '10001:10001'},
            'Image': CONFIG, 'Mounts': [], 'State': {'Running': False, 'ExitCode': 0},
            'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                'SecurityOpt': ['no-new-privileges:true'], 'Memory': 256 * 1024**2,
                'MemorySwap': 256 * 1024**2, 'NanoCpus': 1_000_000_000, 'PidsLimit': 64}}


def probe_commands(monkeypatch, info=None):
    calls, removed = [], []
    monkeypatch.setattr(P.uuid, 'uuid4', lambda: types.SimpleNamespace(hex='f' * 32))
    monkeypatch.setattr(P, 'remove_owned', lambda *a: removed.append(a))
    def command(*args, **kwargs):
        calls.append((args, kwargs))
        if args[2] == 'start':
            return b'installed-ingress-seam-passed\n'
        if args[2] == 'inspect':
            return json.dumps([info or terminal_probe()]).encode()
        return b'created'
    monkeypatch.setattr(P, 'run', command)
    return calls, removed


def test_installed_probe_exact_id_bounded_and_rechecked(monkeypatch):
    inspections, calls = [], []
    monkeypatch.setattr(P, 'image', lambda *a: inspections.append(a))
    calls, removed = probe_commands(monkeypatch)
    evidence = {}
    P.installed_ingress(CONFIG, 'a' * 40, evidence)
    assert inspections == [(CONFIG, CONFIG), (CONFIG, CONFIG)]
    command, kwargs = calls[0]
    for flag, value in [('--network', 'none'), ('--user', '10001:10001'), ('--cpus', '1'),
                        ('--memory', '256m'), ('--memory-swap', '256m'), ('--pids-limit', '64'),
                        ('--cap-drop', 'ALL'), ('--security-opt', 'no-new-privileges:true')]:
        assert command[command.index(flag) + 1] == value
    assert '--read-only' in command and command[:3] == ('docker', 'container', 'create')
    assert command[-6:] == (CONFIG, '-I', '-B', '-c', P.INGRESS_PROBE, 'a' * 40)
    assert kwargs == {'timeout': 60}
    assert removed == [('hub-executor-ingress-' + 'f' * 32, 'f' * 32)]
    assert evidence['ingress_probe_cleanup'] == 'removed'


def test_installed_probe_nonexact_output_rejected(monkeypatch):
    monkeypatch.setattr(P, 'image', lambda *a: {})
    monkeypatch.setattr(P, 'remove_owned', lambda *a: None)
    monkeypatch.setattr(P, 'run', lambda *a, **k: b'private output')
    with pytest.raises(ValueError, match='not verified'):
        P.installed_ingress(CONFIG, 'a' * 40)


@pytest.mark.parametrize('field,value', [('Image', 'sha256:' + '0' * 64),
    ('Memory', 0), ('NetworkMode', 'host'), ('Privileged', True), ('PidsLimit', -1)])
def test_terminal_probe_identity_and_resource_mismatch_rejected(monkeypatch, field, value):
    info = terminal_probe()
    if field == 'Image':
        info[field] = value
    else:
        info['HostConfig'][field] = value
    monkeypatch.setattr(P, 'image', lambda *a: {})
    _, removed = probe_commands(monkeypatch, info)
    evidence = {}
    with pytest.raises(ValueError, match='identity or limits'):
        P.installed_ingress(CONFIG, 'a' * 40, evidence)
    assert len(removed) == 1 and evidence['ingress_probe_cleanup'] == 'removed'


@pytest.mark.parametrize('wrong_owner', [False, True])
def test_attach_timeout_cleans_only_verified_owned_locator(monkeypatch, wrong_owner):
    monkeypatch.setattr(SMOKE, 'docker_command', lambda a: (['docker', *a], {}))
    calls, cleaned = [], []
    monkeypatch.setattr(P.uuid, 'uuid4', lambda: types.SimpleNamespace(hex='f' * 32))
    monkeypatch.setattr(P, 'image', lambda *a: {})
    def command(*args, **kwargs):
        calls.append(args)
        if args[2] == 'start':
            raise subprocess.TimeoutExpired(['private argv'], 60, output=b'private')
        return b'created'
    monkeypatch.setattr(P, 'run', command)
    monkeypatch.setattr(SMOKE.subprocess, 'run', lambda *a, **k:
        subprocess.CompletedProcess(a, 0, json.dumps([terminal_probe('wrong' if wrong_owner else 'f' * 32)]), ''))
    monkeypatch.setattr(SMOKE, 'run', lambda *a, **k: cleaned.append(a))
    evidence = {}
    with pytest.raises(RuntimeError if wrong_owner else subprocess.TimeoutExpired):
        P.installed_ingress(CONFIG, 'a' * 40, evidence)
    assert [a[2] for a in calls] == ['create', 'start']
    assert evidence['ingress_probe_container'] == 'hub-executor-ingress-' + 'f' * 32
    assert evidence['ingress_probe_cleanup'] == ('held' if wrong_owner else 'removed')
    assert cleaned == ([] if wrong_owner else [('docker', 'container', 'rm', '-f', 'hub-executor-ingress-' + 'f' * 32)])


@pytest.fixture
def candidate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    values, local, target = P.invocation(environment())
    identity = {'socket': 'unix:///run/test/engine.sock', 'device': 1, 'inode': 2, 'daemon_id': 'daemon'}
    monkeypatch.setattr(P.ci, 'engine_identity', lambda: identity)
    receipt = {'state': 'passed', 'inputs': values, 'image_published': False,
               'compatibility': {'core_config_digest': 'sha256:' + values['CORE_CONFIG'],
                                 'core_commit': values['CORE_COMMIT'],
                                 'installed_core_identity': 'passed',
                                 'engine_identity': identity,
                                 'executor_imports': {'image_id': CONFIG}}}
    Path('executor-compatibility-receipt.json').write_text(json.dumps(receipt))
    monkeypatch.setattr(P, 'verify_wheel', lambda **k: {})
    monkeypatch.setattr(P, 'image', lambda *a: {'Id': CONFIG, 'Size': 999, 'RepoDigests': []})
    monkeypatch.setattr(P, 'smoke', lambda *a: {'image_id': CONFIG})
    monkeypatch.setattr(P, 'installed_ingress', lambda *a: None)
    monkeypatch.setattr(P, 'private_package', lambda: None)
    return values, local, target, {'stage': 'start', 'publication_attempted': False, 'image_published': False}


def test_wrong_smoke_image_never_pushes(monkeypatch, candidate):
    calls = []
    monkeypatch.setattr(P, 'smoke', lambda *a: {'image_id': 'sha256:' + '0' * 64})
    monkeypatch.setattr(P, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match='Smoke image'):
        P.publish(*candidate)
    assert calls == []


def test_existing_tag_never_pushes(monkeypatch, candidate):
    calls = []
    def existing(target, values, evidence):
        raise ValueError('Publication intent tag already exists')
    monkeypatch.setattr(P, 'absent', existing)
    monkeypatch.setattr(P, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match='already exists'):
        P.publish(*candidate)
    assert calls == []


def test_real_authenticated_absence_failure_never_pushes(monkeypatch, candidate):
    values, target, reads = registry_access(monkeypatch, {2: (403, b'private diagnostic')})
    receipt = json.loads(Path('executor-compatibility-receipt.json').read_text())
    receipt['inputs'] = values
    Path('executor-compatibility-receipt.json').write_text(json.dumps(receipt))
    monkeypatch.setattr(P, 'absent', ABSENCE_CHECK)
    commands = []
    monkeypatch.setattr(P, 'run', lambda *a, **k: commands.append(a))
    record = candidate[-1]
    with pytest.raises(RuntimeError, match='refusing upload'):
        P.publish(values, candidate[1], target, record)
    assert len(reads) == 3 and commands == []
    assert record['publication_attempted'] is False and record['image_published'] is False
    assert record['registry_private_core_verified'] is True
    assert record['registry_absence_phase'] == 'intent_tag'
    assert record['registry_absence_http_status'] == 403
    assert 'authenticated_intent_absence_verified' not in record


def test_push_failure_is_persisted_unknown_and_never_retried(monkeypatch, candidate):
    calls = []
    monkeypatch.setattr(P, 'absent', lambda *a: None)
    def fail(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('docker', 'push'):
            persisted = json.loads(P.RECEIPT.read_text())
            assert persisted['state'] == 'push_outcome_unknown'
            assert persisted['publication_attempted'] is True
            assert persisted['image_published'] is None
            raise P.ci.CommandFailure(1)
        return b''
    monkeypatch.setattr(P, 'run', fail)
    with pytest.raises(P.ci.CommandFailure):
        P.publish(*candidate)
    assert sum(a[:2] == ('docker', 'push') for a in calls) == 1


def test_success_binds_receipt_to_exact_pulled_config_and_digest(monkeypatch, candidate):
    calls, smokes = [], []
    digest = 'sha256:' + '1' * 64
    immutable = P.PACKAGE + '@' + digest
    monkeypatch.setattr(P, 'absent', lambda *a: None)
    monkeypatch.setattr(P, 'run', lambda *a, **k: calls.append(a) or b'')
    monkeypatch.setattr(P, 'image', lambda *a: {'Id': CONFIG, 'Size': 999, 'RepoDigests': [immutable]})
    monkeypatch.setattr(P, 'smoke', lambda *a: smokes.append(a) or {'image_id': CONFIG})
    reads = []
    def manifest(ref, config):
        reads.append((ref, config))
        return {'manifest_digest': digest, 'manifest_bytes': 100, 'compressed_layer_bytes': 123,
                'config_bytes': 17}
    monkeypatch.setattr(P, 'registry_manifest', manifest)
    P.publish(*candidate)
    values, local, target, record = candidate
    assert reads == [(target, CONFIG), (immutable, CONFIG)]
    assert calls == [('docker', 'tag', CONFIG, target), ('docker', 'push', target),
                     ('docker', 'pull', '--platform', 'linux/amd64', immutable),
                     ('docker', 'tag', immutable, 'engineering-hub-executor:build-publication-verified-123-1')]
    assert smokes == [(local, values['SOURCE_COMMIT']),
                      ('engineering-hub-executor:build-publication-verified-123-1', values['SOURCE_COMMIT'])]
    assert record['state'] == 'published_verified'
    assert record['config_digest'] == CONFIG and record['manifest_digest'] == digest
    assert record['image_published'] is True and record['staging_acceptance'] == 'not_tested'
    assert record['local_image_size_estimate_bytes'] == 999
    assert record['private_package_verified'] is True and record['installed_ingress_seam'] == 'passed'


@pytest.mark.parametrize('guard', ['private_package', 'installed_ingress'])
def test_guard_failure_stops_before_any_upload_and_no_success_fact(monkeypatch, candidate, guard):
    calls = []
    def reject(*args):
        raise RuntimeError('private diagnostic')
    monkeypatch.setattr(P, guard, reject)
    monkeypatch.setattr(P, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError):
        P.publish(*candidate)
    assert calls == []
    assert candidate[-1]['publication_attempted'] is False
    assert ('private_package_verified' if guard == 'private_package' else 'installed_ingress_seam') not in candidate[-1]


@pytest.mark.parametrize('package', [{'visibility': 'public'}, {},
    {'name': 'wrong-package'}, None], ids=['public', 'missing', 'wrong', 'unknown'])
def test_real_metadata_guard_stops_before_push(monkeypatch, candidate, package):
    calls = []
    monkeypatch.setattr(P, 'private_package', PRIVATE_PACKAGE_CHECK)
    package_response(monkeypatch, json.dumps(package).encode())
    monkeypatch.setattr(P, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match='refusing upload'):
        P.publish(*candidate)
    assert calls == [] and candidate[-1]['publication_attempted'] is False
    assert 'private_package_verified' not in candidate[-1]


@pytest.mark.parametrize('actual,seam', [('b' * 40, True), (None, False)])
def test_actual_probe_validation_blocks_publication(monkeypatch, candidate, actual, seam):
    calls = []
    monkeypatch.setattr(P, 'installed_ingress', lambda expected, commit, evidence:
                        execute_ingress_probe(commit, actual=actual, seam=seam))
    monkeypatch.setattr(P, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises((AssertionError, KeyError)):
        P.publish(*candidate)
    assert calls == [] and candidate[-1]['publication_attempted'] is False
    assert 'installed_ingress_seam' not in candidate[-1]


def test_attach_timeout_cleans_and_blocks_publication(monkeypatch, candidate):
    calls, cleaned = [], []
    monkeypatch.setattr(P, 'installed_ingress', INSTALLED_INGRESS_CHECK)
    monkeypatch.setattr(P.uuid, 'uuid4', lambda: types.SimpleNamespace(hex='f' * 32))
    def command(*args, **kwargs):
        calls.append(args)
        if args[:3] == ('docker', 'container', 'start'):
            raise subprocess.TimeoutExpired(['private argv'], 60)
        return b'created'
    monkeypatch.setattr(P, 'run', command)
    monkeypatch.setattr(P, 'remove_owned', lambda *a: cleaned.append(a))
    with pytest.raises(subprocess.TimeoutExpired):
        P.publish(*candidate)
    assert all(args[:2] != ('docker', 'push') for args in calls)
    assert cleaned == [('hub-executor-ingress-' + 'f' * 32, 'f' * 32)]
    assert candidate[-1]['ingress_probe_cleanup'] == 'removed'
    assert candidate[-1]['publication_attempted'] is False


def test_readback_failure_retains_unknown_outcome(monkeypatch, candidate):
    monkeypatch.setattr(P, 'absent', lambda *a: None)
    monkeypatch.setattr(P, 'run', lambda *a, **k: b'')
    def unknown(*args):
        raise ValueError('secret registry mismatch')
    monkeypatch.setattr(P, 'registry_manifest', unknown)
    with pytest.raises(ValueError):
        P.publish(*candidate)
    record = candidate[-1]
    assert record['state'] == 'push_outcome_unknown' and record['image_published'] is None


def test_existing_attempt_receipt_refuses_retry_before_publish(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(P.ci, 'git', lambda *a: b'')
    P.RECEIPT.write_text('{}')
    monkeypatch.setattr(P, 'publish', lambda *a: pytest.fail('must not publish'))
    with pytest.raises(RuntimeError, match='refusing retry'):
        P.main()


def test_approved_workflow_is_manual_main_only_and_artifacts_are_json_only():
    source = (ROOT / '.github/workflows/hub-executor-publication.yml').read_text()
    assert "if: ${{ github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' }}" in source
    assert '\n  push:' not in source and '\n  pull_request:' not in source
    assert 'packages: write' in source
    assert 'executor-compatibility-receipt.json' in source
    assert 'executor-publication-receipt.json' in source
    assert 'docker push' not in source  # Only bounded helper can push.


def test_changed_engine_identity_stops_before_publication(monkeypatch, candidate):
    calls = []
    monkeypatch.setattr(P.ci, 'engine_identity', lambda: {'daemon_id': 'different'})
    monkeypatch.setattr(P, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match='engine identity changed'):
        P.publish(*candidate)
    assert calls == []
