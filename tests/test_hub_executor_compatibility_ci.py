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
