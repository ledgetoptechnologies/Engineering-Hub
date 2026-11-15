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
from tests.test_staging_service_scratch import payload, profile, prepare_job, synthetic_slot, Backend as ScratchBackend


class Backend:
    calls=0
    def run(self, paths, profile, command_id):
        self.calls+=1
        (paths['build']/'index.html').write_text('synthetic example')
        (paths['preview']/'index.html').write_text('synthetic example')
        return {'preview_ready':True,'stages':{'build':{'exit_code':0},'test':{'exit_code':0}}}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    from hub_staging_service import core
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
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
    service=configured();job=prepare_job(service,payload());record=TerminalReceipt.record_terminal;events=[]
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
    service=configured();job=prepare_job(service,payload());second=prepare_job(service,payload());service.db=FailTerminalCommit(service.db)
    service.submit(job);service.wait()
    assert service.db.failed and service.active is None and service._receipt_fault
    assert service.status(job['job_id'])['state']=='held'
    evidence=service.terminal_evidence(job['command_id'])
    assert evidence['terminal']['outcome']=='succeeded'  # observation does not promote SQL
    with pytest.raises(Rejected):service.submit(second)
    close(service);reopened=configured()
    assert reopened.status(job['job_id'])['state']=='held' and reopened._receipt_fault
    assert reopened.backend.calls==0 and reopened.terminal_evidence(job['command_id'])==evidence
    with pytest.raises(Rejected):reopened.submit(second)


def test_receipt_terminal_failure_latches_veto_clears_active_and_never_claims_success(configured,monkeypatch):
    service=configured();job=prepare_job(service,payload());second=prepare_job(service,payload())
    monkeypatch.setattr(TerminalReceipt,'record_terminal',Mock(side_effect=OSError('injected ENOSPC')))
    service.submit(job);service.wait()
    assert service.status(job['job_id'])['state']=='held' and service.active is None and service._receipt_fault
    assert service.terminal_evidence(job['command_id'])['state']=='uncertain'
    with pytest.raises(Rejected):service.submit(second)


def test_late_terminal_footer_fsync_failure_stays_uncertain_in_new_evidence_reader(configured,monkeypatch):
    service=configured();job=prepare_job(service,payload());actual_sync=os.fsync;record=TerminalReceipt.record_terminal
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


def source_request(item):
    import base64
    from hub_staging_service.source_provenance import _archive_tree, _object_id
    raw=base64.b64decode(item['source_zip_base64'])
    tree=_archive_tree(raw)[0]
    obj=(f'tree {tree}\nauthor Test <test@example.invalid> 1 +0000\n'
         f'committer Test <test@example.invalid> 1 +0000\n\nfixture\n').encode()
    return {'profile_protocol':'hub-staging-profile.v2', 'source_id':str(uuid.uuid4()),
        'project':item['project'], 'source_digest':item['source_digest'],
        'source_zip_base64':item['source_zip_base64'], 'source_commit':_object_id(b'commit',obj),
        'source_commit_object_base64':base64.b64encode(obj).decode()}


def cleanup_request(job,kind='preview'):
    return {'command_id':str(uuid.uuid4()),'idempotency_key':str(uuid.uuid4()),
            'resources':[{'resource_id':job['resource_ids'][kind],'expected_revision':1}]}


def test_sticky_fault_vetoes_new_source_cleanup_and_direct_gc_but_allows_original_reads(configured):
    service=configured();good_raw=payload();deposit=source_request(good_raw)
    # Preserve the original v1 receipt-retry assertion; v2 POSTs are no-replay.
    del deposit['profile_protocol']
    v2_deposit=source_request(payload());service.put_source(v2_deposit)
    good=prepare_job(service,good_raw);failed=prepare_job(service,payload())
    service.submit(good);service.wait()
    deposited=service.put_source(deposit)
    service.db.execute('UPDATE sources SET created=0 WHERE id=?',(deposit['source_id'],))
    service.db.execute('UPDATE source_bindings SET source_created=0 WHERE source_id=?',(deposit['source_id'],));service.db.commit()
    old_cleanup=cleanup_request(good,'build');old_result=service.cleanup(old_cleanup)
    service.db=FailTerminalCommit(service.db)
    service.submit(failed);service.wait();assert service._receipt_fault
    before=service.db.total_changes
    source_path=service.sources/(deposit['source_id']+'.zip')
    retained=service.resources/good['resource_ids']['preview']
    pending=cleanup_request(good)
    new_deposit={**deposit,'source_id':str(uuid.uuid4())}
    new_v2_deposit={**v2_deposit,'source_id':str(uuid.uuid4())}
    for action in (lambda:service.put_source(new_deposit),lambda:service.put_source(new_v2_deposit),lambda:service.cleanup(pending),
                   lambda:service.cleanup_sources(now=10**12)):
        with pytest.raises(Rejected,match='terminal_receipt_recovery_required'):action()
    assert source_path.is_file() and retained.is_dir() and service.db.total_changes==before
    assert service.put_source(deposit)==deposited
    assert service.source_attempt(v2_deposit['source_id'], 'demo')['state']=='historical_observation'
    with pytest.raises(Rejected, match='terminal_receipt_recovery_required'):
        service.put_source(v2_deposit)
    assert service.db.total_changes==before
    assert service.cleanup(old_cleanup)==old_result
    assert service.submit(good)['state']=='succeeded'
    assert service.status(failed['job_id'])['state']=='held'
    assert service.terminal_evidence(failed['command_id'])['terminal']['outcome']=='succeeded'
    assert service.db.total_changes==before


def test_source_attempt_and_concurrent_fault_are_ordered_under_same_worker_lock(configured, monkeypatch):
    import threading
    service = configured()
    source = source_request(payload())
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    real_lock = service.lock
    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == 'synthetic-fault':
                waiting.set()
            real_lock.acquire()
            return self
        def __exit__(self, *args):
            real_lock.release()
    service.lock = ObservedLock()
    original = service._legacy_put_source
    def blocked_deposit(value, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(value, **kwargs)
    monkeypatch.setattr(service, '_legacy_put_source', blocked_deposit)
    monkeypatch.setattr(service, '_execute_operation', lambda *args: (_ for _ in ()).throw(OSError('synthetic fault')))
    results, errors = [], []
    def submit_source():
        try:
            results.append(service.put_source(source))
        except Exception as exc:
            errors.append(exc)
    source_thread = threading.Thread(target=submit_source, name='synthetic-source')
    fault_thread = threading.Thread(target=service._execute, args=({'command_id': str(uuid.uuid4())}, b''), name='synthetic-fault')
    try:
        source_thread.start()
        assert entered.wait(5)
        fault_thread.start()
        assert waiting.wait(5)
        # The exception occurred, but its fault latch waits behind the already
        # admitted source attempt; it cannot change during that effect.
        assert service._receipt_fault is None
        release.set()
        source_thread.join(5); fault_thread.join(5)
        assert not source_thread.is_alive() and not fault_thread.is_alive()
        assert errors == [] and len(results) == 1
        assert results[0]['state'] == 'historical_observation'
        assert service._receipt_fault == 'terminal_receipt_persistence_failed'
    finally:
        release.set()
        if source_thread.ident is not None: source_thread.join(5)
        if fault_thread.ident is not None: fault_thread.join(5)


def test_backend_oserror_latch_waits_for_admitted_source_attempt(configured, monkeypatch):
    import threading
    service = configured()
    backend_entered, backend_fail = threading.Event(), threading.Event()
    source_entered, source_release, waiting = threading.Event(), threading.Event(), threading.Event()
    class FailingBackend:
        def run(self, paths, profile, command_id):
            backend_entered.set()
            assert backend_fail.wait(5)
            raise OSError('synthetic backend failure')
    service.backend = FailingBackend()
    original_lock = service.lock
    class ObservedLock:
        def __enter__(self):
            if backend_fail.is_set() and threading.current_thread().name == 'synthetic-backend':
                waiting.set()
            original_lock.acquire()
            return self
        def __exit__(self, *args):
            original_lock.release()
    service.lock = ObservedLock()
    submitted = prepare_job(service, payload())
    service.submit(submitted)
    assert backend_entered.wait(5)
    worker_thread = service.thread
    worker_thread.name = 'synthetic-backend'
    original = service._legacy_put_source
    def blocked_deposit(value, **kwargs):
        source_entered.set()
        assert source_release.wait(5)
        return original(value, **kwargs)
    monkeypatch.setattr(service, '_legacy_put_source', blocked_deposit)
    results, errors = [], []
    def send_source():
        try:
            results.append(service.put_source(source_request(payload())))
        except Exception as exc:
            errors.append(exc)
    source_thread = threading.Thread(target=send_source, name='synthetic-source')
    try:
        source_thread.start()
        assert source_entered.wait(5)
        backend_fail.set()
        assert waiting.wait(5)
        assert service._receipt_fault is None
        source_release.set()
        source_thread.join(5); worker_thread.join(5)
        assert not source_thread.is_alive() and not worker_thread.is_alive()
        assert errors == [] and len(results) == 1
        assert results[0]['state'] == 'historical_observation'
        assert service._receipt_fault == 'terminal_receipt_persistence_failed'
        assert service.status(submitted['job_id'])['state'] == 'held'
    finally:
        backend_fail.set(); source_release.set()
        if source_thread.ident is not None: source_thread.join(5)
        worker_thread.join(5)


def test_removed_configuration_persisted_optin_vetoes_all_new_mutations(configured):
    service=configured();close(service);disabled=configured(configure=False)
    assert disabled._receipt_fault
    job=payload();deposit=source_request(job)
    before=disabled.db.total_changes
    for action in (lambda:disabled.put_source(deposit),lambda:disabled.cleanup(cleanup_request(job)),
                   lambda:disabled.cleanup_sources(now=10**12)):
        with pytest.raises(Rejected,match='terminal_receipt_recovery_required'):action()
    assert disabled.db.total_changes==before and not list(disabled.sources.iterdir())


def test_thread_start_failure_retains_bound_operation_and_slot(configured,monkeypatch):
    service=configured();job=prepare_job(service,payload())
    monkeypatch.setattr('hub_staging_service.core.threading.Thread.start',Mock(side_effect=RuntimeError('injected')))
    with pytest.raises(Rejected,match='worker_start_failed'):service.submit(job)
    assert service.active is None and service.thread is None and service.backend.calls==0
    assert service.status(job['job_id'])['state']=='held' and service._receipt_fault
    assert service.terminal_evidence(job['command_id'])['state']=='uncertain'


def test_admission_commit_failure_leaves_orphan_slot_and_cannot_be_bypassed_by_disabling(configured):
    service=configured();job=prepare_job(service,payload());second=prepare_job(service,payload())
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
    with pytest.raises(Rejected,match='terminal_receipt_recovery_required'):reopened.submit(second)
    close(reopened);restored=configured()
    assert restored._receipt_fault and restored.backend.calls==0


def test_prepare_failure_is_not_silently_ignored(configured,monkeypatch):
    service=configured();job=prepare_job(service,payload());second=prepare_job(service,payload());monkeypatch.setattr(TerminalReceipt,'prepare',Mock(side_effect=OSError('injected')))
    with pytest.raises(OSError):service.submit(job)
    assert service._receipt_fault and service.backend.calls==0
    with pytest.raises(Rejected):service.submit(second)


def test_slot_cap_denies_without_recycling_previous_receipts(configured):
    service=configured(slots=1);job=prepare_job(service,payload());second=prepare_job(service,payload());service.submit(job);service.wait()
    root=service._receipt_config['path'];before={p.name:p.read_bytes() for p in root.iterdir()}
    with pytest.raises(Rejected,match='terminal_receipt_slot_limit'):service.submit(second)
    assert service.backend.calls==1 and {p.name:p.read_bytes() for p in root.iterdir()}==before


@pytest.mark.parametrize('tamper',['missing','foreign','root_symlink','corrupt'])
def test_restart_unknown_or_tampered_receipt_never_runs(configured,tamper):
    service=configured();job=prepare_job(service,payload());second=prepare_job(service,payload());service.submit(job);service.wait()
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
        with pytest.raises(Rejected):reopened.submit(second)


def test_terminal_receipt_never_bypasses_sql_or_scratch_absence(configured,synthetic_slot,monkeypatch):
    service=configured(backend=ScratchBackend(),slot=synthetic_slot);job=prepare_job(service,payload())
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
    service=configured(backend=ScratchBackend(),slot=synthetic_slot);job=prepare_job(service,payload())
    service.db=FailTerminalCommit(service.db)
    released=Mock();monkeypatch.setattr(ScratchLease,'release',released)
    service.submit(job);service.wait();released.assert_not_called()
    assert service.db.execute('SELECT state FROM scratch_leases').fetchone()[0]=='occupied'
    assert service.status(job['job_id'])['state']=='held' and service.active is None


@pytest.mark.parametrize('change',[('failed',None), (None,{'forged':'result'}), (None,{'preview_ready':1})])
def test_restart_terminal_receipt_sql_outcome_or_result_mismatch_blocks(configured,change):
    service=configured();job=prepare_job(service,payload());second=prepare_job(service,payload());service.submit(job);service.wait()
    state,result=change
    if state:service.db.execute('UPDATE operations SET state=? WHERE command_id=?',(state,job['command_id']))
    if result is not None:service.db.execute('UPDATE operations SET result=? WHERE command_id=?',(json.dumps(result),job['command_id']))
    service.db.commit();close(service)
    reopened=configured();assert reopened._receipt_fault and reopened.backend.calls==0
    with pytest.raises(Rejected):reopened.submit(second)


def test_legacy_unconfigured_service_still_executes_without_receipts(tmp_path,monkeypatch):
    from hub_staging_service import core
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service=Service(tmp_path/'worker',{'demo':profile()},Backend())
    try:
        job=prepare_job(service,payload());service.submit(job);service.wait()
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
