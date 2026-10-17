import json
import asyncio
import sqlite3
from contextlib import closing
import uuid

import pytest

from engineering_hub.outbox import HubOutboxPublisher
from engineering_hub.repository import HubRepository


class FakeBus:
    def __init__(self, sender='codex', messages=None):
        self.requests = []
        self.messages = {} if messages is None else messages
        self.sender = sender
        self.info_requests = []
        self.info_response = None
        self.info_error = None
        self.fail_after_send = False
        self.bad_receipt = False

    async def bus_info(self, **payload):
        self.info_requests.append(payload)
        if self.info_error is not None:
            raise self.info_error
        return self.info_response if self.info_response is not None else {
            'native_push': {'inbox_uri': 'agentbus://inbox/' + self.sender},
        }

    async def send_message(self, **payload):
        self.requests.append(payload)
        key = (self.sender, payload["client_request_id"])
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
    assert await publisher._verify_sender() is None
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


@pytest.mark.asyncio
async def test_changed_authenticated_sender_cannot_duplicate_uncertain_acceptance(tmp_path):
    repo, first_bus = create(tmp_path)
    first_bus.fail_after_send = True
    first = HubOutboxPublisher(repo, first_bus, recipients=('claude',))
    assert (await first.drain_batch())['held_reason'] == 'bus_acceptance_unconfirmed'
    with closing(sqlite3.connect(repo.path)) as db:
        before = db.execute('SELECT request_json,message_id FROM hub_delivery_receipts').fetchall()
    other_bus = FakeBus(sender='hermes', messages=first_bus.messages)
    restarted = HubOutboxPublisher(repo, other_bus, recipients=('claude',))
    assert await restarted.drain_batch() == {
        'accepted': 0, 'failed': 1, 'pending': 1, 'held_reason': 'sender_identity_conflict',
    }
    assert not other_bus.requests and len(first_bus.messages) == 1
    with closing(sqlite3.connect(repo.path)) as db:
        assert db.execute('SELECT request_json,message_id FROM hub_delivery_receipts').fetchall() == before
        assert db.execute('SELECT principal FROM hub_delivery_sender').fetchone() == ('codex',)


@pytest.mark.asyncio
async def test_same_principal_new_authenticated_session_replays_exact_saved_request(tmp_path):
    repo, old_bus = create(tmp_path)
    old_bus.fail_after_send = True
    assert (await HubOutboxPublisher(repo, old_bus, recipients=('claude',)).drain_batch())['failed'] == 1
    rotated_bus = FakeBus(sender='codex', messages=old_bus.messages)
    replacement = HubOutboxPublisher(repo, rotated_bus, recipients=('claude',))
    assert (await replacement.drain_batch())['accepted'] == 1
    assert old_bus.requests == rotated_bus.requests
    assert len(old_bus.messages) == 1
    assert rotated_bus.info_requests == [{'topic': 'overview'}]


@pytest.mark.parametrize('info', [
    {}, [], True, {'sender': 'codex'}, {'native_push': None}, {'native_push': []},
    {'native_push': {'inbox_uri': 'agentbus://inbox/unknown'}},
    {'native_push': {'inbox_uri': 'agentbus://inbox/codex/'}},
    {'native_push': {'inbox_uri': 'agentbus://inbox/Codex'}},
    {'native_push': {'inbox_uri': 'agentbus://inbox/codex?principal=hermes'}},
    {'native_push': {'inbox_uri': True}},
])
@pytest.mark.asyncio
async def test_missing_malformed_identity_holds_before_preparing_any_request(tmp_path, info):
    repo, bus = create(tmp_path)
    bus.info_response = info
    publisher = HubOutboxPublisher(repo, bus)
    result = await publisher.drain_batch()
    assert result == {'accepted': 0, 'failed': 1, 'pending': 2, 'held_reason': 'sender_identity_unverified'}
    assert not bus.requests
    assert bus.info_requests == [{'topic': 'overview'}]
    with closing(sqlite3.connect(repo.path)) as db:
        assert db.execute('SELECT COUNT(*) FROM hub_delivery_receipts').fetchone() == (0,)
        assert db.execute('SELECT COUNT(*) FROM hub_delivery_sender').fetchone() == (0,)


@pytest.mark.asyncio
async def test_identity_failure_omits_raw_error_and_response_from_public_status(tmp_path):
    repo, bus = create(tmp_path)
    bus.info_error = RuntimeError('private-token-and-origin')
    result = await HubOutboxPublisher(repo, bus).drain_batch()
    assert result['held_reason'] == 'sender_identity_unverified'
    assert 'private-token-and-origin' not in str(result)
    assert not bus.requests


@pytest.mark.asyncio
async def test_server_proven_inbox_not_caller_declared_sender_selects_pin(tmp_path):
    repo, bus = create(tmp_path)
    bus.sender = 'hermes'
    bus.info_response = {'sender': 'codex', 'identity': 'codex',
                         'native_push': {'inbox_uri': 'agentbus://inbox/hermes'}}
    result = await HubOutboxPublisher(repo, bus, recipients=('claude',)).drain_batch()
    assert result['accepted'] == 1
    with closing(sqlite3.connect(repo.path)) as db:
        assert db.execute('SELECT principal FROM hub_delivery_sender').fetchone() == ('hermes',)


@pytest.mark.parametrize('previously_accepted', [False, True])
@pytest.mark.asyncio
async def test_legacy_prepared_state_cannot_adopt_todays_token(tmp_path, previously_accepted):
    repo, bus = create(tmp_path)
    publisher = HubOutboxPublisher(repo, bus, recipients=('hermes',))
    publisher._prepare(1)  # Models pre-pin historical state.
    with closing(sqlite3.connect(repo.path)) as db, db:
        if previously_accepted:
            db.execute('UPDATE hub_delivery_receipts SET message_id=?', (str(uuid.uuid4()),))
        before = db.execute('SELECT request_json,message_id FROM hub_delivery_receipts').fetchall()
    reopened = HubOutboxPublisher(repo, bus, recipients=('hermes',))
    result = await reopened.drain_batch()
    assert result['held_reason'] == 'legacy_sender_unverified'
    assert result['accepted'] == 0 and result['failed'] == 1
    assert result['pending'] == (0 if previously_accepted else 1)
    assert not bus.requests
    with closing(sqlite3.connect(repo.path)) as db:
        assert db.execute('SELECT request_json,message_id FROM hub_delivery_receipts').fetchall() == before
        assert db.execute('SELECT COUNT(*) FROM hub_delivery_sender').fetchone() == (0,)


@pytest.mark.asyncio
async def test_identity_is_checked_once_per_batch_under_bounded_timeout(tmp_path, monkeypatch):
    repo, bus = create(tmp_path)
    observed = []
    original = asyncio.wait_for
    async def bounded(awaitable, timeout):
        observed.append(timeout)
        return await original(awaitable, timeout)
    monkeypatch.setattr('engineering_hub.outbox.asyncio.wait_for', bounded)
    result = await HubOutboxPublisher(repo, bus).drain_batch()
    assert result['accepted'] == 2
    assert observed == [5] and bus.info_requests == [{'topic': 'overview'}]


@pytest.mark.asyncio
async def test_timeout_holds_without_preparation_or_send(tmp_path, monkeypatch):
    repo, bus = create(tmp_path)
    async def timed_out(awaitable, timeout):
        awaitable.close()
        raise TimeoutError('private-network-detail')
    monkeypatch.setattr('engineering_hub.outbox.asyncio.wait_for', timed_out)
    result = await HubOutboxPublisher(repo, bus).drain_batch()
    assert result['held_reason'] == 'sender_identity_unverified'
    assert not bus.requests


@pytest.mark.asyncio
async def test_concurrent_different_authenticated_publishers_share_only_one_pin(tmp_path):
    repo, bus = create(tmp_path)
    bus.fail_after_send = True
    other = FakeBus(sender='hermes', messages=bus.messages)
    first = HubOutboxPublisher(repo, bus, recipients=('claude',))
    second = HubOutboxPublisher(repo, other, recipients=('claude',))
    results = await asyncio.gather(first.drain_batch(), second.drain_batch())
    assert {result['held_reason'] for result in results} == {
        'bus_acceptance_unconfirmed', 'sender_identity_conflict',
    }
    assert len(bus.messages) == 1 and len(bus.requests) == 1 and not other.requests
