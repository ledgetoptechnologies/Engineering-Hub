"""Actual disposable pytest sessions and pure bounded publication failure tests."""
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/modular_ci_pytest_reporter.py'
spec = importlib.util.spec_from_file_location('trusted_bounded_reporter', SCRIPT)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def launch(tmp_path, source, *, options=(), report_exists=False, pytest_args=()):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'src').mkdir()
    (root / 'test_fixture.py').write_text(source, encoding='utf-8')
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    report = evidence / 'python-summary.json'
    if report_exists:
        report.write_bytes(b'preserve previous immutable report')
    env = os.environ.copy()
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    # Console details deliberately go nowhere; the bounded artifacts are tested.
    result = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT),
        '--source-root', str(root), '--report', str(report), *options, '--',
        '-q', '--basetemp=' + str(tmp_path / 'pytest-temp'), '-o',
        'cache_dir=' + str(tmp_path / 'pytest-cache'), *pytest_args],
        cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, timeout=30, check=False)
    return result, report


def read_report(path):
    data = path.read_bytes()
    assert 0 < len(data) <= reporter.MAX_SUMMARY_BYTES
    summary = json.loads(data)
    details = path.parent / summary['details']['file']
    raw = details.read_bytes()
    assert len(raw) == summary['details']['bytes'] <= reporter.MAX_DETAIL_BYTES
    assert hashlib.sha256(raw).hexdigest() == summary['details']['sha256']
    return summary, raw


def test_actual_full_collection_totals_include_pass_skip_xfail_and_xpass(tmp_path):
    result, path = launch(tmp_path, """
import pytest
def test_pass(): assert True
@pytest.mark.skip(reason='fixture')
def test_skip(): pass
@pytest.mark.xfail(reason='fixture')
def test_xfail(): assert False
@pytest.mark.xfail(reason='fixture', strict=False)
def test_xpass(): assert True
""")
    summary, raw = read_report(path)
    assert result.returncode == 0
    assert summary['schema'] == reporter.SCHEMA
    assert summary['state'] == 'passed' and summary['complete'] is True
    assert summary['report_error'] is None and summary['pytest_exit_code'] == 0
    assert summary['outcomes'] == dict(collected=4, deselected=0, executed=4,
        collection_errors=0, passed=1, failed=0, errors=0, skipped=1, xfailed=1, xpassed=1)
    assert raw == b'' and summary['details']['entries'] == 0


def test_setup_call_and_teardown_failures_count_cases_once_and_preserve_phase_counts(tmp_path):
    result, path = launch(tmp_path, """
import pytest
@pytest.fixture
def bad_setup(): raise RuntimeError('setup fixture failed')
@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError('teardown fixture failed')
def test_setup(bad_setup): pass
def test_teardown(bad_teardown): assert True
def test_failure(): assert False, 'ordinary failure'
""")
    summary, raw = read_report(path)
    assert result.returncode == 1 and summary['state'] == 'failed'
    assert summary['complete'] is True
    assert summary['outcomes']['executed'] == summary['outcomes']['collected'] == 3
    assert summary['outcomes']['errors'] == 2 and summary['outcomes']['failed'] == 1
    assert summary['outcomes']['passed'] == 0
    assert summary['reports']['setup']['failed'] == summary['reports']['teardown']['failed'] == 1
    assert summary['reports']['call']['failed'] == 1
    assert b'ordinary failure' in raw and b'setup fixture failed' in raw


def test_many_failures_have_full_counts_and_fixed_total_detail_budget(tmp_path):
    result, path = launch(tmp_path, """
import pytest
@pytest.mark.parametrize('value', range(200))
def test_many(value):
    print('capture' * 5000)
    assert value < 0, 'failure ' + str(value)
""", options=('--detail-bytes', '2048', '--case-bytes', '1024'))
    summary, raw = read_report(path)
    assert result.returncode == 1 and summary['outcomes']['failed'] == 200
    assert summary['outcomes']['executed'] == summary['outcomes']['collected'] == 200
    assert summary['complete'] is True and summary['details']['omitted'] > 0
    assert len(raw) <= 2048
    assert all(len(row) + 1 <= 1024 for row in raw.splitlines())
    assert summary['details']['truncated'] > 0


def test_huge_unicode_capture_and_traceback_are_valid_bounded_utf8_json(tmp_path):
    result, path = launch(tmp_path, """
def test_huge():
    print(('🌲\\\\\\t\\n' * 200000))
    raise RuntimeError('large failure ' + 'é' * 100000)
""")
    summary, raw = read_report(path)
    assert result.returncode == 1 and summary['outcomes']['failed'] == 1
    assert summary['details']['truncated'] == 1
    assert len(raw) <= reporter.MAX_CASE_BYTES
    entry = json.loads(raw)
    assert entry['truncated'] is True and entry['phase'] == 'call'
    assert entry['traceback']


def test_collection_failure_is_reported_without_claiming_passed_tests(tmp_path):
    result, path = launch(tmp_path, "raise RuntimeError('collection fixture failure')\n")
    summary, raw = read_report(path)
    assert result.returncode != 0 and summary['state'] == 'failed'
    assert summary['outcomes']['collection_errors'] == 1
    assert summary['outcomes']['executed'] == 0
    assert summary['complete'] is False and summary['report_error'] == 'collection_failed'
    assert b'collection fixture failure' in raw


def test_forwarded_minority_selection_is_selected_count_not_false_coverage(tmp_path):
    result, path = launch(tmp_path, """
def test_chosen(): pass
def test_other_one(): pass
def test_other_two(): pass
def test_other_three(): pass
""", pytest_args=('-k', 'chosen'))
    summary, _ = read_report(path)
    assert result.returncode == 0 and summary['complete'] is True
    assert summary['outcomes']['collected'] == summary['outcomes']['executed'] == 1
    assert summary['outcomes']['deselected'] == 3


def test_early_stop_failure_never_claims_complete_selected_execution(tmp_path):
    result, path = launch(tmp_path, """
def test_first_failed(): assert False
def test_second_not_run(): pass
def test_third_not_run(): pass
""", pytest_args=('-x',))
    summary, _ = read_report(path)
    assert result.returncode == 1 and summary['complete'] is False
    assert summary['outcomes']['collected'] == 3 and summary['outcomes']['executed'] == 1
    assert summary['report_error'] == 'case_count_mismatch'


def test_actual_keyboard_interrupt_is_incomplete_and_nonzero(tmp_path):
    result, path = launch(tmp_path, 'def test_interrupt(): raise KeyboardInterrupt()\n')
    summary, _ = read_report(path)
    assert result.returncode != 0 and summary['complete'] is False
    assert summary['state'] == 'failed' and summary['pytest_exit_code'] == 2


def test_zero_detail_budget_explicitly_omits_details_without_losing_outcome_counts(tmp_path):
    result, path = launch(tmp_path, 'def test_failed(): assert False\n',
                          options=('--detail-bytes', '0'))
    summary, raw = read_report(path)
    assert result.returncode == 1 and raw == b''
    assert summary['outcomes']['failed'] == 1 and summary['details']['omitted'] == 1
    assert summary['complete'] is True


def test_existing_summary_is_preserved_and_passing_tests_cannot_make_publication_failure_pass(tmp_path):
    result, path = launch(tmp_path, 'def test_pass(): pass\n', report_exists=True)
    assert result.returncode != 0
    assert path.read_bytes() == b'preserve previous immutable report'
    assert not list(path.parent.glob('*.tmp'))


def test_real_entrypoint_loads_trusted_pytest_then_current_source_imports(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'src').mkdir()
    (root / 'src/fixture_branch_module.py').write_text('VALUE = 42\n')
    (root / 'pytest.py').write_text("raise RuntimeError('source shadow must not load')\n")
    (root / 'modular_ci_pytest_reporter.py').write_text("raise RuntimeError('source reporter must not load')\n")
    (root / 'test_branch.py').write_text('import fixture_branch_module\ndef test_current(): assert fixture_branch_module.VALUE == 42\n')
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    path = evidence / 'python-summary.json'
    env = os.environ.copy()
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    completed = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT),
        '--source-root', str(root), '--report', str(path), '--', '-q',
        '--basetemp=' + str(tmp_path / 'temp'), '-o', 'cache_dir=' + str(tmp_path / 'cache')],
        cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    assert completed.returncode == 0
    summary, _ = read_report(path)
    assert summary['outcomes']['passed'] == 1


@pytest.mark.parametrize('failing', ['details', 'summary'])
def test_enospc_publication_is_nonzero_even_when_all_tests_pass(tmp_path, monkeypatch, failing):
    path = tmp_path / 'python-summary.json'
    plugin = reporter.Reporter(path)
    plugin.pytest_collection_finish(SimpleNamespace(items=[]))
    original = reporter.atomic_bytes
    def full(target, *args):
        if target == (plugin.detail_path if failing == 'details' else plugin.path):
            raise OSError(errno.ENOSPC, 'synthetic filesystem full')
        return original(target, *args)
    monkeypatch.setattr(reporter, 'atomic_bytes', full)
    session = SimpleNamespace(exitstatus=0)
    plugin.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1 and plugin.summary['complete'] is False
    assert plugin.summary['report_error'] == failing + '_write_failed'
    if failing == 'details':
        assert json.loads(path.read_bytes())['report_error'] == 'details_write_failed'
    else:
        assert not path.exists()


def test_missing_case_finish_cannot_publish_passing_complete_summary(tmp_path):
    plugin = reporter.Reporter(tmp_path / 'summary.json')
    plugin.pytest_collection_finish(SimpleNamespace(items=[object()]))
    session = SimpleNamespace(exitstatus=0)
    plugin.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1
    assert plugin.summary['complete'] is False
    assert plugin.summary['report_error'] == 'case_count_mismatch'


def test_trace_detail_reads_bounded_existing_strings_without_custom_formatter():
    class Representation:
        reprtraceback = SimpleNamespace(reprentries=[SimpleNamespace(lines=['E    bounded failure'])])
        reprcrash = SimpleNamespace(path='fixture.py', message='bounded failure')
        def toterminal(self, writer):
            raise AssertionError('unbounded formatter must not be invoked')
    text, truncated = reporter.trace_text(Representation(), 128)
    assert 'bounded failure' in text and truncated is False
    text, truncated = reporter.trace_text(Representation(), 8)
    assert len(text.encode()) <= 8 and truncated is True


@pytest.mark.parametrize('kwargs', [
    {'detail_bytes': -1}, {'detail_bytes': reporter.MAX_DETAIL_BYTES + 1},
    {'detail_bytes': True}, {'case_bytes': 511}, {'case_bytes': reporter.MAX_CASE_BYTES + 1},
])
def test_operator_budgets_cannot_exceed_hard_bounds(tmp_path, kwargs):
    with pytest.raises(ValueError):
        reporter.Reporter(tmp_path / 'summary.json', **kwargs)
