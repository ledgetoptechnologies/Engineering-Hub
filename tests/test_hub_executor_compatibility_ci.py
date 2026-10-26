"""Fail-closed dispatch inputs and canonical source preparation; no Docker/network."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('executor_compatibility_ci', ROOT / 'scripts/hub_executor_compatibility_ci.py')
CI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CI)
SOURCE = '40d3d2fa126b8c0fb78f6f48a47c25aae38d38bc'


def environment():
    return dict(SOURCE_COMMIT=SOURCE, CORE_COMMIT=SOURCE, CORE_DIGEST='a' * 64,
                CORE_CONFIG='b' * 64, WHEEL_SHA256='c' * 64,
                SOURCE_DATE_EPOCH='1792972800', GITHUB_REF='refs/heads/main',
                GITHUB_EVENT_NAME='workflow_dispatch')


@pytest.mark.parametrize('key,value', [
    ('SOURCE_COMMIT', 'main'), ('CORE_DIGEST', 'a' * 64 + '\n'),
    ('CORE_CONFIG', '$(id)'), ('WHEEL_SHA256', 'A' * 64),
    ('CORE_COMMIT', '../main'), ('SOURCE_DATE_EPOCH', '-1'),
    ('GITHUB_REF', 'refs/heads/development'), ('GITHUB_EVENT_NAME', 'pull_request'),
])
def test_rejects_unreviewable_inputs(key, value):
    env = environment()
    env[key] = value
    with pytest.raises(ValueError):
        CI.inputs(env)


def test_validated_inputs_do_not_contain_credentials():
    env = environment()
    env['GITHUB_TOKEN'] = 'do-not-copy'
    assert set(CI.inputs(env)) == {'SOURCE_COMMIT', 'CORE_COMMIT', 'CORE_DIGEST',
                                  'CORE_CONFIG', 'WHEEL_SHA256', 'SOURCE_DATE_EPOCH'}


def test_preparation_uses_immutable_git_bytes(tmp_path):
    # Historical reviewed source remains an ancestor; source is never checkout-normalized.
    source, context = CI.prepare(CI.inputs(environment()), tmp_path)
    provenance = json.loads((source / 'src/hub_container_executor/_executor_source.json').read_bytes())
    assert provenance['source_commit'] == SOURCE
    assert provenance['source_archive_sha256'] == '07306b40c628191d4d57802b18d5575e0feb8676571aa487a2eda3190784b8f0'
    for original, destination in [('docker/Dockerfile.hub-executor', 'Dockerfile'),
                                  ('scripts/verify_hub_executor_wheel.py', 'validate_executor_wheel.py')]:
        raw = subprocess.check_output(['git', 'cat-file', 'blob', SOURCE + ':' + original], cwd=ROOT)
        assert (context / destination).read_bytes() == raw
    assert sorted(p.name for p in context.iterdir()) == ['Dockerfile', 'validate_executor_wheel.py']


def test_command_failure_does_not_expose_output(monkeypatch):
    monkeypatch.setattr(CI.subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 1, b'credential', b'private-data'))
    with pytest.raises(RuntimeError) as failure:
        CI.run('docker', 'inspect', 'fixture')
    assert 'credential' not in str(failure.value)
    assert 'private-data' not in str(failure.value)


def test_core_mismatch_stops_before_runtime_or_build(monkeypatch, tmp_path):
    calls = []

    def inspect_only(*args, **kwargs):
        calls.append(args)
        assert args[:3] == ('docker', 'image', 'inspect')
        return json.dumps([{'Id': 'sha256:' + 'd' * 64,
                            'RepoDigests': [CI.PACKAGE + 'a' * 64],
                            'Config': {'User': '568:568', 'Labels': {
                                'org.opencontainers.image.revision': SOURCE}}}]).encode()

    monkeypatch.setattr(CI, 'run', inspect_only)
    with pytest.raises(ValueError, match='identity mismatch'):
        CI.compatibility(CI.inputs(environment()), tmp_path)
    assert len(calls) == 1


def test_nonancestor_stops_before_source_preparation(monkeypatch, tmp_path):
    calls = []

    def reject(*args):
        calls.append(args)
        raise RuntimeError('not an ancestor')

    monkeypatch.setattr(CI, 'git', reject)
    with pytest.raises(RuntimeError, match='not an ancestor'):
        CI.prepare(CI.inputs(environment()), tmp_path)
    assert calls == [('merge-base', '--is-ancestor', SOURCE, 'HEAD')]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('exc,kind,code', [
    (CI.CommandFailure(2), 'command_failed', 2),
    (CI.CommandFailure(999999), 'command_failed', None),
    (subprocess.TimeoutExpired(['private-argv'], 1, output=b'secret'), 'command_timeout', None),
    (ValueError('secret'), 'validation_failed', None),
    (OSError('private-path'), 'io_failed', None),
])
def test_failure_codes_are_bounded_and_do_not_copy_exception_text(exc, kind, code):
    assert CI.failure(exc, 'wheel_build') == {'stage': 'wheel_build', 'kind': kind, 'exit_code': code}


def test_source_diagnostics_survive_preparation_failure(monkeypatch, tmp_path):
    def reject(*args, **kwargs):
        raise ValueError('private source path')

    monkeypatch.setattr(CI, 'prepare_archive', reject)
    evidence = {}
    with pytest.raises(ValueError):
        CI.prepare(CI.inputs(environment()), tmp_path, evidence)
    assert evidence['stage'] == 'source_prepare'
    assert evidence['source_archive_sha256'] == '07306b40c628191d4d57802b18d5575e0feb8676571aa487a2eda3190784b8f0'
    assert evidence['source_archive_bytes'] == 10168320


def test_observed_wheel_preserved_before_expected_verification(monkeypatch, tmp_path):
    source, context = tmp_path / 'source', tmp_path / 'context'
    context.mkdir()
    monkeypatch.setattr(CI, 'prepare', lambda *args: (source, context))
    monkeypatch.setattr(CI.importlib.metadata, 'version', lambda name: '25.0.1')
    backend = b'fixed-backend-fixture'
    import hashlib
    monkeypatch.setattr(CI, 'BACKEND_SHA', hashlib.sha256(backend).hexdigest())
    wheel_bytes = b'observed-wheel-fixture'

    def fake_run(*args, **kwargs):
        if 'download' in args:
            (tmp_path / 'backend/setuptools-84.0.0-py3-none-any.whl').write_bytes(backend)
        elif 'wheel' in args:
            (tmp_path / 'wheels').mkdir()
            (tmp_path / 'wheels/fixture.whl').write_bytes(wheel_bytes)
        return b''

    def reject(**kwargs):
        raise ValueError('expected hash mismatch')

    monkeypatch.setattr(CI, 'run', fake_run)
    monkeypatch.setattr(CI, 'verify_wheel', reject)
    evidence = {}
    with pytest.raises(ValueError):
        CI.wheel(CI.inputs(environment()), tmp_path, evidence)
    assert evidence['stage'] == 'wheel_verify'
    assert evidence['observed_wheel_sha256'] == hashlib.sha256(wheel_bytes).hexdigest()
    assert evidence['observed_wheel_bytes'] == len(wheel_bytes)
    assert evidence['observed_backend_sha256'] == hashlib.sha256(backend).hexdigest()
    assert evidence['pip_version'] == '25.0.1'
    assert not (context / 'executor.whl').exists()


def test_main_writes_substage_and_exit_code_on_failure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key, value in environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, 'argv', ['compatibility-fixture', 'wheel'])

    def reject(values, directory, evidence):
        evidence.update(stage='backend_download', python_version='3.12.15', pip_version='25.0.1')
        raise CI.CommandFailure(2)

    monkeypatch.setattr(CI, 'wheel', reject)
    with pytest.raises(RuntimeError, match='no deployment attempted'):
        CI.main()
    receipt = json.loads((tmp_path / 'executor-compatibility-receipt.json').read_text())
    assert receipt['state'] == 'wheel_failed'
    assert receipt['failure'] == {'stage': 'backend_download', 'kind': 'command_failed', 'exit_code': 2}
    assert receipt['wheel_evidence']['python_version'] == '3.12.15'
    assert receipt['inputs']['WHEEL_SHA256'] == 'c' * 64
    assert receipt['image_published'] is False
