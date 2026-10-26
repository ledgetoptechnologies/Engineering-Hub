"""Synthetic OS facts and real SQLite lifecycle; not quota acceptance."""
import base64
import hashlib
import io
import json
import os
import stat
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid
import zipfile

import pytest

from hub_staging_service.core import Service, Rejected
from hub_staging_service.scratch import ScratchSlot, ScratchLease, filesystem_facts
import hub_staging_service.scratch as scratch


def config(path='/scratch'):
    return dict(path=path, filesystem_uuid='e5742e20-3dc9-4df5-92b0-8699ebc3400c',
                mount_device='8:9', ceiling_bytes=2*1024**3, ceiling_inodes=100000,
                free_floor_bytes=512*1024**2, free_floor_inodes=1000,
                container_uid=10001, container_gid=10001, mapped_uid=210000, mapped_gid=210000)


def payload():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('index.html', 'source')
    raw = stream.getvalue()
    return dict(command_id=str(uuid.uuid4()), job_id=str(uuid.uuid4()), idempotency_key=str(uuid.uuid4()),
                project='demo', recipe='default', source_commit='a'*40,
                source_digest=hashlib.sha256(raw).hexdigest(), source_zip_base64=base64.b64encode(raw).decode(),
                resource_ids={kind: str(uuid.uuid4()) for kind in ('workspace','build','test_run','preview')})


def profile():
    return dict(image_id='sha256:'+'b'*64, build_argv=['fixed-build'], test_argv=['fixed-test'],
                timeout_seconds=30, memory_mb=128, cpus=1, pids=32,
                output_bytes=1024*1024, log_bytes=65536)


@pytest.fixture
def synthetic_slot(tmp_path, monkeypatch):
    root = tmp_path/'scratch'
    root.mkdir(mode=0o700)
    slot = ScratchSlot(**{**config(), 'path': root})
    facts = {'mount_id': 42, 'root_identity': [root.stat().st_dev, root.stat().st_ino]}
    def verify(self, captured=None, *, admission=True):
        if captured is not None:
            assert captured == facts
        return facts.copy()
    monkeypatch.setattr(ScratchSlot, 'verify', verify)
    monkeypatch.setattr(ScratchSlot, 'acquire', lambda self, worker_root, db: None)
    monkeypatch.setattr(ScratchSlot, 'bind_journal', lambda *args: None)
    monkeypatch.setattr(ScratchSlot, 'recheck_journal', lambda *args: None)
    # Native Windows cannot express Linux0777/device semantics; this fixture
    # deliberately substitutes only the OS identity verifier, not the journal.
    def lease_verify(self, *, admission=True):
        assert self.path.is_dir() and not self.path.is_symlink()
        assert [self.path.stat().st_dev,self.path.stat().st_ino] == self._record['directory_identity']
        return facts.copy()
    monkeypatch.setattr(ScratchLease, 'verify', lease_verify)
    return slot


class Backend:
    def __init__(self, proof=True, fail=False):
        self.proof, self.fail, self.calls = proof, fail, 0
    def run(self, paths, profile, command_id, *, scratch_lease=None):
        self.calls += 1
        assert scratch_lease.command_id == command_id
        assert scratch_lease.path.is_dir()
        (scratch_lease.path/'temporary').write_text('temporary')
        if self.proof:
            # Simulated mapped-UID helper's completed sweep, not quota proof.
            (scratch_lease.path/'temporary').unlink()
            scratch_lease.confirm_backend_absent(command_id)
        if self.fail:
            raise Rejected('stage_test_failed')
        return {'preview_ready': False}


@pytest.mark.parametrize('change', [dict(path='/'), dict(path='/scratch/../host'), dict(path='/a//b'),
    dict(ceiling_bytes=True), dict(mapped_uid=0), dict(free_floor_inodes=100000),
    dict(filesystem_uuid='invalid'), dict(mount_device='device'), dict(extra='hostpath')])
def test_synthetic_closed_operator_config(change):
    with pytest.raises((Rejected, ValueError)):
        ScratchSlot.from_config({**config(), **change})


def test_config_preserves_fixed_operator_mapping():
    slot = ScratchSlot.from_config(config())
    assert slot.path == Path('/scratch') and slot.mapped_uid == 210000


def test_synthetic_mountinfo_whole_filesystem_and_real_statvfs_fields(monkeypatch):
    monkeypatch.setattr(os, 'name', 'posix')
    monkeypatch.setattr(Path, 'read_text', lambda self: '42 1 8:9 / /scratch rw,nosuid,nodev - ext4 /dev/test rw\n')
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(f_blocks=100, f_frsize=4096,
        f_bavail=50, f_files=1000, f_favail=900, f_fsid=123), raising=False)
    facts = filesystem_facts(Path('/scratch'))
    assert facts['mount_root'] == '/' and facts['capacity_bytes'] == 409600
    assert facts['free_bytes'] == 204800 and facts['capacity_inodes'] == 1000
    assert facts['filesystem_id'] == 123


def test_synthetic_unmounted_folder_rejected(monkeypatch):
    monkeypatch.setattr(os, 'name', 'posix')
    monkeypatch.setattr(Path, 'read_text', lambda self: '42 1 8:9 / /elsewhere rw - ext4 /dev/test rw\n')
    with pytest.raises(Rejected, match='scratch_dedicated_mount_required'):
        filesystem_facts(Path('/scratch'))


def test_synthetic_acceptance_and_reservation_rollback_together(tmp_path, synthetic_slot, monkeypatch):
    service = Service(tmp_path/'worker', {'demo':profile()}, Backend(), scratch_slot=synthetic_slot)
    original = ScratchSlot.reserve
    def reserve_then_fail(self, *args):
        original(self, *args)
        raise Rejected('injected_after_lease_insert')
    monkeypatch.setattr(ScratchSlot, 'reserve', reserve_then_fail)
    try:
        with pytest.raises(Rejected):
            service.submit(payload())
        assert service.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        assert service.db.execute('SELECT count(*) FROM scratch_leases').fetchone()[0] == 0
        assert list(synthetic_slot.path.iterdir()) == []
    finally:
        service.close()


@pytest.mark.parametrize('failure', [False, True])
def test_synthetic_release_after_terminal_receipt_only(tmp_path, synthetic_slot, monkeypatch, failure):
    # Directory fsync/rmdir are Linux details, separately live-gated.
    monkeypatch.setattr(os, 'O_DIRECTORY', 0, raising=False)
    monkeypatch.setattr(os, 'O_NOFOLLOW', 0, raising=False)
    original_release = ScratchLease.release
    def release(self):
        row = self._db.execute('SELECT state FROM operations WHERE command_id=?',(self.command_id,)).fetchone()
        assert row['state'] == ('failed' if failure else 'succeeded')
        assert self._db.in_transaction is False
        # Exercise transaction ordering without pretending Windows directory
        # fsync/dirfd-rmdir is Linux acceptance.
        self._db.execute("UPDATE scratch_leases SET state='released',receipt=? WHERE command_id=?",
                         (json.dumps({'absent':True}),self.command_id))
        self._db.commit()
    monkeypatch.setattr(ScratchLease, 'release', release)
    service = Service(tmp_path/'worker', {'demo':profile()}, Backend(fail=failure), scratch_slot=synthetic_slot)
    job = payload()
    try:
        service.submit(job); service.wait()
        assert service.status(job['job_id'])['state'] == ('failed' if failure else 'succeeded')
        assert service.db.execute('SELECT state FROM scratch_leases').fetchone()[0] == 'released'
        assert service.submit(job) == service.status(job['job_id'])
        assert service.backend.calls == 1
    finally:
        service.close()


def test_synthetic_missing_absence_holds_and_restart_without_config_cannot_bypass(tmp_path, synthetic_slot):
    root = tmp_path/'worker'
    service = Service(root, {'demo':profile()}, Backend(proof=False), scratch_slot=synthetic_slot)
    job = payload()
    service.submit(job); service.wait()
    assert service.status(job['job_id'])['state'] == 'held'
    assert service.db.execute('SELECT state FROM scratch_leases').fetchone()[0] == 'occupied'
    service.close()
    restarted = Service(root, {'demo':profile()}, Backend())
    try:
        assert restarted.status(job['job_id'])['state'] == 'held'
        with pytest.raises(Rejected, match='worker_busy_or_held'):
            restarted.submit(payload())
        assert restarted.submit(job)['state'] == 'held'
        assert restarted.backend.calls == 0
    finally:
        restarted.close()


def test_synthetic_cleanup_failure_retains_exclusive_lease(tmp_path, synthetic_slot, monkeypatch):
    monkeypatch.setattr(ScratchLease, 'release', lambda self: (_ for _ in ()).throw(OSError('injected fsync failure')))
    service = Service(tmp_path/'worker', {'demo':profile()}, Backend(), scratch_slot=synthetic_slot)
    job=payload()
    try:
        service.submit(job); service.wait()
        assert service.status(job['job_id'])['result']['error'] == 'scratch_cleanup_uncertain'
        assert service.status(job['job_id'])['state'] == 'held'
        assert service.db.execute('SELECT state FROM scratch_leases').fetchone()[0] == 'occupied'
        with pytest.raises(Rejected):
            service.submit(payload())
    finally:
        service.close()


def test_synthetic_wrong_command_never_confirms_absence(tmp_path, synthetic_slot):
    service = Service(tmp_path/'worker', {'demo':profile()}, Backend(proof=False), scratch_slot=synthetic_slot)
    job=payload()
    try:
        service.submit(job); service.wait()
        lease=synthetic_slot.lease(service.db,job['command_id'])
        with pytest.raises(Rejected, match='scratch_command_drift'):
            lease.confirm_backend_absent(str(uuid.uuid4()))
        assert not lease.backend_absent
        with pytest.raises(Rejected, match='scratch_backend_absence_unproven'):
            lease.release()
    finally:
        service.close()


def verify_injected_snapshot(monkeypatch, *, changes=None, info_changes=None, uuid_device=2057,
                             captured=None, admission=True, open_failure=False):
    """Only injected observations; cannot attest Linux mounts or actual quotas."""
    facts = dict(mount_id=42, mount_root='/', mount_device='8:9', filesystem='ext4',
                 options=['rw','nosuid','nodev'], capacity_bytes=1024**3, free_bytes=700*1024**2,
                 capacity_inodes=50000, free_inodes=2000, block_size=4096, blocks=262144,
                 filesystem_id=123)
    facts.update(changes or {})
    info = dict(st_dev=2057, st_ino=88, st_uid=10001, st_mode=stat.S_IFDIR|0o700)
    info.update(info_changes or {})
    closed=[]
    slot=ScratchSlot.from_config(config())
    with monkeypatch.context() as patch:
        patch.setattr(os, 'O_DIRECTORY', 65536, raising=False)
        patch.setattr(os, 'O_NOFOLLOW', 131072, raising=False)
        patch.setattr(os, 'getuid', lambda:10001, raising=False)
        patch.setattr(os, 'makedev', lambda a,b:2057, raising=False)
        def opened(path, flags, **kwargs):
            if path != '/':
                assert flags & os.O_NOFOLLOW
                if open_failure:
                    raise OSError('synthetic symlink/ELOOP')
            return 100+len(closed)
        patch.setattr(os,'open',opened)
        patch.setattr(os,'close',closed.append)
        patch.setattr(os,'fstat',lambda fd:SimpleNamespace(**info))
        patch.setattr(Path,'stat',lambda self,**kw:SimpleNamespace(**info) if str(self)==str(slot.path)
                      else SimpleNamespace(st_mode=stat.S_IFBLK,st_rdev=uuid_device))
        patch.setattr(scratch,'filesystem_facts',lambda path:facts.copy())
        result=slot.verify(captured,admission=admission)
    assert len(closed)==2
    return result


@pytest.mark.parametrize('changes', [dict(mount_root='/subdir'),dict(filesystem='tmpfs'),dict(mount_device='8:10'),
    dict(options=['rw','nosuid','nodev','noexec']),dict(options=['rw','nodev']),
    dict(capacity_bytes=3*1024**3),dict(capacity_inodes=100001),dict(capacity_bytes=0),
    dict(free_bytes=1),dict(free_inodes=1)])
def test_synthetic_admission_rejects_wrong_boundary_or_exhaustion(monkeypatch,changes):
    with pytest.raises(Rejected):
        verify_injected_snapshot(monkeypatch,changes=changes)


@pytest.mark.parametrize('changes',[dict(st_uid=7),dict(st_mode=stat.S_IFDIR|0o777),dict(st_dev=99)])
def test_synthetic_owner_mode_device_admission(monkeypatch,changes):
    with pytest.raises(Rejected):
        verify_injected_snapshot(monkeypatch,info_changes=changes)


def test_synthetic_uuid_alias_and_symlink_open_are_not_accepted(monkeypatch):
    with pytest.raises(Rejected,match='scratch_uuid_drift'):
        verify_injected_snapshot(monkeypatch,uuid_device=99)
    with pytest.raises(OSError):
        verify_injected_snapshot(monkeypatch,open_failure=True)


def test_synthetic_cleanup_exhaustion_does_not_weaken_identity(monkeypatch):
    captured=verify_injected_snapshot(monkeypatch)
    assert verify_injected_snapshot(monkeypatch,captured=captured,
        changes=dict(free_bytes=0,free_inodes=0),admission=False)==captured
    with pytest.raises(Rejected,match='scratch_filesystem_drift'):
        verify_injected_snapshot(monkeypatch,captured=captured,changes=dict(mount_id=99),admission=False)


def test_synthetic_materialized_binding_cannot_change_resource_ids(tmp_path,synthetic_slot):
    service=Service(tmp_path/'worker',{'demo':profile()},Backend(proof=False),scratch_slot=synthetic_slot)
    job=payload()
    try:
        service.submit(job);service.wait()
        row=service.db.execute('SELECT payload FROM operations').fetchone()
        changed=json.loads(row[0]);changed['resource_ids']['build']=str(uuid.uuid4())
        service.db.execute('UPDATE operations SET payload=?',(json.dumps(changed),));service.db.commit()
        with pytest.raises(Rejected,match='scratch_binding_drift'):
            synthetic_slot.lease(service.db,job['command_id'])
    finally:
        service.close()


@pytest.mark.parametrize('start_fails',[False,True])
def test_synthetic_accepted_or_start_failure_survives_restart(tmp_path,synthetic_slot,monkeypatch,start_fails):
    import hub_staging_service.core as core
    class DeferredThread:
        def __init__(self,**kwargs): pass
        def start(self):
            if start_fails: raise RuntimeError('synthetic thread failure')
        def join(self): pass
    monkeypatch.setattr(core.threading,'Thread',DeferredThread)
    root=tmp_path/'worker'
    service=Service(root,{'demo':profile()},Backend(),scratch_slot=synthetic_slot)
    job=payload()
    if start_fails:
        with pytest.raises(Rejected,match='worker_start_failed'):service.submit(job)
    else:
        assert service.submit(job)['state']=='accepted'
    assert service.db.execute('SELECT state FROM scratch_leases').fetchone()[0]=='occupied'
    assert list(synthetic_slot.path.iterdir())==[]
    service.close()
    restarted=Service(root,{'demo':profile()},Backend(),scratch_slot=synthetic_slot)
    try:
        assert restarted.status(job['job_id'])['state']=='held'
        with pytest.raises(Rejected):restarted.submit(payload())
    finally:restarted.close()


@pytest.mark.skipif(os.name!='posix',reason='Linux dirfd-rmdir and directory fsync required')
def test_synthetic_filesystem_with_real_posix_cleanup_receipt(tmp_path,synthetic_slot):
    # Actual POSIX temp-directory lifecycle, but injected dedicated-fs facts.
    # This proves journal/removal behavior, not a physical quota.
    service=Service(tmp_path/'worker',{'demo':profile()},Backend(),scratch_slot=synthetic_slot)
    job=payload()
    try:
        service.submit(job);service.wait()
        assert service.status(job['job_id'])['state']=='succeeded'
        row=service.db.execute('SELECT * FROM scratch_leases').fetchone()
        assert row['state']=='released' and json.loads(row['receipt'])['absent'] is True
        assert json.loads(row['absence'])['scope']=='owned_containers_engine_binds_worker_visible_mounts'
        assert list(synthetic_slot.path.iterdir())==[]
    finally:service.close()


@pytest.mark.skipif(os.name!='posix',reason='POSIX dirfd marker persistence; no actual quota claim')
def test_synthetic_slot_real_journal_cross_root_restart_never_adopts(tmp_path):
    slotroot=tmp_path/'slot';slotroot.mkdir(mode=0o700)
    slot=ScratchSlot(**{**config(),'path':slotroot})
    old=Service(tmp_path/'old-worker',{'demo':profile()},Backend())
    captured={'root_identity':[slotroot.stat().st_dev,slotroot.stat().st_ino]}
    fd=os.open(slotroot,os.O_RDONLY|os.O_DIRECTORY)
    try:
        slot.bind_journal(old.root,old.db,fd,captured)
        command=str(uuid.uuid4());job_id=str(uuid.uuid4())
        directory=slotroot/('job-'+command);directory.mkdir(mode=0o777)
        record={'name':directory.name,'directory_identity':[directory.stat().st_dev,directory.stat().st_ino]}
        old.db.execute('INSERT INTO scratch_leases VALUES (?,?,?,?,?,?)',
                       (command,job_id,'occupied',json.dumps(record),'{}','{}'))
        old.db.commit();old.close()
        # Exact original journal accepts its occupied metadata, never frees it.
        restarted=Service(tmp_path/'old-worker',{'demo':profile()},Backend())
        try:
            slot.bind_journal(restarted.root,restarted.db,fd,captured)
            assert restarted.db.execute('SELECT state FROM scratch_leases').fetchone()[0]=='occupied'
        finally:restarted.close()
        fresh=Service(tmp_path/'different-worker',{'demo':profile()},Backend())
        try:
            with pytest.raises(Rejected,match='scratch_foreign_journal'):
                slot.bind_journal(fresh.root,fresh.db,fd,captured)
            assert directory.is_dir()
            assert fresh.db.execute('SELECT count(*) FROM scratch_leases').fetchone()[0]==0
        finally:fresh.close()
    finally:
        os.close(fd)
        if old.process_lock.closed is False:old.close()


@pytest.mark.skipif(os.name!='posix',reason='POSIX dirfd owner marker checks')
@pytest.mark.parametrize('problem',['unknown_directory','missing_capture','duplicate_marker','marker_symlink'])
def test_synthetic_slot_existing_or_incomplete_state_never_adopted(tmp_path,problem):
    slotroot=tmp_path/'slot';slotroot.mkdir(mode=0o700)
    slot=ScratchSlot(**{**config(),'path':slotroot})
    service=Service(tmp_path/'worker',{'demo':profile()},Backend())
    captured={'root_identity':[slotroot.stat().st_dev,slotroot.stat().st_ino]}
    fd=os.open(slotroot,os.O_RDONLY|os.O_DIRECTORY)
    try:
        if problem=='unknown_directory':
            (slotroot/'old-job').mkdir()
        else:
            slot.bind_journal(service.root,service.db,fd,captured)
            if problem=='missing_capture':
                command=str(uuid.uuid4());name='job-'+command;(slotroot/name).mkdir()
                service.db.execute('INSERT INTO scratch_leases VALUES (?,?,?,?,?,?)',
                    (command,str(uuid.uuid4()),'occupied',json.dumps({'name':name,'directory_identity':None}),'{}','{}'))
                service.db.commit()
            elif problem=='duplicate_marker':
                (slotroot/'.worker-journal.json').write_text('{"schema":1,"schema":1}')
            else:
                (slotroot/'.worker-journal.json').rename(slotroot/'preserved-marker')
                (slotroot/'.worker-journal.json').symlink_to(slotroot/'preserved-marker')
        with pytest.raises((Rejected,OSError)):
            slot.bind_journal(service.root,service.db,fd,captured)
        if problem=='missing_capture':assert (slotroot/name).is_dir()
    finally:os.close(fd);service.close()


@pytest.mark.skipif(os.name!='posix',reason='POSIX empty-only captured-root removal')
def test_synthetic_backend_false_absence_cannot_trigger_recursive_host_cleanup(tmp_path,synthetic_slot):
    class UnsweptBackend(Backend):
        def run(self,paths,profile,command_id,*,scratch_lease=None):
            nested=scratch_lease.path/'private';nested.mkdir(mode=0o700)
            (nested/'retained').write_text('do not recursively delete')
            scratch_lease.confirm_backend_absent(command_id)
            return {}
    service=Service(tmp_path/'worker',{'demo':profile()},UnsweptBackend(),scratch_slot=synthetic_slot)
    job=payload()
    try:
        service.submit(job);service.wait()
        assert service.status(job['job_id'])['state']=='held'
        assert service.status(job['job_id'])['result']['error']=='scratch_cleanup_uncertain'
        assert (synthetic_slot.path/('job-'+job['command_id'])/'private'/'retained').read_text()=='do not recursively delete'
        assert service.db.execute('SELECT state FROM scratch_leases').fetchone()[0]=='cleaning'
    finally:service.close()
