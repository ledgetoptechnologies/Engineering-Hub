"""The negative control must reject latency, never accept generic child failure."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from tests import responsiveness_probe as probe


def valid():
    return dict(schema='engineering-hub.isolated-responsiveness.v1',source={},scenario='unified',
        action=None,negative_control=True,gc_enabled=True,warmup=False,functional_verified=True,
        latency_limit_seconds=.25,samples=[.31,.001,.001,.001,.001],sample_count=5,
        max_seconds=.31,latency_accepted=False,rejection='latency_limit_exceeded',
        inline_observation_overlaps_request=True,workflow_state='succeeded',
        session_deleted=True,lifespan_drained=True,resource_audit_passed=True)


@pytest.mark.parametrize('change', ['source','functional','latency','rejection','gc','warmup','overlap','cleanup','resource'])
def test_report_validation_refuses_forged_or_nonspecific_negative(change):
    value=copy.deepcopy(valid())
    if change=='source': value['source']={'wrong':'bytes'}
    elif change=='functional': value['functional_verified']=False
    elif change=='latency': value['max_seconds']=.01
    elif change=='rejection': value['rejection']='generic_child_error'
    elif change=='gc': value['gc_enabled']=False
    elif change=='warmup': value['warmup']=True
    elif change=='overlap': value['inline_observation_overlaps_request']=False
    elif change=='cleanup': value['session_deleted']=False
    else: value['resource_audit_passed']=False
    with pytest.raises(AssertionError):
        probe.validate_report(value,scenario='unified',action=None,negative_control=True,expected_source={})


def test_unexpected_child_exit_is_not_negative_control_success(monkeypatch):
    output=probe.PREFIX+json.dumps(valid())+'\n'
    monkeypatch.setattr(probe, 'source_provenance', lambda: {})
    monkeypatch.setattr(probe.subprocess,'run',lambda *args,**kwargs:
        SimpleNamespace(returncode=17,stdout=output,stderr='synthetic unrelated failure'))
    with pytest.raises(AssertionError):
        probe.run_isolated_probe('unified',negative_control=True)


def test_missing_or_duplicate_child_report_is_not_success(monkeypatch):
    output=probe.PREFIX+json.dumps(valid())+'\n'
    for stdout in ('',output+output):
        monkeypatch.setattr(probe.subprocess,'run',lambda *args,**kwargs:
            SimpleNamespace(returncode=0,stdout=stdout,stderr=''))
        with pytest.raises(AssertionError):
            probe.run_isolated_probe('unified',negative_control=True)


def retained(tmp_path):
    files = list(tmp_path.glob('*.json'))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding='utf-8'))


@pytest.mark.parametrize('exit_code', [0, 3])
def test_complete_report_survives_acceptance_or_latency_failure(monkeypatch, tmp_path, exit_code):
    value = valid()
    value['negative_control'] = exit_code == 0
    # A long report reproduces the failure that lost the maximum in stdout[-4000:].
    value['samples'] = [.31] + [.001] * 1000
    value['sample_count'] = len(value['samples'])
    monkeypatch.setenv('EH_RESPONSIVENESS_ARTIFACT_DIR', str(tmp_path))
    monkeypatch.setattr(probe, 'source_provenance', lambda: {})
    monkeypatch.setattr(probe.subprocess, 'run', lambda *args, **kwargs:
        SimpleNamespace(returncode=exit_code, stdout=probe.PREFIX+json.dumps(value)+'\n',
                        stderr='synthetic-private-stderr-marker'))
    if exit_code:
        with pytest.raises(AssertionError, match='max_seconds'):
            probe.run_isolated_probe('unified')
    else:
        assert probe.run_isolated_probe('unified', negative_control=True) == value
    evidence = retained(tmp_path)
    assert evidence['report'] == value
    assert evidence['child_returncode'] == exit_code
    assert evidence['validation'] == ('failed' if exit_code else 'passed')
    assert 'synthetic-private-stderr-marker' not in json.dumps(evidence)


@pytest.mark.parametrize('stdout', ['', 'EH_RESPONSIVENESS_REPORT broken',
    'EH_RESPONSIVENESS_REPORT {"max_seconds":NaN}',
    'EH_RESPONSIVENESS_REPORT {"max_seconds":1e999}',
    'EH_RESPONSIVENESS_REPORT []',
    'EH_RESPONSIVENESS_REPORT {"noise":"' + 'x' * (1024 * 1024) + '"}',
    'EH_RESPONSIVENESS_REPORT {}\nEH_RESPONSIVENESS_REPORT {}'],
    ids=['missing', 'malformed', 'nan', 'overflow', 'array', 'oversize', 'duplicate'])
def test_invalid_report_is_failure_with_bounded_metadata(monkeypatch, tmp_path, stdout):
    monkeypatch.setenv('EH_RESPONSIVENESS_ARTIFACT_DIR', str(tmp_path))
    monkeypatch.setattr(probe.subprocess, 'run', lambda *args, **kwargs:
        SimpleNamespace(returncode=0, stdout=stdout, stderr='private-marker'))
    with pytest.raises((AssertionError, ValueError)):
        probe.run_isolated_probe('unified')
    evidence = retained(tmp_path)
    assert evidence['validation'] == 'failed' and evidence['report'] is None
    assert 'private-marker' not in json.dumps(evidence)


def test_timeout_retains_failure_without_arbitrary_child_output(monkeypatch, tmp_path):
    monkeypatch.setenv('EH_RESPONSIVENESS_ARTIFACT_DIR', str(tmp_path))
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('synthetic', 60, output='private-marker')
    monkeypatch.setattr(probe.subprocess, 'run', timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        probe.run_isolated_probe('unified')
    evidence = retained(tmp_path)
    assert evidence['error_type'] == 'TimeoutExpired' and evidence['validation'] == 'failed'
    assert 'private-marker' not in json.dumps(evidence)


def test_each_invocation_keeps_a_separate_report(monkeypatch, tmp_path):
    monkeypatch.setenv('EH_RESPONSIVENESS_ARTIFACT_DIR', str(tmp_path))
    for attempt in range(2):
        probe.retain_probe_evidence({'attempt': attempt})
    assert sorted(json.loads(p.read_text())['attempt'] for p in tmp_path.glob('*.json')) == [0, 1]


def test_artifacts_exclude_unknown_values_and_unverified_source_paths(monkeypatch, tmp_path):
    value = valid()
    value.update(private='synthetic-private-marker', source={'private': 'synthetic-private-marker'})
    value['samples'] = ['synthetic-private-marker']
    monkeypatch.setenv('EH_RESPONSIVENESS_ARTIFACT_DIR', str(tmp_path))
    monkeypatch.setattr(probe, 'source_provenance', lambda: {})
    monkeypatch.setattr(probe.subprocess, 'run', lambda *args, **kwargs:
        SimpleNamespace(returncode=3, stdout=probe.PREFIX+json.dumps(value)+'\n', stderr=''))
    with pytest.raises(AssertionError):
        probe.run_isolated_probe('unified')
    evidence = retained(tmp_path)
    assert evidence['omitted_field_count'] == 3
    assert 'synthetic-private-marker' not in json.dumps(evidence)
    assert evidence['report']['max_seconds'] == .31


def test_diagnostic_filter_handles_huge_integer_without_masking_failure():
    value, omitted = probe.diagnostic_report({'max_seconds': 10**1000}, {})
    assert value == {} and omitted == 1


@pytest.mark.skipif(sys.platform != 'linux',reason='Linux owned responsiveness scenario')
@pytest.mark.parametrize('failure', ['warning','unraisable'])
def test_resource_failure_cannot_emit_accepted_report(failure):
    root=Path(probe.__file__).resolve().parent.parent
    env=dict(os.environ,PYTHONPATH=os.pathsep.join((str(root/'src'),str(root))),PYTHONDONTWRITEBYTECODE='1')
    child=subprocess.run([sys.executable,'-B','-m','tests.responsiveness_probe','health',
        '--action','decline','--resource-negative-control',failure],cwd=root,env=env,
        capture_output=True,text=True,timeout=60)
    assert child.returncode != 0
    assert probe.PREFIX not in child.stdout
    assert ('ResourceWarning' if failure=='warning' else 'unraisable_cleanup_failure') in child.stderr
