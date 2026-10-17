import json
import sqlite3
from contextlib import closing
import uuid

import pytest

from engineering_hub.outbox import HubOutboxPublisher
from engineering_hub.repository import HubRepository


class FakeBus:
    def __init__(self):
        self.requests = []
        self.messages = {}
        self.fail_after_send = False
        self.bad_receipt = False

    async def send_message(self, **payload):
        self.requests.append(payload)
        key = payload["client_request_id"]
        if key in self.messages:
            assert self.messages[key]["payload"] == payload
        else:
            self.messages[key] = {"id": str(uuid.uuid4()), "payload": payload}
        if self.fail_after_send:
            raise RuntimeError("sensitive-client-token")
        return {"id": "invalid" if self.bad_receipt else self.messages[key]["id"]}


def create(tmp_path):
    repo = HubRepository(tmp_path / "hub.db")
    repo.create_task("human:123", "3d-viewer", "PRIVATE TITLE MUST NOT LEAVE HUB", "test-task")
    return repo, FakeBus()


@pytest.mark.asyncio
async def test_bounded_fanout_pointer_only_and_no_repeat(tmp_path):
    repo, bus = create(tmp_path)
    publisher = HubOutboxPublisher(repo, bus)
    first = await publisher.drain_batch(limit=1)
    assert first == {"accepted": 1, "failed": 0, "pending": 1, "held_reason": None}
    assert await publisher.drain_batch() == {"accepted": 1, "failed": 0, "pending": 0, "held_reason": None}
    assert await publisher.drain_batch() == {"accepted": 0, "failed": 0, "pending": 0, "held_reason": None}
    assert len(bus.requests) == 2
    assert {request["recipient"] for request in bus.requests} == {"hermes", "claude"}
    for request in bus.requests:
        assert request["body"]["topic"] == "auto-wake-v1-test"
        assert request["correlation_id"] == "auto-wake-v1-test/engineering-hub-events"
        assert not {"sender", "token", "actor"} & request.keys()
        assert set(request["body"]) == {"topic", "event", "entity_id", "revision", "outbox_sequence"}
        assert "PRIVATE TITLE" not in json.dumps(request)


@pytest.mark.asyncio
async def test_uncertain_send_reopen_reuses_exact_key_payload(tmp_path):
    repo, bus = create(tmp_path)
    publisher = HubOutboxPublisher(repo, bus, recipients=("hermes",))
    bus.fail_after_send = True
    assert await publisher.drain_batch() == {"accepted": 0, "failed": 1, "pending": 1, "held_reason": "bus_acceptance_unconfirmed"}
    bus.fail_after_send = False
    reopened = HubOutboxPublisher(HubRepository(repo.path), bus, recipients=("hermes",), correlation_id="auto-wake-v1-test/new-config")
    assert await reopened.drain_batch() == {"accepted": 1, "failed": 0, "pending": 0, "held_reason": None}
    assert bus.requests[0] == bus.requests[1]
    assert len(bus.messages) == 1


@pytest.mark.asyncio
async def test_crash_after_send_before_receipt_replays_without_duplicate(tmp_path, monkeypatch):
    repo, bus = create(tmp_path)
    publisher = HubOutboxPublisher(repo, bus, recipients=("codex",))

    def crash(*args):
        raise SystemExit("simulated process loss")

    monkeypatch.setattr(publisher, "_mark_accepted", crash)
    with pytest.raises(SystemExit):
        await publisher.drain_batch()
    reopened = HubOutboxPublisher(repo, bus, recipients=("codex",))
    assert await reopened.drain_batch() == {"accepted": 1, "failed": 0, "pending": 0, "held_reason": None}
    assert len(bus.messages) == 1
    assert bus.requests[0] == bus.requests[1]


@pytest.mark.asyncio
async def test_invalid_receipt_never_marks_accepted_and_pause_allows_notifications(tmp_path):
    repo, bus = create(tmp_path)
    repo.set_pause("human:123", True)
    bus.bad_receipt = True
    publisher = HubOutboxPublisher(repo, bus, recipients=("hermes",))
    assert await publisher.drain_batch() == {"accepted": 0, "failed": 1, "pending": 2, "held_reason": "bus_acceptance_unconfirmed"}
    with closing(sqlite3.connect(repo.path)) as db, db:
        assert db.execute("SELECT COUNT(*) FROM hub_delivery_receipts WHERE message_id IS NOT NULL").fetchone()[0] == 0
    bus.bad_receipt = False
    assert await publisher.drain_batch() == {"accepted": 2, "failed": 0, "pending": 0, "held_reason": None}
    assert repo.get_paused() is True


def test_recipient_topic_and_limits_are_explicit(tmp_path):
    repo, bus = create(tmp_path)
    for recipients in ((), ("beau",), ("https://external.example",)):
        with pytest.raises(ValueError):
            HubOutboxPublisher(repo, bus, recipients=recipients)
    with pytest.raises(ValueError):
        HubOutboxPublisher(repo, bus, topic="unapproved-topic")


@pytest.mark.asyncio
async def test_bad_batch_limit_rejected_before_send(tmp_path):
    repo, bus = create(tmp_path)
    publisher = HubOutboxPublisher(repo, bus)
    for limit in (0, 501, True):
        with pytest.raises(ValueError):
            await publisher.drain_batch(limit)
    assert not bus.requests


@pytest.mark.parametrize('correlation', [
    'engineering-hub-events', 'another-topic/engineering-hub-events',
    'auto-wake-v1-test/', 'auto-wake-v1-test//events', '', True,
])
def test_configured_routing_conflicts_rejected_before_namespace_write(tmp_path, correlation):
    repo, bus = create(tmp_path)
    with pytest.raises(ValueError):
        HubOutboxPublisher(repo, bus, correlation_id=correlation)
    with closing(sqlite3.connect(repo.path)) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_delivery_receipts'").fetchone()
    assert not bus.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_correlation', ['engineering-hub-events', 'auto-wake-v1-test//events'])
async def test_persisted_conflicting_envelope_is_held_never_rewritten_or_resent(tmp_path, legacy_correlation):
    repo, bus = create(tmp_path)
    publisher = HubOutboxPublisher(repo, bus, recipients=('hermes',))
    prepared = publisher._prepare(1)[0]
    original = json.loads(prepared['request_json'])
    original['correlation_id'] = legacy_correlation
    encoded = json.dumps(original, sort_keys=True, separators=(',', ':'))
    with closing(sqlite3.connect(repo.path)) as db, db:
        db.execute('UPDATE hub_delivery_receipts SET request_json=?', (encoded,))
    reopened = HubOutboxPublisher(repo, bus, recipients=('hermes',))
    assert await reopened.drain_batch() == {
        'accepted': 0, 'failed': 1, 'pending': 1, 'held_reason': 'routing_conflict',
    }
    assert bus.requests == []
    with closing(sqlite3.connect(repo.path)) as db:
        saved = db.execute('SELECT request_json,message_id FROM hub_delivery_receipts').fetchone()
        assert saved == (encoded, None)
