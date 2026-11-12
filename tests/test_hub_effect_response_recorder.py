"""Synthetic same-process response recorder, never daemon recovery authority."""
from dataclasses import replace
import os
import pytest

pytest.importorskip('fcntl')
from hub_container_executor.effect_binding_canonical import canonical_nonsecret_binding
from hub_container_executor.effect_mac_key import EffectMacKey, create_for_new_journal
from hub_container_executor.effect_response_recorder import DurableEffectResponseRecorder
from hub_container_executor.operation_journal import OperationBinding, SQLiteOperationJournal, OperationJournalError
from tests.test_hub_container_effect_binding_canonical import fixture


def setup_journal(tmp_path):
    root=tmp_path/'private'; root.mkdir(mode=0o700); path=root/'journal.sqlite'
    key_id=create_for_new_journal(root,path.name)
    journal=SQLiteOperationJournal(path,create=True,allow_ephemeral_test_storage=True)
    key=EffectMacKey(root,path.name,expected_key_id=key_id)
    journal.enable_bindings()
    journal.enable_response_records()
    expected=replace(fixture(),owner_epoch=journal._owner.epoch)
    binding=OperationBinding(expected.boot_id,expected.daemon_pid,expected.daemon_start_ticks,
        expected.resource_name,expected.container_id,key.mac(canonical_nonsecret_binding(expected)),expected.fence)
    journal.begin_bound(expected.operation_id,expected.kind,binding)
    return root,path,journal,key,expected


def test_response_projection_is_durable_bound_and_does_not_complete_effect(tmp_path):
    root,path,journal,key,expected=setup_journal(tmp_path)
    recorder=DurableEffectResponseRecorder(journal,key)
    try:
        secret='synthetic-private-token-93401'
        mac=recorder.begin(expected.operation_id,'create',route='/v1.45/containers/create',
                           params={'name':expected.resource_name},body={'Env':['TOKEN='+secret]})
        assert recorder.inspect(expected.operation_id)[0][3]=='pending'
        with pytest.raises(OperationJournalError): journal.require_no_pending(allow_bound=True)
        with pytest.raises(OperationJournalError):
            recorder.begin(expected.operation_id,'create',route='/v1.45/containers/create',params=None,body={})
        raw=b'{"Id":"'+b'a'*64+b'","Warnings":[]}'
        recorder.complete(expected.operation_id,'create',mac,status=201,raw=raw)
        row=recorder.inspect(expected.operation_id)[0]
        assert row[3:7] == ('received',201,row[5],'a'*64)
        assert recorder.matches_raw_response(expected.operation_id, 'create', raw) is True
        assert recorder.matches_raw_response(expected.operation_id, 'create', raw + b' ') is False
        assert journal.inspect_record(expected.operation_id).state=='pending'
        with pytest.raises(OperationJournalError): journal.require_no_pending(allow_bound=True)
        assert secret.encode() not in path.read_bytes()
        with pytest.raises(OperationJournalError): recorder.complete(expected.operation_id,'create',mac,status=201,raw=raw)
    finally:
        recorder.close(); key.close(); journal.close()


def test_migration_refuses_pending_and_lost_response_remains_pending(tmp_path):
    root=tmp_path/'private'; root.mkdir(mode=0o700); path=root/'journal.sqlite'
    key_id=create_for_new_journal(root,path.name)
    journal=SQLiteOperationJournal(path,create=True,allow_ephemeral_test_storage=True)
    key=EffectMacKey(root,path.name,expected_key_id=key_id)
    try:
        journal.enable_bindings()
        expected=replace(fixture(),owner_epoch=journal._owner.epoch)
        binding=OperationBinding(expected.boot_id,expected.daemon_pid,expected.daemon_start_ticks,
            expected.resource_name,expected.container_id,key.mac(canonical_nonsecret_binding(expected)),expected.fence)
        journal.begin_bound(expected.operation_id,expected.kind,binding)
        with pytest.raises(OperationJournalError): journal.enable_response_records()
        with journal._transaction() as db: assert db.execute('PRAGMA user_version').fetchone()==(2,)
    finally:
        key.close(); journal.close()


@pytest.mark.parametrize('failure', ['none','pre_post_guard','response_record'])
def test_backend_records_intent_before_post_and_response_before_return(tmp_path, monkeypatch, failure):
    import time
    from uuid import uuid4
    from engineering_hub.container_pilot_client import ContainerPilotRequest
    from hub_container_executor.adapter import EffectCallContext
    from tests.test_hub_container_executor_daemon_backend import create_transcript
    root,path,journal,key,expected=setup_journal(tmp_path)
    recorder=DurableEffectResponseRecorder(journal,key)
    backend,manifest,image,plan,requests=create_transcript(tmp_path,operation_journal=journal)
    backend.effect_response_recorder=recorder
    context=EffectCallContext(ContainerPilotRequest.model_construct(run_id=str(uuid4()),fencing_generation=1),
                              expected.operation_id,'create_candidate')
    if failure=='pre_post_guard':
        monkeypatch.setattr(backend,'_recheck_effect',lambda *args: (_ for _ in ()).throw(ValueError('denied')))
    if failure=='response_record':
        monkeypatch.setattr(recorder,'complete',lambda *args,**kwargs: (_ for _ in ()).throw(ValueError('lost')))
    try:
        with backend.effect_context_scope(context):
            if failure=='none':
                result=backend._effect_response(expected.operation_id,'create',backend._route('/containers/create'),
                    time.monotonic()+3,expected_status=201,admitted=None,
                    json_body={'Env':['TOKEN=synthetic-secret-482']},params={'name':plan.resource_name})
                assert b'"Id"' in result
            else:
                with pytest.raises(ValueError):
                    backend._effect_response(expected.operation_id,'create',backend._route('/containers/create'),
                        time.monotonic()+3,expected_status=201,admitted=None,
                        json_body={'Env':['TOKEN=synthetic-secret-482']},params={'name':plan.resource_name})
        posts=[item for item in requests if item[0]=='POST']
        assert len(posts)==(0 if failure=='pre_post_guard' else 1)
        rows=recorder.inspect(expected.operation_id)
        assert len(rows)==1 and rows[0][3]==('received' if failure=='none' else 'pending')
        assert b'synthetic-secret-482' not in path.read_bytes()
        assert journal.inspect_record(expected.operation_id).state=='pending'
        with pytest.raises(OperationJournalError): journal.require_no_pending(allow_bound=True)
    finally:
        backend.close(); recorder.close(); key.close(); journal.close()


def test_recorded_projection_reopens_but_unknown_effect_stays_held(tmp_path):
    root,path,journal,key,expected=setup_journal(tmp_path)
    key_id='sha256:'+key._key_digest.hex()
    recorder=DurableEffectResponseRecorder(journal,key)
    mac=recorder.begin(expected.operation_id,'create',route='/v1.45/containers/create',params=None,body={})
    recorder.complete(expected.operation_id,'create',mac,status=201,raw=b'{"Id":"'+b'a'*64+b'","Warnings":[]}')
    recorder.close(); key.close(); journal.close()
    journal=SQLiteOperationJournal(path,create=False,allow_ephemeral_test_storage=True)
    key=EffectMacKey(root,path.name,expected_key_id=key_id)
    recorder=DurableEffectResponseRecorder(journal,key)
    try:
        assert recorder.inspect(expected.operation_id)[0][3]=='received'
        assert journal.inspect_record(expected.operation_id).state=='pending'
        with pytest.raises(OperationJournalError): journal.require_no_pending(allow_bound=True)
        with pytest.raises(OperationJournalError):
            recorder.begin(expected.operation_id,'create',route='/v1.45/containers/create',params=None,body={})
    finally:
        recorder.close(); key.close(); journal.close()


def test_modified_response_projection_fails_authentication(tmp_path):
    root,path,journal,key,expected=setup_journal(tmp_path)
    recorder=DurableEffectResponseRecorder(journal,key)
    try:
        mac=recorder.begin(expected.operation_id,'create',route='/v1.45/containers/create',params=None,body={})
        recorder.complete(expected.operation_id,'create',mac,status=201,raw=b'{"Id":"'+b'a'*64+b'","Warnings":[]}')
        with journal._transaction() as db:
            db.execute('UPDATE private_effect_response SET safe_value=? WHERE operation_id=?',('b'*64,expected.operation_id))
        with pytest.raises(ValueError): recorder.inspect(expected.operation_id)
        with pytest.raises(OperationJournalError): journal.require_no_pending(allow_bound=True)
    finally:
        recorder.close(); key.close(); journal.close()
