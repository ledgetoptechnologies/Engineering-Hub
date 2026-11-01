"""Disposable fixture snapshots; real SQLite, no Docker or staging authority."""
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

if sys.platform != 'linux':
    pytest.skip('Linux local fixture', allow_module_level=True)

from scripts import hub_local_scratch_snapshot as scratch

@pytest.fixture
def seeded(tmp_path,monkeypatch):
    if os.getuid()!=10001 or os.getgid()!=10001:
        pytest.skip('isolated app UID/GID 10001 required')
    # Test copy semantics on the private task filesystem; quota has its own test.
    monkeypatch.setattr(scratch,'_scratch_quota',lambda fd:None)
    root=tmp_path/'generations';root.mkdir(mode=0o700)
    gen=root/'baseline-one';gen.mkdir(mode=0o700)
    seed=tmp_path/'seed.sqlite3';fd=os.open(seed,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
    db=sqlite3.connect(seed);db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE t(value INTEGER)');db.execute('INSERT INTO t VALUES(42)');db.commit()
    for suffix in ('','-wal','-shm'):
        src=Path(str(seed)+suffix)
        if src.exists():
            target=gen/('hub.sqlite3'+suffix);shutil.copyfile(src,target);os.chmod(target,0o600)
    db.close()
    before={p.name:p.read_bytes() for p in gen.iterdir()}
    return root,gen,before


def call(root):
    return scratch.freeze_copy(str(root),'baseline-one','frozen.sqlite3',maximum=2*1024*1024,
                               deadline=time.monotonic()+10)


def test_private_copy_preserves_source_wal_and_publishes_committed_state(seeded):
    root,gen,before=seeded
    result=call(root)
    assert {p.name:p.read_bytes() for p in gen.iterdir()}==before
    assert set(p.name for p in root.iterdir())=={'baseline-one','frozen.sqlite3'}
    assert result.size_bytes==(root/'frozen.sqlite3').stat().st_size
    with sqlite3.connect((root/'frozen.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('SELECT value FROM t').fetchone()==(42,)


def test_retained_fixture_bundle_copy_remains_unchanged(seeded,monkeypatch):
    bundle=os.environ.get('EH_LOCAL_BUNDLE')
    if not bundle:
        pytest.skip('optional task-owned synthetic evidence copy unavailable')
    root,gen,_=seeded
    for path in gen.iterdir():path.unlink()
    source=Path(bundle)
    assert {p.name for p in source.iterdir()}=={'hub.sqlite3','hub.sqlite3-wal','hub.sqlite3-shm'}
    for path in source.iterdir():
        shutil.copyfile(path,gen/path.name);os.chmod(gen/path.name,0o600)
    before={p.name:p.read_bytes() for p in gen.iterdir()}
    monkeypatch.undo()  # Exercise the real tmpfs capacity gate in this task-only run.
    call(root)
    assert {p.name:p.read_bytes() for p in gen.iterdir()}==before
    with sqlite3.connect((root/'frozen.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)


def test_source_drift_holds_exact_scratch_and_prevents_reuse(seeded,monkeypatch):
    root,gen,_=seeded
    old=scratch.copy_sqlite_generation
    def drift(*args,**kwargs):
        (gen/'hub.sqlite3-wal').write_bytes(b'changed')
        return old(*args,**kwargs)
    monkeypatch.setattr(scratch,'copy_sqlite_generation',drift)
    with pytest.raises(Exception):call(root)
    assert not (root/'frozen.sqlite3').exists()
    assert len([p for p in root.iterdir() if p.name.startswith('snapshot-work-')])==1
    with pytest.raises(Exception):call(root)


def test_sidecar_replacement_is_not_adopted(seeded,monkeypatch):
    root,gen,_=seeded
    old=scratch.copy_sqlite_generation
    def replace(*args,**kwargs):
        path=gen/'hub.sqlite3-wal';path.rename(gen/'old-wal');path.write_bytes(b'replaced')
        return old(*args,**kwargs)
    monkeypatch.setattr(scratch,'copy_sqlite_generation',replace)
    with pytest.raises(Exception):call(root)
    assert not (root/'frozen.sqlite3').exists()
    assert (gen/'old-wal').exists()


def test_close_failure_keeps_work_held(seeded,monkeypatch):
    root,_,_=seeded
    original=scratch.os.close
    def fail_close(fd):
        original(fd)
        if any(p.name.startswith('snapshot-work-') for p in root.iterdir()):
            raise OSError('close failed')
    with monkeypatch.context() as m:
        m.setattr(scratch.os,'close',fail_close)
        with pytest.raises(OSError):call(root)
    assert not (root/'frozen.sqlite3').exists()
    assert len([p for p in root.iterdir() if p.name.startswith('snapshot-work-')])==1
    with pytest.raises(Exception):call(root)


def test_existing_frozen_collision_is_preserved(seeded):
    root,_,_=seeded
    (root/'frozen.sqlite3').write_bytes(b'preserve');os.chmod(root/'frozen.sqlite3',0o600)
    with pytest.raises(Exception):call(root)
    assert (root/'frozen.sqlite3').read_bytes()==b'preserve'


@pytest.mark.parametrize('failure',['write','fsync','deadline'])
def test_storage_or_deadline_failure_never_publishes(seeded,monkeypatch,failure):
    root,_,before=seeded
    if failure=='write':
        monkeypatch.setattr(scratch.os,'write',lambda *a:(_ for _ in ()).throw(OSError(28,'full')))
    elif failure=='fsync':
        monkeypatch.setattr(scratch.os,'fsync',lambda *a:(_ for _ in ()).throw(OSError('failed')))
    with pytest.raises(Exception):
        if failure=='deadline':
            scratch.freeze_copy(str(root),'baseline-one','frozen.sqlite3',maximum=2*1024*1024,deadline=time.monotonic()-1)
        else:call(root)
    assert not (root/'frozen.sqlite3').exists()
    assert {p.name:p.read_bytes() for p in (root/'baseline-one').iterdir()}==before


def test_unknown_after_publication_keeps_artifact_and_holds_scratch(seeded,monkeypatch):
    root,_,_=seeded
    original=scratch.os.link
    def link_then_fail(*args,**kwargs):
        original(*args,**kwargs)
        raise OSError('lost result')
    monkeypatch.setattr(scratch.os,'link',link_then_fail)
    with pytest.raises(OSError):call(root)
    assert (root/'frozen.sqlite3').exists()
    assert len([p for p in root.iterdir() if p.name.startswith('snapshot-work-')])==1
    with pytest.raises(Exception):call(root)


def test_quota_requires_bounded_tmpfs(monkeypatch):
    import ctypes
    import types
    class Lib:
        kind=0x01021994
        def fstatfs(self,fd,buf):
            ctypes.cast(buf,ctypes.POINTER(ctypes.c_long))[0]=self.kind
            return 0
    lib=Lib()
    monkeypatch.setattr(scratch.ctypes,'CDLL',lambda *a,**k:lib)
    stats=types.SimpleNamespace(f_flag=0,f_frsize=4096,f_blocks=131072,f_files=4096)
    monkeypatch.setattr(scratch.os,'fstatvfs',lambda fd:stats)
    scratch._scratch_quota(5)
    for update in ({'f_blocks':131073},{'f_files':4097},{'f_flag':os.ST_RDONLY}):
        old={k:getattr(stats,k) for k in update}
        for k,v in update.items():setattr(stats,k,v)
        with pytest.raises(ValueError,match='bounded scratch'):scratch._scratch_quota(5)
        for k,v in old.items():setattr(stats,k,v)
    lib.kind=0xef53
    with pytest.raises(ValueError,match='bounded scratch'):scratch._scratch_quota(5)


def test_actual_task_tmpfs_quota_when_provided():
    path=os.environ.get('EH_LOCAL_BUNDLE')
    if not path:pytest.skip('optional bounded task tmpfs unavailable')
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:scratch._scratch_quota(fd)
    finally:os.close(fd)
