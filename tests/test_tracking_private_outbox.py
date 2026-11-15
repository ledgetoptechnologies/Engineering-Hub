"""Project-private tracking pointers must never enter the unscoped bus."""
import uuid

import pytest

from engineering_hub.outbox import HubOutboxPublisher
from tests.test_hub_outbox import FakeBus
from tests.test_session_hub_bridge import OWNER, PROJECT, unified


@pytest.mark.asyncio
async def test_tracking_mutations_are_audited_without_generic_pointers(unified):
    _, app, _, _ = unified
    repo = app.state.repository
    task = repo.propose_tracking_task('codex', PROJECT, 'Private', 'propose')
    claim = repo.update_tracking_task('codex', task['id'], task['revision'], 'claim',
        progress='Done', evidence_links=[{'label': 'Evidence', 'url': 'https://example.com/evidence'}],
        completion_claim=True)
    repo.review_tracking_task('claude', task['id'], claim['revision'], 'review', verdict='verify',
        note='Checked', evidence_links=[{'label': 'Evidence', 'url': 'https://example.com/evidence'}])
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_audit WHERE entity_id=?', (task['id'],)).fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM hub_outbox WHERE entity_id=?', (task['id'],)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_retained_tracking_pointers_and_prepared_receipts_are_held_before_limit(unified):
    _, app, _, _ = unified
    repo = app.state.repository
    task = repo.propose_tracking_task('codex', PROJECT, 'Private', 'propose')
    fake = FakeBus()
    publisher = HubOutboxPublisher(repo, fake, recipients=('hermes',))
    missing = str(uuid.uuid4())
    malformed = str(uuid.uuid4())
    with repo._transaction() as db:
        db.execute("INSERT INTO hub_delivery_sender(id,principal) VALUES(1,'codex')")
        db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,'tracking_task','not-json')", (malformed,))
        for identity, action in ((task['id'], 'tracking_task.updated'),
                                 (missing, 'tracking_task.reviewed'),
                                 (malformed, 'tracking_task.proposed'),
                                 (task['id'], 'unexpected.legacy')):
            db.execute('INSERT INTO hub_outbox(action,entity_id,revision,created_at) VALUES(?,?,1,?)',
                       (action, identity, 'synthetic'))
            sequence = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            db.execute("INSERT INTO hub_delivery_receipts(sequence,recipient,request_json,message_id) VALUES(?, 'hermes', '{}', NULL)", (sequence,))
    ordinary = repo.create_decision(OWNER, PROJECT, 'Ordinary', ['Yes', 'No'])
    with repo._transaction() as db:
        db.execute("INSERT INTO hub_outbox(action,entity_id,revision,created_at) VALUES('trackingXtask.created',?,1,'synthetic')", (ordinary['id'],))
    result = await publisher.drain_batch(limit=1)
    assert result['failed'] == 0 and result['accepted'] == 1 and result['pending'] == 1
    result = await publisher.drain_batch(limit=1)
    assert result['failed'] == 0 and result['accepted'] == 1 and result['pending'] == 0
    assert len(fake.requests) == 2 and all(r['body']['entity_id'] == ordinary['id'] for r in fake.requests)
    assert fake.requests[1]['body']['event'] == 'trackingXtask.created'
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_delivery_receipts WHERE message_id IS NULL').fetchone()[0] == 4
