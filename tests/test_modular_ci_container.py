"""Synthetic image-owned CI admission/orchestration; no image or network work."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    'modular_ci_container', Path(__file__).parents[1] / 'scripts/modular_ci_container.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / 'workspace'
    cache = tmp_path / 'cache'
    output = tmp_path / 'output'
    evidence = tmp_path / 'evidence'
    for path in (root, cache, output, evidence):
        path.mkdir()
    dependencies = {}
    for name in ci.FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((name + '\n').encode())
        dependencies[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'schema': ci.MANIFEST_SCHEMA, 'dependency_sha256': dependencies,
                'cache_base_image': 'fixture.invalid/cache@sha256:' + 'a' * 64,
                'prepared_source_commit': 'b' * 40,
                'bootstrap_sha256': hashlib.sha256(Path(ci.__file__).read_bytes()).hexdigest()}
    (cache / 'modular-cache.json').write_text(json.dumps(manifest))
    dist = root / 'hub-ui/dist'
    dist.mkdir()
    (dist / 'index.html').write_text('<h1>synthetic UI</h1>')
    (dist / 'asset.js').write_text('export const fixture = true;')
    return SimpleNamespace(root=root, cache=cache, output=output, evidence=evidence,
                           manifest=manifest)


def stage(layout, name, runner):
    return ci.run_stage(name, workspace=layout.root, cache=layout.cache,
                        output=layout.output, evidence=layout.evidence, runner=runner)


def fake_facade(monkeypatch, events):
    monkeypatch.setattr(ci, 'node_facade', lambda *args: events.append('facade'))


def test_entire_python_suite_and_ui_unit_build_with_image_owned_locks(layout, monkeypatch):
    events, calls = [], []
    fake_facade(monkeypatch, events)

    def execute(argv, **kwargs):
        events.append('command')
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    build = stage(layout, 'build', execute)
    assert build['state'] == 'passed'
    assert (layout.output / 'index.html').read_text() == '<h1>synthetic UI</h1>'
    tested = stage(layout, 'test', execute)
    assert tested['state'] == 'passed'
    assert events == ['facade', 'command', 'facade', 'command', 'command']
    build_argv, python_argv, ui_argv = [call[0] for call in calls]
    assert build_argv == [ci.NPM, 'run', 'build', '--', '--base', './']
    assert python_argv[:6] == [ci.PYTHON, '-W', 'default', '-m', 'pytest', '-q']
    assert not {'-x', '-k', '-m', 'tests/test_staging_service_core.py'} & set(python_argv[6:])
    assert all(not part.startswith('tests/') for part in python_argv)
    assert '--junitxml=' + str(layout.evidence / 'python-junit.xml') in python_argv
    assert ui_argv[:3] == [ci.NPM, 'test', '--']
    assert '--maxWorkers=1' in ui_argv and '--no-file-parallelism' in ui_argv
    assert all(call[1]['shell'] is False and call[1]['check'] is False for call in calls)
    assert calls[1][1]['cwd'] == layout.root
    assert calls[1][1]['env']['PYTHONPATH'].split(os.pathsep)[0] == str(layout.root / 'src')
    assert calls[1][1]['env']['PIP_NO_INDEX'] == '1'
    assert calls[2][1]['env']['npm_config_offline'] == 'true'
    assert json.loads((layout.evidence / 'test-summary.json').read_text()) == tested
    assert [item['name'] for item in tested['checks']] == ['python_pytest', 'ui_unit']
    assert tested['commit_verification'] == 'declared'


@pytest.mark.parametrize('file', ci.FILES)
def test_lock_drift_refuses_before_source_commands_or_facade(layout, monkeypatch, file):
    events = []
    fake_facade(monkeypatch, events)
    (layout.root / file).write_bytes(b'changed lock\r\n')
    with pytest.raises(ci.CIError, match='ci_dependency_mismatch'):
        stage(layout, 'build', lambda *a, **kw: events.append('command'))
    assert events == []
    assert not list(layout.output.iterdir()) and not list(layout.evidence.iterdir())


@pytest.mark.parametrize('field,value', [
    ('bootstrap_sha256', '0' * 64), ('cache_base_image', 'cache:latest'),
    ('prepared_source_commit', 'HEAD'), ('dependency_sha256', {}), ('schema', 'unknown'),
])
def test_invalid_image_binding_refuses_before_execution(layout, monkeypatch, field, value):
    events = []
    fake_facade(monkeypatch, events)
    manifest = layout.manifest | {field: value}
    (layout.cache / 'modular-cache.json').write_text(json.dumps(manifest))
    with pytest.raises(ci.CIError):
        stage(layout, 'build', lambda *a, **kw: events.append('command'))
    assert events == []


def test_duplicate_manifest_keys_refused(layout):
    (layout.cache / 'modular-cache.json').write_text('{"schema":"one","schema":"two"}')
    with pytest.raises(ci.CIError, match='invalid_ci_manifest'):
        ci.verify_dependencies(layout.root, layout.cache)


def test_missing_image_manifest_refuses(layout):
    (layout.cache / 'modular-cache.json').unlink()
    with pytest.raises(OSError):
        ci.verify_dependencies(layout.root, layout.cache)


def test_failing_real_child_does_not_skip_ui_or_report_passing(layout, monkeypatch):
    events = []
    fake_facade(monkeypatch, events)
    stage(layout, 'build', lambda *a, **kw: SimpleNamespace(returncode=0))
    executed = []

    def execute(argv, **kwargs):
        executed.append(argv)
        # Exercise actual subprocess return semantics without invoking npm or pytest.
        code = 7 if argv[0] == ci.PYTHON else 0
        return subprocess.run([sys.executable, '-c', 'raise SystemExit(' + str(code) + ')'],
                              stdin=subprocess.DEVNULL, check=False)

    with pytest.raises(ci.CIError, match='ci_checks_failed'):
        stage(layout, 'test', execute)
    assert len(executed) == 2
    report = json.loads((layout.evidence / 'test-summary.json').read_text())
    assert report['state'] == 'failed'
    assert [item['exit_code'] for item in report['checks']] == [7, 0]


def test_failed_ui_build_never_exports_preview(layout, monkeypatch):
    fake_facade(monkeypatch, [])
    with pytest.raises(ci.CIError, match='ci_checks_failed'):
        stage(layout, 'build', lambda *a, **kw: SimpleNamespace(returncode=1))
    assert not list(layout.output.iterdir())
    assert json.loads((layout.evidence / 'build-summary.json').read_text())['state'] == 'failed'


def test_test_requires_same_successful_build_binding(layout, monkeypatch):
    fake_facade(monkeypatch, [])
    stage(layout, 'build', lambda *a, **kw: SimpleNamespace(returncode=0))
    build = json.loads((layout.output / 'ci-build.json').read_text())
    build['cache_binding_sha256'] = 'c' * 64
    (layout.output / 'ci-build.json').write_text(json.dumps(build))
    calls = []
    with pytest.raises(ci.CIError, match='ci_build_binding_mismatch'):
        stage(layout, 'test', lambda *a, **kw: calls.append(a))
    assert calls == []


@pytest.mark.parametrize('change', ['missing_index', 'oversized'])
def test_static_export_closed_bounds(layout, monkeypatch, change):
    if change == 'missing_index':
        (layout.root / 'hub-ui/dist/index.html').unlink()
    else:
        monkeypatch.setattr(ci, 'MAX_OUTPUT', 1)
    with pytest.raises(ci.CIError):
        ci.export_dist(layout.root, layout.output)
    assert not list(layout.output.iterdir())


@pytest.mark.skipif(os.name != 'posix', reason='Actual symlink creation/readonly package facade')
def test_node_packages_stay_cached_and_vite_temp_is_local(layout):
    modules = layout.cache / 'node_modules'
    (modules / 'vite').mkdir(parents=True)
    (modules / 'vite/package.json').write_text('{"name":"vite"}')
    (modules / '.vite').mkdir()
    (modules / '.vite/old').write_text('cache must not be reused')
    (modules / '.bin').mkdir()
    (modules / '.bin/vite').symlink_to('../vite/package.json')
    ci.node_facade(layout.root, layout.cache)
    target = layout.root / 'hub-ui/node_modules'
    assert (target / 'vite').is_symlink()
    assert (target / '.bin/vite').read_text() == '{"name":"vite"}'
    assert not (target / '.vite').exists()
    (target / '.vite').mkdir()
    (target / '.vite/new').write_text('job cache')
    assert not (modules / '.vite/new').exists()
    with pytest.raises(ci.CIError, match='source_node_modules_not_allowed'):
        ci.node_facade(layout.root, layout.cache)


@pytest.mark.skipif(os.name != 'posix', reason='Actual symlink refusal')
def test_dist_and_lock_symlinks_refused(layout):
    (layout.root / 'hub-ui/dist/link').symlink_to(layout.root / 'uv.lock')
    with pytest.raises(ci.CIError, match='ci_dist_unsafe'):
        ci.export_dist(layout.root, layout.output)
    lock = layout.root / 'uv.lock'
    lock.unlink()
    lock.symlink_to(layout.root / 'pyproject.toml')
    with pytest.raises(ci.CIError, match='invalid_ci_input'):
        ci.verify_dependencies(layout.root, layout.cache)


def test_existing_source_node_modules_refused(layout):
    (layout.cache / 'node_modules/vite').mkdir(parents=True)
    (layout.root / 'hub-ui/node_modules').mkdir()
    with pytest.raises(ci.CIError, match='source_node_modules_not_allowed'):
        ci.node_facade(layout.root, layout.cache)


def test_legacy_bootstrap_interface_untouched_and_image_does_not_install():
    root = Path(__file__).parents[1]
    legacy = (root / 'scripts/local_ci_container.py').read_text()
    assert "INPUT = Path('/input')" in legacy and "INPUT / 'source.tar'" in legacy
    candidate = (root / 'docker/Dockerfile.modular-development-ci').read_text()
    assert 'RUN --network=none ' in candidate
    assert 'npm ci' not in candidate and 'pip install' not in candidate and 'uv sync' not in candidate
    assert '/opt/eh-ci/modular-bootstrap.py' in candidate
