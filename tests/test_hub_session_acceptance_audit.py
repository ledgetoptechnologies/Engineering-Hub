"""Real Linux owned SQLite; synthetic operator/identity, no live host effects."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubRepository
from engineering_hub.session_acceptance import SessionAcceptanceAudit, SessionAcceptanceBroker
from engineering_hub.session_acceptance_audit import (OwnedSessionAcceptanceAudit,
                                                     SessionAcceptanceAuditError)
from engineering_hub.startup_reconciliation import (ExecutorSessionIdentity,
    OperatorReconciliation, StartupReconciliation)

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='real Linux PilotOwner required')
DIGEST = 'sha256:' + 'a' * 64
TABLE = 'hub_session_acceptance_audit'


@pytest.fixture
def owned(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'hub.sqlite3'
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(path, pilot_owner=owner)
        record = SessionAcceptanceAudit('intent', 'accept', 'b' * 64, 1,
            'human:123', owner.epoch, 'engineering-hub:staging', DIGEST,
            'sha256:' + 'c' * 64, ExecutorSessionIdentity(str(uuid4()), DIGEST))
        yield SimpleNamespace(owner=owner, repo=repo, path=path, record=record)


def rows(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        return [(event, json.loads(raw)) for event, raw in db.execute(
            'SELECT event,record FROM ' + TABLE + ' ORDER BY rowid')]


def result(c, outcome='accepted', **changes):
    return replace(c.record, event='result', outcome=outcome, **changes)


def test_intent_then_matching_result_committed_and_reopened_history_only(owned):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    assert sink(c.record) is True
    assert rows(c.path)[0][0] == 'intent'  # Independent connection sees commit.
    assert sink(result(c)) is True
    original = rows(c.path)
    assert [row[0] for row in original] == ['intent', 'result']
    sink.close()
    c.owner.assert_active(c.path)
    c.owner.close()
    with PilotOwner(c.path, allow_ephemeral_test_storage=True, require_existing=True) as owner:
        reopened = OwnedSessionAcceptanceAudit(HubRepository(c.path, pilot_owner=owner))
        assert rows(c.path) == original and owner.epoch != c.record.owner_epoch
        with pytest.raises(SessionAcceptanceAuditError):
            reopened(c.record)
        assert rows(c.path) == original


@pytest.mark.parametrize('intent,outcome', [('accept', 'accepted'), ('accept', 'failed'),
                                         ('decline', 'declined'), ('decline', 'failed')])
def test_action_result_consistency(owned, intent, outcome):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    assert sink(replace(c.record, outcome=intent)) is True
    assert sink(result(c, outcome)) is True
    assert len(rows(c.path)) == 2


@pytest.mark.parametrize('change', [dict(actor='human:999'), dict(revision=2),
    dict(manifest_digest='sha256:' + 'd' * 64), dict(report_digest='sha256:' + 'd' * 64),
    dict(executor=ExecutorSessionIdentity(str(uuid4()), DIGEST)), dict(outcome='declined')])
def test_result_requires_exact_matching_durable_intent(owned, change):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    sink(c.record)
    before = rows(c.path)
    with pytest.raises(SessionAcceptanceAuditError):
        sink(replace(result(c), **change))
    assert rows(c.path) == before


def test_result_without_intent_and_new_epoch_cannot_adopt_old_intent(owned):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    with pytest.raises(SessionAcceptanceAuditError):
        sink(result(c))
    assert rows(c.path) == []
    sink = OwnedSessionAcceptanceAudit(c.repo)
    sink(c.record)
    c.owner.close()
    with PilotOwner(c.path, allow_ephemeral_test_storage=True, require_existing=True) as owner:
        sink = OwnedSessionAcceptanceAudit(HubRepository(c.path, pilot_owner=owner))
        with pytest.raises(SessionAcceptanceAuditError):
            sink(result(c, owner_epoch=owner.epoch))
        assert len(rows(c.path)) == 1


@pytest.mark.parametrize('event', ['intent', 'result'])
def test_replay_rejects_without_overwrite_or_extra_rows(owned, event):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    sink(c.record)
    if event == 'result':
        sink(result(c))
    before = rows(c.path)
    with pytest.raises(SessionAcceptanceAuditError):
        sink(c.record if event == 'intent' else result(c))
    assert rows(c.path) == before


@pytest.mark.parametrize('change', [dict(event='other'), dict(outcome='accepted'),
    dict(request_id=True), dict(request_id='b' * 63), dict(request_id='é' * 64),
    dict(revision=True), dict(revision=0), dict(revision=1.0), dict(revision=2**31),
    dict(actor='codex'), dict(actor='human:é'), dict(actor='human:0'),
    dict(owner_epoch=str(uuid4())), dict(owner_epoch=True), dict(target_id='other:staging'),
    dict(manifest_digest=DIGEST.upper()), dict(report_digest=True),
    dict(executor=True), dict(executor=ExecutorSessionIdentity(True, DIGEST)),
    dict(executor=ExecutorSessionIdentity(str(uuid4()), True)),
    dict(execution_authorization=True), dict(execution_authorization=0)])
def test_strict_record_shape_and_current_owner_binding(owned, change):
    sink = OwnedSessionAcceptanceAudit(owned.repo)
    with pytest.raises(SessionAcceptanceAuditError):
        sink(replace(owned.record, **change))
    assert rows(owned.path) == []


def test_requires_exact_record_and_owned_repository(owned):
    with pytest.raises(SessionAcceptanceAuditError):
        OwnedSessionAcceptanceAudit(object())
    sink = OwnedSessionAcceptanceAudit(owned.repo)
    with pytest.raises(SessionAcceptanceAuditError):
        sink(dict(event='intent'))
    assert rows(owned.path) == []


@pytest.mark.parametrize('corruption', ['bool-revision', 'extra-field', 'duplicate-key', 'event', 'pretty-json'])
def test_stored_intent_must_remain_exact_typed_canonical_record(owned, corruption):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    sink(c.record)
    with c.repo._transaction() as db:
        raw = db.execute('SELECT record FROM ' + TABLE).fetchone()[0]
        value = json.loads(raw)
        if corruption == 'bool-revision':
            value['revision'] = True  # Python True == 1 must not bypass typing.
        elif corruption == 'extra-field':
            value['extra'] = 'not permitted'
        elif corruption == 'event':
            value.update(event='result', outcome='accepted')
        if corruption == 'duplicate-key':
            altered = raw.replace('"revision":1', '"revision":1,"revision":1')
        else:
            altered = json.dumps(value, sort_keys=True,
                **({'indent': 2} if corruption == 'pretty-json' else {'separators': (',', ':')}))
        db.execute('UPDATE ' + TABLE + ' SET record=?', (altered,))
    with pytest.raises(SessionAcceptanceAuditError):
        sink(result(c))
    with c.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM ' + TABLE).fetchone()[0] == 1


def test_two_sinks_concurrent_intent_only_one_commit(owned):
    sinks = [OwnedSessionAcceptanceAudit(owned.repo) for _ in range(2)]
    def attempt(sink):
        try:
            return sink(owned.record)
        except SessionAcceptanceAuditError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, sinks)) == [False, True]
    assert len(rows(owned.path)) == 1


@pytest.mark.parametrize('synchronous', [0, 1])
def test_unsafe_synchronous_is_denied_without_repair(owned, monkeypatch, synchronous):
    original = owned.repo._write_connection
    observed = []
    @contextmanager
    def unsafe():
        with original() as db:
            db.execute(f'PRAGMA synchronous={synchronous}')
            yield db
            observed.append(db.execute('PRAGMA synchronous').fetchone()[0])
    monkeypatch.setattr(owned.repo, '_write_connection', unsafe)
    with pytest.raises(SessionAcceptanceAuditError):
        OwnedSessionAcceptanceAudit(owned.repo)
    with sqlite3.connect(owned.path) as db:
        assert db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (TABLE,)).fetchone() is None


def test_non_wal_is_denied_without_repair(owned):
    with owned.repo._write_connection() as db:
        assert db.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
    with pytest.raises(SessionAcceptanceAuditError):
        OwnedSessionAcceptanceAudit(owned.repo)
    with sqlite3.connect(owned.path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (TABLE,)).fetchone() is None


@pytest.mark.parametrize('kind', ['table', 'trigger', 'index'])
def test_existing_schema_is_checked_not_repaired(owned, kind):
    c = owned
    if kind == 'table':
        with c.repo._transaction() as db:
            db.execute('CREATE TABLE ' + TABLE + '(request_id TEXT)')
        with pytest.raises(SessionAcceptanceAuditError):
            OwnedSessionAcceptanceAudit(c.repo)
    else:
        sink = OwnedSessionAcceptanceAudit(c.repo)
        with c.repo._transaction() as db:
            if kind == 'trigger':
                db.execute('CREATE TRIGGER audit_surprise AFTER INSERT ON ' + TABLE +
                           ' BEGIN DELETE FROM ' + TABLE + '; END')
            else:
                db.execute('CREATE INDEX audit_surprise ON ' + TABLE + '(event)')
        with pytest.raises(SessionAcceptanceAuditError):
            sink(c.record)
        assert rows(c.path) == []
        with c.repo._connection() as db:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='audit_surprise'").fetchone()


def test_commit_failure_rolls_back_and_never_acknowledges(owned, monkeypatch):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    original = c.repo._write_connection
    class CommitFailure:
        def __init__(self, db):
            self.db = db
        def __getattr__(self, name):
            return getattr(self.db, name)
        def commit(self):
            raise sqlite3.OperationalError('PRIVATE commit failure')
    @contextmanager
    def broken():
        with original() as db:
            yield CommitFailure(db)
    monkeypatch.setattr(c.repo, '_write_connection', broken)
    with pytest.raises(SessionAcceptanceAuditError, match='^owned session acceptance audit unavailable; preserve evidence$'):
        sink(c.record)
    assert rows(c.path) == []


@pytest.mark.parametrize('after_commit', [False, True])
def test_owner_loss_before_or_after_commit_is_not_false_rollback(owned, monkeypatch, after_commit):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    original = c.repo._transaction
    @contextmanager
    def revoke():
        with original() as db:
            yield db
            if not after_commit:
                c.owner.close()
        if after_commit:
            c.owner.close()
    monkeypatch.setattr(c.repo, '_transaction', revoke)
    with pytest.raises(SessionAcceptanceAuditError):
        sink(c.record)
    assert len(rows(c.path)) == (1 if after_commit else 0)


def test_closed_owner_repository_rebinding_and_close_denied(owned):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    c.owner.close()
    with pytest.raises(SessionAcceptanceAuditError):
        sink(c.record)
    assert rows(c.path) == []


@pytest.mark.parametrize('field', ['path', 'pilot_owner'])
def test_repository_capability_replacement_cannot_redirect_audit(owned, field):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    original = getattr(c.repo, field)
    try:
        setattr(c.repo, field, original + '.other' if field == 'path' else object())
        with pytest.raises(SessionAcceptanceAuditError):
            sink(c.record)
        assert rows(c.path) == []
    finally:
        setattr(c.repo, field, original)


def test_pid_check_precedes_inherited_mutex(owned):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        class ForbiddenLock:
            def __enter__(self):
                os.write(write_fd, b'lock-touched')
                raise AssertionError
        sink._lock = ForbiddenLock()
        try:
            sink(c.record)
        except SessionAcceptanceAuditError:
            os.write(write_fd, b'closed')
            os._exit(0)
        os._exit(2)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 64) == b'closed'
        assert os.waitpid(pid, 0)[1] == 0
    finally:
        os.close(read_fd)
    assert sink(c.record) is True
    sink.close()
    c.owner.assert_active(c.path)
    with pytest.raises(SessionAcceptanceAuditError):
        sink(result(c))


def test_real_sink_used_by_broker_intent_and_result_without_runtime_activation(owned):
    c = owned
    sink = OwnedSessionAcceptanceAudit(c.repo)
    identity = c.record.executor
    gate = StartupReconciliation(owner=c.owner, database=c.path,
        target_id=c.record.target_id, manifest_digest=DIGEST, executor=identity,
        lifetime_seconds=60, current_executor_identity=lambda: identity)
    evidence = 'Synthetic report, not current-host proof.'
    report = OperatorReconciliation(gate.challenge, c.owner.epoch, c.record.target_id,
        DIGEST, identity, 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(), True, True, True)
    broker = SessionAcceptanceBroker(gate=gate, owner=c.owner, target_id=c.record.target_id,
        manifest_digest=DIGEST, executor=identity, report=report, evidence=evidence,
        operator_actor=c.record.actor, audit=sink)
    try:
        snap = broker.snapshot(actor=c.record.actor)
        assert broker.decide(actor=c.record.actor, request_id=snap['request_id'],
            expected_revision=1, action='accept')['execution_authorization'] is False
        assert gate.admission_state == 'accepted'
        assert [record['outcome'] for _, record in rows(c.path)] == ['accept', 'accepted']
    finally:
        broker.close()
        sink.close()
