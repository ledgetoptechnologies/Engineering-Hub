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
    assert first == {"delivered": 1, "failed": 0, "pending": 1}
    assert await publisher.drain_batch() == {"delivered": 1, "failed": 0, "pending": 0}
    assert await publisher.drain_batch() == {"delivered": 0, "failed": 0, "pending": 0}
    assert len(bus.requests) == 2
    assert {request["recipient"] for request in bus.requests} == {"hermes", "claude"}
    for request in bus.requests:
        assert request["body"]["topic"] == "auto-wake-v1-test"
        assert request["correlation_id"] == "engineering-hub-events"
        assert not {"sender", "token", "actor"} & request.keys()
        assert set(request["body"]) == {"topic", "event", "entity_id", "revision", "outbox_sequence"}
        assert "PRIVATE TITLE" not in json.dumps(request)


@pytest.mark.asyncio
async def test_uncertain_send_reopen_reuses_exact_key_payload(tmp_path):
    repo, bus = create(tmp_path)
    publisher = HubOutboxPublisher(repo, bus, recipients=("hermes",))
    bus.fail_after_send = True
    assert await publisher.drain_batch() == {"delivered": 0, "failed": 1, "pending": 1}
    bus.fail_after_send = False
    reopened = HubOutboxPublisher(HubRepository(repo.path), bus, recipients=("hermes",), correlation_id="new-config")
    assert await reopened.drain_batch() == {"delivered": 1, "failed": 0, "pending": 0}
    assert bus.requests[0] == bus.requests[1]
    assert len(bus.messages) == 1


@pytest.mark.asyncio
async def test_crash_after_send_before_receipt_replays_without_duplicate(tmp_path, monkeypatch):
    repo, bus = create(tmp_path)
    publisher = HubOutboxPublisher(repo, bus, recipients=("codex",))

    def crash(*args):
        raise SystemExit("simulated process loss")

    monkeypatch.setattr(publisher, "_mark_delivered", crash)
    with pytest.raises(SystemExit):
        await publisher.drain_batch()
    reopened = HubOutboxPublisher(repo, bus, recipients=("codex",))
    assert await reopened.drain_batch() == {"delivered": 1, "failed": 0, "pending": 0}
    assert len(bus.messages) == 1
    assert bus.requests[0] == bus.requests[1]


@pytest.mark.asyncio
async def test_invalid_receipt_never_marks_delivered_and_pause_allows_notifications(tmp_path):
    repo, bus = create(tmp_path)
    repo.set_pause("human:123", True)
    bus.bad_receipt = True
    publisher = HubOutboxPublisher(repo, bus, recipients=("hermes",))
    assert await publisher.drain_batch() == {"delivered": 0, "failed": 1, "pending": 2}
    with closing(sqlite3.connect(repo.path)) as db, db:
        assert db.execute("SELECT COUNT(*) FROM hub_delivery_receipts WHERE message_id IS NOT NULL").fetchone()[0] == 0
    bus.bad_receipt = False
    assert await publisher.drain_batch() == {"delivered": 2, "failed": 0, "pending": 0}
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
