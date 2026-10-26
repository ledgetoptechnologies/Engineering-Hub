"""Offline publication boundary checks; no registry, Docker or credential use."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('executor_publication', ROOT / 'scripts/publish_hub_executor.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
CONFIG = 'sha256:' + 'd' * 64


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


@pytest.mark.parametrize('code,stdout,stderr,allowed', [
    (1, b'', b'no such manifest: fixture', True),
    (0, b'{}', b'', False), (1, b'', b'denied: secret', False),
    (1, b'', b'not found', False), (2, b'', b'no such manifest: fixture', False),
    (1, b'private', b'no such manifest: fixture', False)])
def test_only_exact_absence_allows_publication(monkeypatch, code, stdout, stderr, allowed):
    monkeypatch.setattr(P.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a, code, stdout, stderr))
    if allowed:
        P.absent('fixture')
    else:
        with pytest.raises((ValueError, RuntimeError)) as failure:
            P.absent('fixture')
        assert 'secret' not in str(failure.value)
        assert 'private' not in str(failure.value)


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


@pytest.fixture
def candidate(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    values, local, target = P.invocation(environment())
    receipt = {'state': 'passed', 'inputs': values, 'image_published': False,
               'compatibility': {'core_config_digest': 'sha256:' + values['CORE_CONFIG'],
                                 'core_commit': values['CORE_COMMIT'],
                                 'installed_core_identity': 'passed',
                                 'executor_imports': {'image_id': CONFIG}}}
    Path('executor-compatibility-receipt.json').write_text(json.dumps(receipt))
    monkeypatch.setattr(P, 'verify_wheel', lambda **k: {})
    monkeypatch.setattr(P, 'image', lambda *a: {'Id': CONFIG, 'Size': 999, 'RepoDigests': []})
    monkeypatch.setattr(P, 'smoke', lambda *a: {'image_id': CONFIG})
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
    def existing(target):
        raise ValueError('Publication intent tag already exists')
    monkeypatch.setattr(P, 'absent', existing)
    monkeypatch.setattr(P, 'run', lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match='already exists'):
        P.publish(*candidate)
    assert calls == []


def test_push_failure_is_persisted_unknown_and_never_retried(monkeypatch, candidate):
    calls = []
    monkeypatch.setattr(P, 'absent', lambda t: None)
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
    monkeypatch.setattr(P, 'absent', lambda t: None)
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


def test_readback_failure_retains_unknown_outcome(monkeypatch, candidate):
    monkeypatch.setattr(P, 'absent', lambda t: None)
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


def test_workflow_remains_disabled_and_artifacts_are_json_only():
    source = (ROOT / '.github/workflows/hub-executor-publication.yml').read_text()
    assert "if: ${{ false && github.ref == 'refs/heads/main'" in source
    assert 'packages: write' in source
    assert 'executor-compatibility-receipt.json' in source
    assert 'executor-publication-receipt.json' in source
    assert 'docker push' not in source  # Only bounded helper can push.
