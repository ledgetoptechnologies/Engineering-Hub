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
