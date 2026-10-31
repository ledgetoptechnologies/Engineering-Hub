"""No live database/mounts. Linux fixtures simulate only the RO mount reading."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

import pytest

from engineering_hub import work_queue_inspection as module


@pytest.mark.parametrize('machine,jobs,state', [
    (None, [], 'unknown'),
    ((0, None), [], 'not_reserved'),
    ((1, 'j'), [('j', 'm', 'claimed', 1, 101)], 'active'),
    ((1, 'j'), [('j', 'm', 'claimed', 1, 99)], 'held'),
    ((1, 'j'), [('j', 'm', 'held', 1, None)], 'held'),
    ((2, 'j'), [('j', 'm', 'held', 1, None)], 'unknown'),
    ((1, None), [('j', 'm', 'held', 1, None)], 'unknown'),
    ((1, 'j'), [('j', 'm', 'claimed', 1, float('nan'))], 'unknown'),
    ((1, 'j'), [('j', 'm', 'completed', 1, None)], 'unknown'),
])
def test_conservative_queue_only_reservation(machine, jobs, state):
    assert module._reservation(machine, jobs, 100) == state


def test_platform_and_invalid_inputs_have_fixed_error(tmp_path):
    for machine in ('../secret', '', True):
        with pytest.raises(module.QueueInspectionUnavailable, match='^queue inspection unavailable$'):
            module.inspect_work_queue(str(tmp_path / 'secret.sqlite'), machine_id=machine)


@pytest.fixture
def wal_database():
    if sys.platform != 'linux' or os.geteuid() != 0:
        pytest.skip('synthetic root-private Linux fixture; no live data')
    with tempfile.TemporaryDirectory(prefix='eh-inspect-', dir='/root') as directory:
        path = Path(directory) / 'queue.sqlite'
        writer = sqlite3.connect(path)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE hub_work_machines(machine_id TEXT PRIMARY KEY,generation INTEGER,job_id TEXT)')
        writer.execute('CREATE TABLE hub_work_jobs(id TEXT PRIMARY KEY,machine_id TEXT,state TEXT,fence INTEGER,lease_until REAL)')
        writer.execute("INSERT INTO hub_work_machines VALUES('staging-pc',1,'job')")
        writer.execute("INSERT INTO hub_work_jobs VALUES('job','staging-pc','held',1,NULL)")
        writer.commit()
        try:
            yield path, writer
        finally:
            writer.close()


def test_writable_filesystem_refuses_before_sqlite_open(wal_database, monkeypatch):
    path, _ = wal_database
    called = []
    monkeypatch.setattr(module.sqlite3, 'connect', lambda *a, **k: called.append(True))
    with pytest.raises(module.QueueInspectionUnavailable):
        module.inspect_work_queue(str(path), machine_id='staging-pc')
    assert called == []


def test_wal_committed_hold_and_nonmutation(wal_database, monkeypatch):
    path, writer = wal_database
    # Unit seam only: actual kernel RO-view acceptance remains untested. All
    # remaining no-follow, sidecar, identity and SQLite mode=ro checks execute.
    monkeypatch.setattr(module, '_readonly', lambda fd: None)
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    result = module.inspect_work_queue(str(path), machine_id='staging-pc')
    assert result['queue_reservation'] == 'held' and result['queue_only'] is True
    assert result['requires_reconciliation'] is True
    # A main-file-only view really is stale: schema/hold still lives in WAL.
    stale = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        assert stale.execute("SELECT name FROM sqlite_master WHERE name='hub_work_machines'").fetchone() is None
    finally:
        stale.close()
    after = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    # SHM read marks may change under a writable test mount; production requires
    # an OS read-only view. Database and WAL bytes must never change here.
    assert before.keys() == after.keys()
    assert before[path.name] == after[path.name]
    assert before[path.name + '-wal'] == after[path.name + '-wal']
    assert writer.execute('SELECT generation,job_id FROM hub_work_machines').fetchone() == (1, 'job')


@pytest.mark.parametrize('fault', ['missing_shm', 'journal', 'symlink', 'schema', 'expired', 'stray'])
def test_denials_and_wal_snapshot_states(wal_database, monkeypatch, fault):
    path, writer = wal_database
    monkeypatch.setattr(module, '_readonly', lambda fd: None)
    if fault == 'expired':
        writer.execute("UPDATE hub_work_jobs SET state='claimed',lease_until=0")
    elif fault == 'stray':
        writer.execute("INSERT INTO hub_work_jobs VALUES('other','staging-pc','held',2,NULL)")
    elif fault == 'schema':
        writer.execute('DROP TABLE hub_work_jobs')
    writer.commit()
    if fault == 'missing_shm':
        # Pin check seam avoids removing a writer-owned shared-memory file.
        original = module.os.open
        def missing(name, *args, **kwargs):
            if str(name).endswith('-shm'):
                raise FileNotFoundError()
            return original(name, *args, **kwargs)
        monkeypatch.setattr(module.os, 'open', missing)
    elif fault == 'journal':
        Path(str(path) + '-journal').write_bytes(b'')
    elif fault == 'symlink':
        alias = path.parent / 'alias.sqlite'
        alias.symlink_to(path)
        path = alias
    if fault in ('expired', 'stray'):
        result = module.inspect_work_queue(str(path), machine_id='staging-pc')
        assert result['queue_reservation'] == ('held' if fault == 'expired' else 'unknown')
    else:
        with pytest.raises(module.QueueInspectionUnavailable):
            module.inspect_work_queue(str(path), machine_id='staging-pc')


def test_sqlite_connection_rejects_writes(wal_database, monkeypatch):
    path, _ = wal_database
    monkeypatch.setattr(module, '_readonly', lambda fd: None)
    original = module.sqlite3.connect
    calls = []
    def connect(*args, **kwargs):
        assert '?mode=ro' in args[0] and 'immutable' not in args[0]
        db = original(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError):
            db.execute('DELETE FROM hub_work_machines')
        calls.append(True)
        return db
    monkeypatch.setattr(module.sqlite3, 'connect', connect)
    assert module.inspect_work_queue(str(path), machine_id='staging-pc')['queue_reservation'] == 'held'
    assert calls == [True]


def test_lease_expiring_during_snapshot_read_is_held(wal_database, monkeypatch):
    path, writer = wal_database
    writer.execute("UPDATE hub_work_jobs SET state='claimed',lease_until=5")
    writer.commit()
    monkeypatch.setattr(module, '_readonly', lambda fd: None)
    original = module.sqlite3.connect
    clock = [0]
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        def trace(sql):
            if sql.startswith('SELECT id,machine_id,state'):
                clock[0] = 10
        db.set_trace_callback(trace)
        return db
    monkeypatch.setattr(module.sqlite3, 'connect', connect)
    result = module.inspect_work_queue(str(path), machine_id='staging-pc')
    assert result['queue_reservation'] == 'held' and result['observed_at'] == 10
