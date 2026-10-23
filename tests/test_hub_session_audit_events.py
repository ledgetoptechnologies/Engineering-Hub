"""Durable audit invalidation pointers are not session acceptance authority."""
from dataclasses import replace

import pytest

from engineering_hub.events import HubOutboxCursorReader
from engineering_hub.session_acceptance_audit import (
    OwnedSessionAcceptanceAudit, SessionAcceptanceAuditError)
from engineering_hub.startup_reconciliation import StartupReconciliation
from tests.test_hub_session_acceptance_audit import owned, rows, result


def pointers(case):
    with case.repo._connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM hub_outbox ORDER BY sequence')]


@pytest.mark.parametrize('intent,outcome', [('accept', 'accepted'),
    ('decline', 'declined'), ('accept', 'failed')])
def test_only_committed_result_emits_one_sanitized_pointer_no_replay(owned, intent, outcome):
    sink = OwnedSessionAcceptanceAudit(owned.repo)
    before = pointers(owned)
    sink(replace(owned.record, outcome=intent))
    assert pointers(owned) == before
    sink(result(owned, outcome))
    saved = pointers(owned)
    assert len(saved) == len(before) + 1
    pointer = saved[-1]
    assert set(pointer) == {'sequence', 'action', 'entity_id', 'revision', 'created_at'}
    assert pointer['action'] == 'staging.session.' + outcome
    assert pointer['entity_id'] == 'staging-session-' + owned.record.request_id
    assert pointer['revision'] == owned.record.revision
    assert owned.record.actor not in str(pointer)
    assert owned.record.report_digest not in str(pointer)
    assert owned.record.executor.generation_id not in str(pointer)
    with pytest.raises(SessionAcceptanceAuditError):
        sink(result(owned, outcome))
    assert pointers(owned) == saved
    assert [event for event, _ in rows(owned.path)] == ['intent', 'result']


def test_result_and_pointer_roll_back_together_when_emit_fails(owned, monkeypatch):
    sink = OwnedSessionAcceptanceAudit(owned.repo)
    sink(owned.record)
    before = pointers(owned)
    original_emit = owned.repo._emit
    def emit_then_fail(db, action, item):
        original_emit(db, action, item)
        raise RuntimeError('Synthetic failure after outbox insertion')
    monkeypatch.setattr(owned.repo, '_emit', emit_then_fail)
    with pytest.raises(SessionAcceptanceAuditError):
        sink(result(owned))
    assert pointers(owned) == before
    assert [event for event, _ in rows(owned.path)] == ['intent']


def test_native_cursor_invalidates_but_history_never_accepts_new_gate(owned):
    reader = HubOutboxCursorReader()
    sink = OwnedSessionAcceptanceAudit(owned.repo)
    with owned.repo._connection() as db:
        _, old_head = reader(db, None)
    sink(owned.record)
    with owned.repo._connection() as db:
        assert reader(db, old_head) == ((), old_head)
    sink(result(owned))
    with owned.repo._connection() as db:
        changed, head = reader(db, old_head)
    assert changed == ('hub',) and head > old_head
    gate = StartupReconciliation(owner=owned.owner, database=owned.path,
        target_id=owned.record.target_id, manifest_digest=owned.record.manifest_digest,
        executor=owned.record.executor, lifetime_seconds=60,
        current_executor_identity=lambda: pytest.fail('No observation at construction'),
        async_admission=True)
    try:
        assert gate.admission_state == 'pending'
        assert gate.challenge != owned.record.request_id
        assert [event for event, _ in rows(owned.path)] == ['intent', 'result']
    finally:
        gate.close()
