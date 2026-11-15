import json
import sqlite3
import threading

import pytest

from hub_staging_service.core import Service, Rejected


@pytest.fixture
def service():
    s = Service.__new__(Service)
    s.lock = threading.RLock()
    s.db = sqlite3.connect(':memory:')
    s.db.row_factory = sqlite3.Row
    s.db.execute('CREATE TABLE operations (command_id TEXT, job_id TEXT, payload TEXT)')
    s.db.execute('ALTER TABLE operations ADD COLUMN state TEXT NOT NULL')
    s.db.execute('CREATE TABLE scratch_leases (state TEXT NOT NULL)')
    s.db.execute("CREATE INDEX status_unsettled_operations ON operations(state)\n                WHERE state NOT IN ('succeeded','failed') OR state IS NULL")
    s.db.execute("CREATE INDEX status_unreleased_leases ON scratch_leases(state)\n                WHERE state != 'released' OR state IS NULL")
    s._receipt_fault = None
    s.active = None
    # Unit fixture has an in-memory journal; real reader/path tests below use
    # an actual worker root and SQLite file.
    s._legacy_idle_status = lambda: None
    yield s
    s.db.close()


def add_active(s, payload):
    s.active = 'command-a'
    s.db.execute('INSERT INTO operations VALUES (?, ?, ?, ?)', ('command-a', 'job-a', payload, 'running'))


def unavailable(call):
    with pytest.raises(Rejected) as error:
        call()
    assert error.value.code == 'worker_status_unavailable'
    assert error.value.status == 503


def test_no_active_keeps_existing_null(service):
    assert service.legacy_active() is None
    assert service.scoped_active('alice', {'demo'}, {'demo': {'default'}}) is None


def test_legacy_active_and_scoped_foreign(service):
    add_active(service, json.dumps({'project': 'demo'}))
    assert service.legacy_active() == 'command-a'
    unavailable(lambda: service.scoped_active('alice', {'demo'}, {'demo': {'default'}}))


def test_scoped_owner_and_foreign_active(service, monkeypatch):
    add_active(service, json.dumps({'_receiver_principal': 'alice', 'project': 'demo'}))
    monkeypatch.setattr(service, '_scoped_job', lambda *args: ({'command_id': 'command-a'}, None))
    assert service.scoped_active('alice', {'demo'}, {'demo': {'default'}}) == 'command-a'
    unavailable(lambda: service.legacy_active(scoped_mode=True))
    unavailable(lambda: service.scoped_active('bob', {'demo'}, {'demo': {'default'}}))
    unavailable(lambda: service.scoped_active('alice', {'other'}, {'other': {'default'}}))


@pytest.mark.parametrize('payload', ['{', '[]', 'null'])
def test_corrupt_payload_never_looks_idle(service, payload):
    add_active(service, payload)
    unavailable(lambda: service.legacy_active(scoped_mode=True))
    unavailable(lambda: service.scoped_active('alice', {'demo'}, {'demo': {'default'}}))


def test_missing_active_row_never_looks_idle(service):
    service.active = 'missing'
    unavailable(lambda: service.legacy_active(scoped_mode=True))
    unavailable(lambda: service.scoped_active('alice', {'demo'}, {'demo': {'default'}}))


def test_scoped_owner_metadata_mismatch_is_unavailable(service, monkeypatch):
    add_active(service, json.dumps({'_receiver_principal': 'alice', 'project': 'demo'}))
    monkeypatch.setattr(service, '_scoped_job', lambda *args: ({'command_id': 'other'}, None))
    unavailable(lambda: service.scoped_active('alice', {'demo'}, {'demo': {'default'}}))

def provision(service):
    # Synthetic-only explicit owner migration; production startup never does this.
    service.db.executescript("""CREATE INDEX status_unsettled_operations ON operations(state)
                WHERE state NOT IN ('succeeded','failed') OR state IS NULL;
            CREATE INDEX status_unreleased_leases ON scratch_leases(state)
                WHERE state != 'released' OR state IS NULL;""")


def test_legacy_startup_never_auto_provisions_status_indexes(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    try:
        def indexes():
            return service.db.execute("SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN ('status_unsettled_operations','status_unreleased_leases')").fetchall()
        assert indexes() == []
        assert service.legacy_active() is None
        unavailable(lambda: service.scoped_active('alice', {'demo'}, {'demo': {'default'}}))
        assert indexes() == []
    finally:
        service.close()
    reopened = Service(tmp_path, {'demo': profile()}, Backend())
    try:
        assert reopened.legacy_active() is None
        assert reopened.db.execute("SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('status_unsettled_operations','status_unreleased_leases')").fetchall() == []
    finally:
        reopened.close()


def test_surviving_scoped_metadata_requires_explicit_status_indexes(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    try:
        service.db.execute('CREATE TABLE source_attempt_owners (source_id TEXT)')
        service.db.commit()
        unavailable(service.legacy_active)
        unavailable(lambda: service.legacy_active(scoped_mode=True))
        unavailable(lambda: service.scoped_active('alice', {'demo'}, {'demo': {'default'}}))
        assert service.db.execute("SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('status_unsettled_operations','status_unreleased_leases')").fetchall() == []
    finally:
        service.close()

def test_legacy_reader_fails_closed_for_surviving_attempt_or_operation_marker(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    try:
        service.db.execute('CREATE TABLE source_attempts (source_id TEXT, state TEXT)')
        service.db.execute("INSERT INTO source_attempts VALUES ('source-a','scoped_uncertain')")
        service.db.commit()
        unavailable(service.legacy_active)
        service.db.execute('DROP TABLE source_attempts')
        service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                           ('command-a', 'identity', 'job-a', 'held', '{"_receiver_principal":"alice"}', '{}'))
        service.db.commit()
        unavailable(service.legacy_active)
        provision(service)
        unavailable(service.legacy_active)
        assert service.db.execute('SELECT state FROM operations').fetchone()['state'] == 'held'
    finally:
        service.close()


def test_legacy_completed_history_without_scope_stays_available_and_does_not_migrate(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    try:
        service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                           ('command-a', 'identity', 'job-a', 'succeeded', '{"project":"demo"}', '{}'))
        service.db.commit()
        assert service.legacy_active() is None
        assert service.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name IN ('status_unsettled_operations','status_unreleased_leases')").fetchone() is None
    finally:
        service.close()


def test_legacy_runtime_scope_probe_preserves_real_legacy_active_and_hides_scoped_or_malformed(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    try:
        service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                           ('command-a', 'identity', 'job-a', 'running', '{"project":"demo"}', '{}'))
        service.db.commit()
        assert service.legacy_runtime_allowed() is True
        service.db.execute('UPDATE operations SET payload=?', ('{"_receiver_principal":"alice"}',))
        service.db.commit()
        assert service.legacy_runtime_allowed() is False
        service.db.execute('UPDATE operations SET payload=?', ('{',))
        service.db.commit()
        assert service.legacy_runtime_allowed() is False
        service.db.execute('UPDATE operations SET payload=?', ('{"project":"demo"}',))
        service.db.execute('CREATE TABLE source_attempts (source_id TEXT, state TEXT)')
        service.db.commit()
        # Even historical unscoped attempts are conservatively unavailable.
        assert service.legacy_runtime_allowed() is False
    finally:
        service.close()


def test_legacy_runtime_large_history_hits_budget_without_changing_shared_progress_handler(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    calls = []
    try:
        service.db.executemany('INSERT INTO operations VALUES (?,?,?,?,?,?)',
            ((f'command-{i}', f'identity-{i}', f'job-{i}', 'succeeded', '{"project":"demo"}', '{}')
             for i in range(20000)))
        service.db.commit()
        service.db.set_progress_handler(lambda: calls.append(1) or 0, 100)
        unavailable(service.legacy_runtime_allowed)
        calls.clear()
        service.db.execute('SELECT count(*) FROM operations').fetchone()
        # SQLite may optimize count(*), so inspect a bounded statement with rows.
        service.db.execute("SELECT count(*) FROM operations WHERE state='succeeded'").fetchone()
        assert calls
    finally:
        service.db.set_progress_handler(None, 0)
        service.close()


def test_reader_does_not_clobber_shared_connection_progress_handler(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    calls = []
    try:
        service.db.set_progress_handler(lambda: calls.append(1) or 0, 1)
        assert service.legacy_active() is None
        calls.clear()
        service.db.execute('SELECT count(*) FROM operations').fetchone()
        assert calls
    finally:
        service.db.set_progress_handler(None, 0)
        service.close()


def test_legacy_reader_budget_timeout_fails_closed_without_indexes(tmp_path, monkeypatch):
    from hub_staging_service import core
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    try:
        values = iter([100.0, 101.0])
        with monkeypatch.context() as scoped:
            scoped.setattr(core.time, 'monotonic', lambda: next(values, 101.0))
            unavailable(service.legacy_active)
        assert service.legacy_active() is None
        assert service.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name IN ('status_unsettled_operations','status_unreleased_leases')").fetchone() is None
    finally:
        service.close()


def test_legacy_reader_rejects_replaced_journal_identity(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    journal, backup = tmp_path / 'journal.sqlite3', tmp_path / 'saved-journal.sqlite3'
    try:
        journal.rename(backup)
        journal.write_bytes(b'not-the-open-journal')
        unavailable(service.legacy_active)
    finally:
        journal.unlink(missing_ok=True)
        backup.rename(journal)
        service.close()


def test_isolated_legacy_reader_holds_one_wal_snapshot_across_handoff(tmp_path, monkeypatch):
    from hub_staging_service import core
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    provision(service)
    service.db.execute('INSERT INTO scratch_leases VALUES (?,?,?,?,?,?)',
                       ('lease-a', 'job-a', 'held', '{}', '{}', '{}'))
    service.db.commit()
    writer = sqlite3.connect(str(tmp_path / 'journal.sqlite3'))
    real_connect = sqlite3.connect
    snapshot_ready, continue_reader = threading.Event(), threading.Event()
    outcomes, guard_results = [], []

    class ObservedCursor:
        def __init__(self, wrapped): self.wrapped = wrapped
        def fetchone(self):
            row = self.wrapped.fetchone()
            guard_results.append(row[0])
            return row

    class ObservedConnection:
        def __init__(self, wrapped): self.wrapped = wrapped
        def __getattr__(self, name): return getattr(self.wrapped, name)
        def execute(self, sql, *args):
            result = self.wrapped.execute(sql, *args)
            if "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN" in sql:
                snapshot_ready.set()
                assert continue_reader.wait(3), 'synthetic reader gate timed out'
            if 'SELECT CASE WHEN' in sql:
                return ObservedCursor(result)
            return result

    def observed_connect(*args, **kwargs):
        return ObservedConnection(real_connect(*args, **kwargs))

    def read_status():
        try: outcomes.append(service.legacy_active())
        except Exception as exc: outcomes.append(exc)

    thread = threading.Thread(target=read_status)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(core.sqlite3, 'connect', observed_connect)
            thread.start()
            assert snapshot_ready.wait(3)
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                           ('command-a', 'identity', 'job-a', 'held', '{"_receiver_principal":"alice"}', '{}'))
            writer.execute("UPDATE scratch_leases SET state='released'")
            writer.commit()
            continue_reader.set()
            thread.join(3)
            assert not thread.is_alive() and len(outcomes) == 1
            assert isinstance(outcomes[0], Rejected) and outcomes[0].code == 'worker_status_unavailable'
            assert guard_results == [1]  # The old snapshot saw the held lease.
        # The next snapshot sees the inserted held operation instead.
        unavailable(service.legacy_active)
    finally:
        continue_reader.set()
        thread.join(3)
        writer.close()
        service.close()
        assert not thread.is_alive()


@pytest.fixture
def real_service(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    provision(service)
    yield service
    service.close()


def assert_both_unavailable(service):
    unavailable(lambda: service.legacy_active(scoped_mode=True))
    unavailable(lambda: service.scoped_active('alice', {'demo'}, {'demo': {'default'}}))


def test_real_service_held_and_recovering_are_unavailable(real_service):
    assert real_service.legacy_active() is None
    assert real_service.scoped_active('alice', {'demo'}, {'demo': {'default'}}) is None
    for state in ('held', 'accepted', 'running', 'corrupt'):
        real_service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                                ('command-' + state, 'identity', 'job-' + state, state, '{}', '{}'))
        real_service.db.commit()
        assert real_service.active is None
        assert_both_unavailable(real_service)
        real_service.db.execute('DELETE FROM operations')
        real_service.db.commit()
    assert real_service.legacy_active() is None


def test_real_service_receipt_fault_is_unavailable(real_service):
    real_service._receipt_fault = 'terminal_receipt_recovery_required'
    assert_both_unavailable(real_service)


def test_real_service_closed_database_is_unavailable(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    service.db.close()
    try:
        assert_both_unavailable(service)
    finally:
        service.process_lock.close()


def test_restart_of_accepted_operation_remains_unavailable(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    provision(service)
    service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                       ('command-a', 'identity', 'job-a', 'accepted', '{}', '{}'))
    service.db.commit()
    service.close()
    service = Service(tmp_path, {'demo': profile()}, Backend())
    try:
        assert service.active is None
        assert service.db.execute('SELECT state FROM operations').fetchone()['state'] == 'held'
        assert_both_unavailable(service)
    finally:
        service.close()



def test_status_queries_use_partial_indexes_and_missing_index_fails_closed(real_service):
    query = """SELECT EXISTS(SELECT 1 FROM operations INDEXED BY status_unsettled_operations
               WHERE state NOT IN ('succeeded','failed')),
               EXISTS(SELECT 1 FROM scratch_leases INDEXED BY status_unreleased_leases
               WHERE state != 'released')"""
    plan = ' '.join(str(tuple(row)) for row in real_service.db.execute('EXPLAIN QUERY PLAN ' + query))
    assert 'status_unsettled_operations' in plan
    assert 'status_unreleased_leases' in plan
    real_service.db.execute('DROP INDEX status_unsettled_operations')
    assert_both_unavailable(real_service)


@pytest.mark.parametrize('name,table,column', [
    ('status_unsettled_operations', 'operations', 'state'),
    ('status_unreleased_leases', 'scratch_leases', 'state'),
])
def test_wrong_same_named_index_fails_closed(real_service, name, table, column):
    real_service.db.execute('DROP INDEX ' + name)
    real_service.db.execute('CREATE INDEX ' + name + ' ON ' + table + '(' + column + ')')
    assert_both_unavailable(real_service)


def test_cross_connection_handoff_cannot_appear_idle(real_service, tmp_path):
    real_service.db.execute('INSERT INTO scratch_leases VALUES (?,?,?,?,?,?)',
                            ('lease-a', 'job-a', 'held', '{}', '{}', '{}'))
    real_service.db.commit()
    writer = sqlite3.connect(str(tmp_path / 'journal.sqlite3'))
    try:
        real_service.db.execute('BEGIN')
        assert real_service.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        writer.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                       ('command-a', 'identity', 'job-a', 'held', '{}', '{}'))
        writer.execute("UPDATE scratch_leases SET state='released'")
        writer.commit()
        # The established WAL snapshot sees the held lease despite the writer.
        unavailable(lambda: real_service.legacy_active(scoped_mode=True))
        real_service.db.rollback()
        # A fresh snapshot sees the newly held operation.
        assert_both_unavailable(real_service)
    finally:
        real_service.db.rollback()
        writer.close()


def test_wrong_index_survives_restart_but_status_fails_closed(tmp_path):
    from tests.test_staging_service_core import Backend, profile
    service = Service(tmp_path, {'demo': profile()}, Backend())
    provision(service)
    service.db.execute('DROP INDEX status_unsettled_operations')
    service.db.execute('CREATE INDEX status_unsettled_operations ON operations(state)')
    service.db.commit()
    service.close()
    service = Service(tmp_path, {'demo': profile()}, Backend())
    try:
        assert_both_unavailable(service)
    finally:
        service.close()

@pytest.mark.parametrize('table', ['operations', 'scratch_leases'])
def test_nullable_state_schema_fails_closed_even_with_expected_index(service, table):
    if table == 'operations':
        service.db.execute('DROP TABLE operations')
        service.db.execute('CREATE TABLE operations (command_id TEXT, job_id TEXT, payload TEXT, state TEXT)')
        service.db.execute("CREATE INDEX status_unsettled_operations ON operations(state)\n                WHERE state NOT IN ('succeeded','failed') OR state IS NULL")
        service.db.execute('INSERT INTO operations VALUES (?, ?, ?, ?)', ('x', 'j', '{}', None))
    else:
        service.db.execute('DROP TABLE scratch_leases')
        service.db.execute('CREATE TABLE scratch_leases (state TEXT)')
        service.db.execute("CREATE INDEX status_unreleased_leases ON scratch_leases(state)\n                WHERE state != 'released' OR state IS NULL")
        service.db.execute('INSERT INTO scratch_leases VALUES (NULL)')
    unavailable(lambda: service.scoped_active('alice', {'demo'}, {'demo': {'default'}}))

@pytest.mark.parametrize('state,held', [('succeeded', False), ('failed', False), ('held', True), ('unknown', True)])
def test_indexed_operation_states_never_look_idle_when_unsettled(service, state, held):
    service.db.execute('INSERT INTO operations VALUES (?, ?, ?, ?)', ('x', 'j', '{}', state))
    if held:
        unavailable(lambda: service.scoped_active('alice', {'demo'}, {'demo': {'default'}}))
    else:
        assert service.scoped_active('alice', {'demo'}, {'demo': {'default'}}) is None
