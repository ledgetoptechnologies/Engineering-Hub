"""Synthetic private decision outbox regression; no peer messages are sent."""
import json
import uuid
from types import SimpleNamespace

import pytest

from engineering_hub.outbox import HubOutboxPublisher
from tests.test_dispatch_coordination import setup_dispatch
from tests.test_hub_outbox import FakeBus
from tests.test_session_hub_bridge import OWNER, PROJECT, call, unified


@pytest.mark.asyncio
async def test_legacy_private_and_malformed_decisions_never_reach_unscoped_publisher(unified):
    client, app, bus, csrf = unified
    repo = app.state.repository
    env = SimpleNamespace(repo=repo, bridge=app.state.session_hub_bridge, sessions=bus.state.agent_sessions)
    _, worker, _, _, _, service, _, dispatch = setup_dispatch(env)
    asked = call(env, worker, 'hub_dispatch_report', lambda actor: service.report(
        actor, dispatch['id'], 1, 1, 'ask', 'Private question', 'ask', 'Choose', ['Yes', 'No']))
    identity = asked['decision_id']
    fake = FakeBus()
    publisher = HubOutboxPublisher(repo, fake, recipients=('hermes',))
    with repo._transaction() as db:
        db.execute("INSERT INTO hub_delivery_sender(id,principal) VALUES(1,'codex')")
        # Simulate a previously queued private event and a pre-materialized unsent receipt.
        db.execute("INSERT INTO hub_outbox(action,entity_id,revision,created_at) VALUES('decision.answered',?,2,'synthetic')", (identity,))
        sequence = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.execute("INSERT INTO hub_delivery_receipts(sequence,recipient,request_json,message_id) VALUES(?, 'hermes', '{}', NULL)", (sequence,))
        bad_ids = []
        for raw in ('not-json', '[]', 'null'):
            bad = str(uuid.uuid4()); bad_ids.append(bad)
            db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,'decision',?)", (bad, raw))
            db.execute("INSERT INTO hub_outbox(action,entity_id,revision,created_at) VALUES('decision.answered',?,2,'synthetic')", (bad,))
        missing = str(uuid.uuid4())
        db.execute("INSERT INTO hub_outbox(action,entity_id,revision,created_at) VALUES('decision.answered',?,2,'synthetic')", (missing,))
    ordinary = repo.create_decision(OWNER, PROJECT, 'Public event', ['Yes', 'No'])
    answered = repo.answer_decision(OWNER, ordinary['id'], ordinary['revision'], 'Yes')
    assert answered['status'] == 'answered'
    for _ in range(10):
        result = await publisher.drain_batch(limit=1)
        assert result['failed'] == 0
        if result['pending'] == 0:
            break
    assert result['pending'] == 0
    bodies = [request['body'] for request in fake.requests]
    assert any(body['entity_id'] == ordinary['id'] and body['event'] == 'decision.answered' for body in bodies)
    assert all(body['entity_id'] != identity for body in bodies)
    assert all(body['entity_id'] not in [missing, *bad_ids] for body in bodies)
    with repo._connection() as db:
        assert db.execute('SELECT message_id FROM hub_delivery_receipts WHERE sequence=?', (sequence,)).fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM hub_audit WHERE entity_id=?', (ordinary['id'],)).fetchone()[0] == 2
