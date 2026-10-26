"""Real temporary deletion/SQLite/fsync; injected faults, not full-FS proof."""
import errno
import json
import os
from pathlib import Path
import stat
from unittest.mock import Mock
import uuid

import pytest

from hub_staging_service.core import Rejected
from hub_staging_service.core import Service
from hub_staging_service.terminal_receipt import TerminalReceipt, FRAME_BYTES, _decode
from tests.test_staging_service_scratch import payload, profile
from tests.test_staging_service_terminal_receipt import configured, close, Backend


def request(job,kinds=('preview',)):
    return {'command_id':str(uuid.uuid4()),'idempotency_key':str(uuid.uuid4()),
        'resources':[{'resource_id':job['resource_ids'][kind],'expected_revision':1} for kind in kinds]}


def finished(configured):
    service=configured(slots=8);job=payload();service.submit(job);service.wait()
    assert service.status(job['job_id'])['state']=='succeeded'
    return service,job


def test_cleanup_v2_binding_original_ids_and_job_v1_survive_restart(configured,monkeypatch):
    service,job=finished(configured);cleanup=request(job)
    remove=service._delete_cleanup_resource;calls=[]
    def deleted(item,parent):
        calls.append(item['id'])
        row=service.db.execute('SELECT * FROM operations WHERE command_id=?',(cleanup['command_id'],)).fetchone()
        assert row['state']=='running' and row['job_id'] is None
        assert service.terminal_evidence(cleanup['command_id'])['state']=='uncertain'
        return remove(item,parent)
    monkeypatch.setattr(service,'_delete_cleanup_resource',deleted)
    result=service.cleanup(cleanup)
    assert result['state']=='succeeded' and result['job_id'] is None
    assert service.cleanup(cleanup)==result and calls==[job['resource_ids']['preview']]
    rows=service.db.execute('SELECT * FROM terminal_receipts').fetchall()
    for row in rows:
        original=json.loads(row['binding']);data=(Path(row['root'])/('receipt-'+row['slot_id']+'.bin')).read_bytes()
        if row['command_id']==job['command_id']:
            assert _decode(data[:FRAME_BYTES])['schema'].endswith('.v1') and 'operation_kind' not in original
        else:
            assert _decode(data[:FRAME_BYTES])['schema'].endswith('.v2')
            assert original['operation_kind']=='cleanup' and original['job_id'] is None and original['lease_identity'] is None
            assert original['resources'][0]['resource_id']==job['resource_ids']['preview']
            assert original['resources'][0]['job_id']==job['job_id']
    evidence=service.terminal_evidence(cleanup['command_id'])
    assert evidence['authority']=='evidence_only' and evidence['terminal']['result']==result['result']
    close(service);reopened=configured(slots=8)
    assert reopened._receipt_fault is None and reopened.cleanup(cleanup)==result and reopened.backend.calls==0


def test_parent_directory_fsync_precedes_terminal_observation_and_sql_success(configured,monkeypatch):
    service,job=finished(configured);cleanup=request(job)
    parent_inode=service.resources.stat().st_ino;events=[];sync=os.fsync;record=TerminalReceipt.record_terminal
    def synced(fd):
        value=os.fstat(fd)
        if stat.S_ISDIR(value.st_mode) and value.st_ino==parent_inode:events.append('parent-fsync')
        return sync(fd)
    def terminal(receipt,original,**kwargs):
        if original.get('operation_kind')=='cleanup':
            assert events==['parent-fsync']
            assert service.operation(cleanup['command_id'])['state']=='running'
            assert service.metadata(job['resource_ids']['preview'])['state']=='deleted'
            events.append('terminal-receipt')
        return record(receipt,original,**kwargs)
    monkeypatch.setattr(os,'fsync',synced);monkeypatch.setattr(TerminalReceipt,'record_terminal',terminal)
    assert service.cleanup(cleanup)['state']=='succeeded'
    assert events==['parent-fsync','terminal-receipt']


class FailCleanupCommit:
    def __init__(self,db,command,*,also_hold=False):
        self.db,self.command,self.also_hold=db,command,also_hold
        self.terminal=False;self.failures=0
    def __getattr__(self,name):return getattr(self.db,name)
    def execute(self,sql,args=()):
        if sql.startswith("UPDATE operations SET state='succeeded',result=?") and args[-1]==self.command:self.terminal=True
        return self.db.execute(sql,args)
    def commit(self):
        if self.terminal and (self.failures==0 or self.also_hold):
            self.failures+=1;raise OSError(errno.ENOSPC,'injected final cleanup commit full')
        return self.db.commit()


@pytest.mark.parametrize('also_hold',[False,True])
def test_final_sql_full_preserves_truthful_receipt_without_promoting_or_repeating(configured,monkeypatch,also_hold):
    service,job=finished(configured);cleanup=request(job)
    service.db=FailCleanupCommit(service.db,cleanup['command_id'],also_hold=also_hold)
    deleted=service._delete_cleanup_resource;calls=[]
    def remove(*args):calls.append(args[0]['id']);return deleted(*args)
    monkeypatch.setattr(service,'_delete_cleanup_resource',remove)
    with pytest.raises(Rejected,match='cleanup_uncertain'):service.cleanup(cleanup)
    assert service._receipt_fault and not (service.resources/job['resource_ids']['preview']).exists()
    assert service.terminal_evidence(cleanup['command_id'])['terminal']['outcome']=='succeeded'
    original=service.cleanup(cleanup)
    assert original['state']==('running' if also_hold else 'held') and len(calls)==1
    assert service.db.failures==(2 if also_hold else 1)
    with pytest.raises(Rejected):service.cleanup(request(job,('build',)))
    close(service);reopened=configured(slots=8)
    assert reopened._receipt_fault and reopened.operation(cleanup['command_id'])['state']=='held'
    assert reopened.cleanup(cleanup)['state']=='held' and reopened.backend.calls==0
    assert reopened.terminal_evidence(cleanup['command_id'])['terminal']['outcome']=='succeeded'


def test_parent_fsync_failure_is_not_recorded_as_confirmed_absence(configured,monkeypatch):
    service,job=finished(configured);cleanup=request(job);parent_inode=service.resources.stat().st_ino;sync=os.fsync
    def fail(fd):
        info=os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and info.st_ino==parent_inode:raise OSError(errno.ENOSPC,'parent metadata full')
        return sync(fd)
    monkeypatch.setattr(os,'fsync',fail)
    with pytest.raises(Rejected):service.cleanup(cleanup)
    assert service._receipt_fault and service.operation(cleanup['command_id'])['state']=='held'
    assert service.metadata(job['resource_ids']['preview'])['state']=='delete_failed'
    evidence=service.terminal_evidence(cleanup['command_id'])['terminal']
    assert evidence['outcome']=='held' and evidence['result']['deleted_resource_ids']==[]


def test_partial_delete_is_bound_and_never_replayed(configured,monkeypatch):
    service,job=finished(configured);cleanup=request(job,('build','preview'))
    import hub_staging_service.core as core
    remove=core.shutil.rmtree;calls=[]
    def fail(path,*args,**kwargs):
        calls.append(Path(path).name)
        if Path(path).name==job['resource_ids']['preview']:raise OSError(errno.ENOSPC,'partial fixture')
        return remove(path,*args,**kwargs)
    monkeypatch.setattr(core.shutil,'rmtree',fail)
    with pytest.raises(Rejected):service.cleanup(cleanup)
    assert service.metadata(job['resource_ids']['build'])['state']=='deleted'
    assert service.metadata(job['resource_ids']['preview'])['state']=='delete_failed'
    evidence=service.terminal_evidence(cleanup['command_id'])['terminal']
    assert evidence['outcome']=='held' and evidence['result']['deleted_resource_ids']==[job['resource_ids']['build']]
    assert service.cleanup(cleanup)['state']=='held' and len(calls)==2


def test_preallocation_partial_write_fails_before_delete_and_orphan_blocks_restart(configured,monkeypatch):
    service,job=finished(configured);cleanup=request(job);write=os.pwrite;calls=[0]
    def full(fd,raw,offset):
        calls[0]+=1
        if calls[0]==1:return write(fd,raw[:37],offset)
        raise OSError(errno.ENOSPC,'receipt allocation full')
    monkeypatch.setattr(os,'pwrite',full)
    remove=Mock();monkeypatch.setattr(service,'_delete_cleanup_resource',remove)
    with pytest.raises(Rejected):service.cleanup(cleanup)
    remove.assert_not_called();assert service._receipt_fault
    assert (service.resources/job['resource_ids']['preview']).is_dir()
    assert not service.db.execute('SELECT 1 FROM operations WHERE command_id=?',(cleanup['command_id'],)).fetchone()
    monkeypatch.setattr(os,'pwrite',write);close(service);reopened=configured(slots=8)
    assert reopened._receipt_fault and reopened.backend.calls==0


def test_cleanup_footer_fsync_failure_keeps_live_evidence_uncertain(configured,monkeypatch):
    service,job=finished(configured);cleanup=request(job);record=TerminalReceipt.record_terminal;sync=os.fsync
    inside=[False];calls=[0]
    def terminal(*args,**kwargs):
        inside[0]=True
        try:return record(*args,**kwargs)
        finally:inside[0]=False
    def fail(fd):
        if inside[0]:
            calls[0]+=1
            if calls[0]==2:raise OSError(errno.ENOSPC,'late receipt footer fsync')
        return sync(fd)
    monkeypatch.setattr(TerminalReceipt,'record_terminal',terminal);monkeypatch.setattr(os,'fsync',fail)
    with pytest.raises(Rejected):service.cleanup(cleanup)
    assert calls[0]==2 and service._receipt_fault
    assert service.terminal_evidence(cleanup['command_id'])=={'state':'uncertain','reason':'receipt_persistence_failed'}
    assert service.cleanup(cleanup)['state']=='held'


@pytest.mark.parametrize('change',['binding','frame','parent'])
def test_restart_changed_cleanup_evidence_never_authorizes_new_effect(configured,change):
    service,job=finished(configured);cleanup=request(job);service.cleanup(cleanup)
    row=service.db.execute('SELECT * FROM terminal_receipts WHERE command_id=?',(cleanup['command_id'],)).fetchone()
    if change=='binding':
        binding=json.loads(row['binding']);binding['resources'][0]['inode']+=1
        service.db.execute('UPDATE terminal_receipts SET binding=? WHERE command_id=?',(json.dumps(binding),cleanup['command_id']));service.db.commit()
    elif change=='frame':
        path=Path(row['root'])/('receipt-'+row['slot_id']+'.bin')
        with path.open('r+b') as stream:stream.seek(FRAME_BYTES+30);stream.write(b'torn')
    else:
        service.resources.rename(service.resources.with_name('old-resources'));service.resources.mkdir(mode=0o700)
    close(service);reopened=configured(slots=8)
    assert reopened._receipt_fault and reopened.backend.calls==0
    with pytest.raises(Rejected):reopened.cleanup(request(job,('workspace',)))


@pytest.mark.parametrize('change',[
    {'job_id':str(uuid.uuid4())},{'operation_kind':'job'},{'lease_identity':'a'*64},
    {'resources':[]},{'resource_root_identity':[True,1]}])
def test_cleanup_binding_variant_is_explicit_and_bounded(change):
    from hub_staging_service.terminal_receipt import _binding
    binding={'operation_kind':'cleanup','command_id':str(uuid.uuid4()),'job_id':None,
        'operation_identity':'a'*64,'worker_id':str(uuid.uuid4()),'journal_identity':[1,2],
        'lease_identity':None,'resource_root_identity':[1,3],'resources':[
            {'resource_id':str(uuid.uuid4()),'job_id':str(uuid.uuid4()),'kind':'preview','revision':1,'inode':4,'manifest_sha256':'b'*64}]}
    with pytest.raises(Rejected):_binding({**binding,**change})


def test_legacy_cleanup_uncertainty_veto_survives_restart_without_receipt_config(tmp_path,monkeypatch):
    root=tmp_path/'legacy';service=Service(root,{'demo':profile()},Backend())
    job=payload();service.submit(job);service.wait();cleanup=request(job)
    remove=Mock(side_effect=OSError(errno.ENOSPC,'legacy uncertain deletion'))
    monkeypatch.setattr(service,'_delete_cleanup_resource',remove)
    try:
        with pytest.raises(Rejected):service.cleanup(cleanup)
        assert service._receipt_fault and service.cleanup(cleanup)['state']=='held'
        remove.assert_called_once()
    finally:service.close()
    reopened=Service(root,{'demo':profile()},Backend())
    try:
        assert reopened._receipt_fault=='cleanup_recovery_required' and reopened.cleanup(cleanup)['state']=='held'
        with pytest.raises(Rejected):reopened.cleanup(request(job,('build',)))
        with pytest.raises(Rejected):reopened.cleanup_sources()
    finally:reopened.close()
