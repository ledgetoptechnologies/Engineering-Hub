"""Real private receipt/SQLite integration with synthetic source execution."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid
from unittest.mock import Mock

import pytest

from hub_staging_service.core import Service, Rejected, terminal_receipt_config
from hub_staging_service.terminal_receipt import TerminalReceipt
from hub_staging_service.scratch import ScratchLease
from tests.test_staging_service_scratch import payload, profile, synthetic_slot, Backend as ScratchBackend


class Backend:
    calls=0
    def run(self, paths, profile, command_id):
        self.calls+=1
        (paths['build']/'index.html').write_text('synthetic example')
        (paths['preview']/'index.html').write_text('synthetic example')
        return {'preview_ready':True,'stages':{'build':{'exit_code':0},'test':{'exit_code':0}}}


@pytest.fixture
def configured(tmp_path):
    if os.name!='posix':pytest.skip('Actual POSIX private receipt writes/fsync')
    services=[]
    def create(*,backend=None,slots=4,configure=True,slot=None):
        root=tmp_path/'worker'
        config={'path':str(root/'private-receipts'),'max_slots':slots}
        service=Service(root,{'demo':profile()},backend or Backend(),
            terminal_receipts=config if configure else None,scratch_slot=slot)
        services.append(service)
        return service
    yield create
    for service in services:
        if service.db is not None:
            service.close();service.db=None


def close(service):
    service.close();service.db=None


def test_success_receipt_precedes_sql_terminal_and_survives_restart(configured,monkeypatch):
    service=configured();job=payload();record=TerminalReceipt.record_terminal;events=[]
    def terminal(receipt,original,**kwargs):
        row=service.db.execute('SELECT state FROM operations WHERE command_id=?',(job['command_id'],)).fetchone()
        assert row['state']=='running'
        events.append('receipt');return record(receipt,original,**kwargs)
    monkeypatch.setattr(TerminalReceipt,'record_terminal',terminal)
    service.submit(job);service.wait()
    assert service.status(job['job_id'])['state']=='succeeded' and events==['receipt']
    before=service.db.total_changes
    evidence=service.terminal_evidence(job['command_id'])
    assert evidence['authority']=='evidence_only' and evidence['terminal']['runtime'] is None
    assert service.db.total_changes==before and service.active is None
    row=service.db.execute('SELECT * FROM terminal_receipts').fetchone()
    binding=json.loads(row['binding'])
    assert binding['operation_identity']==hashlib.sha256(json.dumps(job,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert binding['lease_identity'] is None
    close(service);reopened=configured()
    assert reopened._receipt_fault is None
    assert reopened.terminal_evidence(job['command_id'])==evidence
    assert reopened.backend.calls==0


class FailTerminalCommit:
    """One real SQLite terminal-commit failure; all other writes remain real."""
    def __init__(self,db):self.db=db;self.terminal=False;self.failed=False
    def __getattr__(self,key):return getattr(self.db,key)
    def execute(self,sql,parameters=()):
        if sql.startswith('UPDATE operations SET state=?,result=?') and parameters[0]=='succeeded':self.terminal=True
        return self.db.execute(sql,parameters)
    def commit(self):
        if self.terminal and not self.failed:
            self.failed=True;raise sqlite3.OperationalError('database or disk is full')
        return self.db.commit()


def test_terminal_sql_failure_preserves_receipt_and_blocks_new_admission_after_restart(configured):
    service=configured();job=payload();service.db=FailTerminalCommit(service.db)
    service.submit(job);service.wait()
    assert service.db.failed and service.active is None and service._receipt_fault
    assert service.status(job['job_id'])['state']=='held'
    evidence=service.terminal_evidence(job['command_id'])
    assert evidence['terminal']['outcome']=='succeeded'  # observation does not promote SQL
    with pytest.raises(Rejected):service.submit(payload())
    close(service);reopened=configured()
    assert reopened.status(job['job_id'])['state']=='held' and reopened._receipt_fault
    assert reopened.backend.calls==0 and reopened.terminal_evidence(job['command_id'])==evidence
    with pytest.raises(Rejected):reopened.submit(payload())


def test_receipt_terminal_failure_latches_veto_clears_active_and_never_claims_success(configured,monkeypatch):
    service=configured();job=payload()
    monkeypatch.setattr(TerminalReceipt,'record_terminal',Mock(side_effect=OSError('injected ENOSPC')))
    service.submit(job);service.wait()
    assert service.status(job['job_id'])['state']=='held' and service.active is None and service._receipt_fault
    assert service.terminal_evidence(job['command_id'])['state']=='uncertain'
    with pytest.raises(Rejected):service.submit(payload())


def test_late_terminal_footer_fsync_failure_stays_uncertain_in_new_evidence_reader(configured,monkeypatch):
    service=configured();job=payload();actual_sync=os.fsync;record=TerminalReceipt.record_terminal
    active=[False];syncs=[0]
    def sync(fd):
        if active[0]:
            syncs[0]+=1
            if syncs[0]==2:raise OSError('injected fsync failure AFTER complete footer write')
        return actual_sync(fd)
    def terminal(*args,**kwargs):
        active[0]=True
        try:return record(*args,**kwargs)
        finally:active[0]=False
    monkeypatch.setattr(os,'fsync',sync)
    monkeypatch.setattr(TerminalReceipt,'record_terminal',terminal)
    service.submit(job);service.wait()
    assert syncs[0]==2 and service.active is None and service._receipt_fault
    assert service.status(job['job_id'])['state']=='held'
    assert service.terminal_evidence(job['command_id'])=={'state':'uncertain','reason':'receipt_persistence_failed'}
    row=service.db.execute('SELECT * FROM terminal_receipts').fetchone()
    # Valid bytes really ARE present, so a newly opened primitive lacks the
    # live I/O-error history. Service must preserve that negative knowledge.
    fresh=TerminalReceipt(row['root'],row['slot_id'],expected_capture=json.loads(row['capture']))
    assert fresh.evidence(json.loads(row['binding']))['state']=='observed'


def cleanup_request(job,kind='preview'):
    return {'command_id':str(uuid.uuid4()),'idempotency_key':str(uuid.uuid4()),
            'resources':[{'resource_id':job['resource_ids'][kind],'expected_revision':1}]}


def test_sticky_fault_vetoes_new_source_cleanup_and_direct_gc_but_allows_original_reads(configured):
    service=configured();good=payload();service.submit(good);service.wait()
    deposit={'source_id':str(uuid.uuid4()),'project':good['project'],
        'source_digest':good['source_digest'],'source_zip_base64':good['source_zip_base64']}
    deposited=service.put_source(deposit)
    service.db.execute('UPDATE sources SET created=0');service.db.commit()
    old_cleanup=cleanup_request(good,'build');old_result=service.cleanup(old_cleanup)
    service.db=FailTerminalCommit(service.db)
    failed=payload();service.submit(failed);service.wait();assert service._receipt_fault
    before=service.db.total_changes
    source_path=service.sources/(deposit['source_id']+'.zip')
    retained=service.resources/good['resource_ids']['preview']
    pending=cleanup_request(good)
    new_deposit={**deposit,'source_id':str(uuid.uuid4())}
    for action in (lambda:service.put_source(new_deposit),lambda:service.cleanup(pending),
                   lambda:service.cleanup_sources(now=10**12)):
        with pytest.raises(Rejected,match='terminal_receipt_recovery_required'):action()
    assert source_path.is_file() and retained.is_dir() and service.db.total_changes==before
    assert service.put_source(deposit)==deposited
    assert service.cleanup(old_cleanup)==old_result
    assert service.submit(good)['state']=='succeeded'
    assert service.status(failed['job_id'])['state']=='held'
    assert service.terminal_evidence(failed['command_id'])['terminal']['outcome']=='succeeded'
    assert service.db.total_changes==before


def test_removed_configuration_persisted_optin_vetoes_all_new_mutations(configured):
    service=configured();close(service);disabled=configured(configure=False)
    assert disabled._receipt_fault
    job=payload();deposit={'source_id':str(uuid.uuid4()),'project':job['project'],
        'source_digest':job['source_digest'],'source_zip_base64':job['source_zip_base64']}
    before=disabled.db.total_changes
    for action in (lambda:disabled.put_source(deposit),lambda:disabled.cleanup(cleanup_request(job)),
                   lambda:disabled.cleanup_sources(now=10**12)):
        with pytest.raises(Rejected,match='terminal_receipt_recovery_required'):action()
    assert disabled.db.total_changes==before and not list(disabled.sources.iterdir())


def test_thread_start_failure_retains_bound_operation_and_slot(configured,monkeypatch):
    service=configured();job=payload()
    monkeypatch.setattr('hub_staging_service.core.threading.Thread.start',Mock(side_effect=RuntimeError('injected')))
    with pytest.raises(Rejected,match='worker_start_failed'):service.submit(job)
    assert service.active is None and service.thread is None and service.backend.calls==0
    assert service.status(job['job_id'])['state']=='held' and service._receipt_fault
    assert service.terminal_evidence(job['command_id'])['state']=='uncertain'


def test_admission_commit_failure_leaves_orphan_slot_and_cannot_be_bypassed_by_disabling(configured):
    service=configured();job=payload()
    class FailAdmission:
        def __getattr__(self,key):return getattr(service._real_db,key)
        def commit(self):raise sqlite3.OperationalError('injected admission disk full')
    service._real_db=service.db;service.db=FailAdmission()
    with pytest.raises(sqlite3.OperationalError):service.submit(job)
    assert service._receipt_fault and service.backend.calls==0
    assert service.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
    assert len(list(service._receipt_config['path'].iterdir()))==1
    service.db=service._real_db;close(service)
    reopened=configured(configure=False)
    assert reopened._receipt_fault
    with pytest.raises(Rejected,match='terminal_receipt_recovery_required'):reopened.submit(payload())
    close(reopened);restored=configured()
    assert restored._receipt_fault and restored.backend.calls==0


def test_prepare_failure_is_not_silently_ignored(configured,monkeypatch):
    service=configured();monkeypatch.setattr(TerminalReceipt,'prepare',Mock(side_effect=OSError('injected')))
    with pytest.raises(OSError):service.submit(payload())
    assert service._receipt_fault and service.backend.calls==0
    with pytest.raises(Rejected):service.submit(payload())


def test_slot_cap_denies_without_recycling_previous_receipts(configured):
    service=configured(slots=1);job=payload();service.submit(job);service.wait()
    root=service._receipt_config['path'];before={p.name:p.read_bytes() for p in root.iterdir()}
    with pytest.raises(Rejected,match='terminal_receipt_slot_limit'):service.submit(payload())
    assert service.backend.calls==1 and {p.name:p.read_bytes() for p in root.iterdir()}==before


@pytest.mark.parametrize('tamper',['missing','foreign','root_symlink','corrupt'])
def test_restart_unknown_or_tampered_receipt_never_runs(configured,tamper):
    service=configured();job=payload();service.submit(job);service.wait()
    root=service._receipt_config['path'];file=next(root.iterdir());close(service)
    if tamper=='missing':file.unlink()
    elif tamper=='foreign':(root/'foreign').write_text('unknown')
    elif tamper=='root_symlink':
        moved=root.with_name('moved');root.rename(moved);root.symlink_to(moved,target_is_directory=True)
    else:
        with file.open('r+b') as stream:stream.seek(65536);stream.write(b'torn')
    if tamper=='root_symlink':
        with pytest.raises(Rejected):configured()
    else:
        reopened=configured();assert reopened._receipt_fault and reopened.backend.calls==0
        with pytest.raises(Rejected):reopened.submit(payload())


def test_terminal_receipt_never_bypasses_sql_or_scratch_absence(configured,synthetic_slot,monkeypatch):
    service=configured(backend=ScratchBackend(),slot=synthetic_slot);job=payload()
    release=ScratchLease.release;events=[]
    def released(lease):
        assert service.status(job['job_id'])['state']=='succeeded'
        assert service.terminal_evidence(job['command_id'])['terminal']['outcome']=='succeeded'
        events.append('released');return release(lease)
    monkeypatch.setattr(ScratchLease,'release',released)
    service.submit(job);service.wait()
    assert events==['released'] and service.status(job['job_id'])['state']=='succeeded'
    binding=json.loads(service.db.execute('SELECT binding FROM terminal_receipts').fetchone()[0])
    assert len(binding['lease_identity'])==64


def test_terminal_sql_failure_never_calls_scratch_release(configured,synthetic_slot,monkeypatch):
    service=configured(backend=ScratchBackend(),slot=synthetic_slot);job=payload()
    service.db=FailTerminalCommit(service.db)
    released=Mock();monkeypatch.setattr(ScratchLease,'release',released)
    service.submit(job);service.wait();released.assert_not_called()
    assert service.db.execute('SELECT state FROM scratch_leases').fetchone()[0]=='occupied'
    assert service.status(job['job_id'])['state']=='held' and service.active is None


@pytest.mark.parametrize('change',[('failed',None), (None,{'forged':'result'}), (None,{'preview_ready':1})])
def test_restart_terminal_receipt_sql_outcome_or_result_mismatch_blocks(configured,change):
    service=configured();job=payload();service.submit(job);service.wait()
    state,result=change
    if state:service.db.execute('UPDATE operations SET state=? WHERE command_id=?',(state,job['command_id']))
    if result is not None:service.db.execute('UPDATE operations SET result=? WHERE command_id=?',(json.dumps(result),job['command_id']))
    service.db.commit();close(service)
    reopened=configured();assert reopened._receipt_fault and reopened.backend.calls==0
    with pytest.raises(Rejected):reopened.submit(payload())


def test_legacy_unconfigured_service_still_executes_without_receipts(tmp_path):
    service=Service(tmp_path/'worker',{'demo':profile()},Backend())
    try:
        job=payload();service.submit(job);service.wait()
        assert service.status(job['job_id'])['state']=='succeeded' and service._receipt_fault is None
        assert not service.db.execute('SELECT 1 FROM terminal_receipts').fetchone()
    finally:service.close()


@pytest.mark.parametrize('change',[{'path':'/outside'}, {'max_slots':True}, {'max_slots':257}, {'extra':'source'}])
def test_static_configuration_is_bounded_and_private(tmp_path,change):
    root=tmp_path/'worker';config={'path':str(root/'private-receipts'),'max_slots':4}
    with pytest.raises(Rejected):terminal_receipt_config({**config,**change},root)


def test_cli_validates_optional_receipt_config_without_initializing_service(tmp_path,monkeypatch):
    from hub_staging_service.__main__ import main
    from tests.test_staging_service_http import StorageConfigTests
    config=StorageConfigTests().configuration(tmp_path)
    config['terminal_receipts']={'path':str(Path(config['worker_root'])/'private-receipts'),'max_slots':4}
    file=tmp_path/'operator.json';file.write_text(json.dumps(config))
    monkeypatch.setattr(sys,'argv',['worker','--config',str(file),'--check-config'])
    monkeypatch.setattr('hub_staging_service.__main__.credentials',lambda _: 'synthetic-only-token')
    service=Mock();monkeypatch.setattr('hub_staging_service.__main__.Service',service)
    main();service.assert_not_called()
    config['terminal_receipts']['max_slots']=True;file.write_text(json.dumps(config))
    with pytest.raises(Rejected):main()
    service.assert_not_called()
