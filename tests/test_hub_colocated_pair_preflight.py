"""Static synthetic data checks; no credentials, socket or host acceptance."""
import json
from uuid import uuid4

import pytest

pytest.importorskip('fcntl')
from hub_container_executor.colocated_pair_preflight import validate_pair
from tests.test_hub_container_executor_installed_runner import configuration as runner_configuration, canonical
from tests.test_hub_container_executor_installed_coordinator import configuration as coordinator_configuration
from tests.test_hub_trusted_queued_session import document
from hub_container_executor import installed_runner, installed_coordinator
from engineering_hub.container_pilot_manifest import PilotManifest


def fixture_documents():
    runner=runner_configuration(); coordinator=coordinator_configuration()
    runner['schema']=installed_runner.SCHEMA_SINGLE_PILOT; runner['resources']['single_pilot_protocol']=True
    coordinator['schema']=installed_coordinator.SCHEMA_TASK_SCOPED; coordinator['single_pilot_protocol']=True
    coordinator['manifest']=runner['manifest']
    manifest=PilotManifest.from_bytes(canonical(runner['manifest']))
    session=document(); session['release']=dict(runner['release_enrollment'])
    session['scope']={'actors': ['codex'], 'task_ids': coordinator['allowed_task_ids']}
    session['launch']['engine_id']=manifest.runner_binding.daemon_identity
    session['report']['executor']=dict(session['launch'], image_config_id=session['release']['image_config_id'])
    session['report']['manifest_digest']=manifest.digest()
    return runner,coordinator,session


def check(runner,coordinator,session):
    return validate_pair(canonical(runner),canonical(coordinator),canonical(session))


def test_static_pair_accepts_same_pins_without_host_access(monkeypatch):
    runner,coordinator,session=fixture_documents()
    import socket
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('unexpected socket'))
    result=check(runner,coordinator,session)
    assert result['state']=='static_pair_consistent_not_approved' and result['single_pilot_protocol'] is True


def test_legacy_pair_is_not_reported_ready_without_task_scope():
    runner, coordinator, session = fixture_documents()
    coordinator['schema'] = installed_coordinator.SCHEMA_SINGLE_PILOT
    del coordinator['allowed_task_ids']
    with pytest.raises(ValueError, match='^colocated pair validation failed$'):
        check(runner, coordinator, session)


@pytest.mark.parametrize('mismatch', ['protocol','manifest','bearer','issuer','key','image','profile'])
def test_static_pair_rejects_mismatched_independent_pins(mismatch):
    runner,coordinator,session=fixture_documents()
    if mismatch=='protocol':
        runner['schema']=installed_runner.SCHEMA; del runner['resources']['single_pilot_protocol']
    elif mismatch=='manifest': runner['manifest']['campaign_id']=str(uuid4())
    elif mismatch=='bearer': runner['resources']['secret_bindings'][-1][2]='sha256:'+'e'*64
    elif mismatch=='issuer': session['release']['issuer_id']='different-issuer'
    elif mismatch=='key': session['release']['public_key_hex']='b'*64
    elif mismatch=='image':
        session['release']['image_config_id']='sha256:'+'c'*64
        session['report']['executor']['image_config_id']=session['release']['image_config_id']
    elif mismatch=='profile': session['release']['launch_profile_digest']='sha256:'+'d'*64
    with pytest.raises(ValueError,match='^colocated pair validation failed$'):
        check(runner,coordinator,session)


def test_invalid_report_fails_before_any_approval():
    runner,coordinator,session=fixture_documents()
    session['report']['attestations']['writers_excluded']=False
    with pytest.raises(ValueError,match='^colocated pair validation failed$'):
        check(runner,coordinator,session)


def test_cli_requires_readonly_protected_reads_and_only_reports_safe_summary(monkeypatch, capsys):
    from hub_container_executor import colocated_pair_preflight as module
    runner,coordinator,session=fixture_documents()
    inputs={'/runner':canonical(runner), '/coordinator':canonical(coordinator), '/session':canonical(session)}
    seen=[]
    def read(path, *, maximum_bytes, require_readonly):
        assert require_readonly is True and maximum_bytes > 0
        seen.append(path)
        return inputs[path]
    monkeypatch.setattr(module, 'read_protected_file', read)
    assert module.main(['--runner','/runner','--coordinator','/coordinator','--session','/session'])==0
    assert seen==['/runner','/coordinator','/session']
    output=json.loads(capsys.readouterr().out)
    assert output['state']=='static_pair_consistent_not_approved'
    assert set(output)=={'state','manifest_digest','single_pilot_protocol'}
