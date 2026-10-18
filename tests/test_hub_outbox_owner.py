"""Outbox writes and external I/O share the enrolled repository capability."""
import sys
import sqlite3
from contextlib import closing

import pytest

from engineering_hub.outbox import HubOutboxPublisher
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_outbox import FakeBus


@pytest.fixture
def owned(tmp_path):
    if sys.platform != 'linux':
        pytest.skip('Actual Linux ownership is required')
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    owner = PilotOwner(private / 'hub.sqlite', allow_ephemeral_test_storage=True)
    try:
        repo = HubRepository(private / 'hub.sqlite', pilot_owner=owner)
        repo.create_task('human:1234', 'engineering-hub', 'Event', 'event')
        yield owner, repo
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_live_owner_outbox_schema_and_receipts_use_guarded_transactions(owned):
    _, repo = owned
    bus = FakeBus()
    publisher = HubOutboxPublisher(repo, bus, recipients=('hermes',))
    assert publisher.repo is repo
    assert await publisher.drain_batch() == dict(accepted=1, failed=0, pending=0, held_reason=None)
    with repo._connection() as db:
        assert db.execute('SELECT principal FROM hub_delivery_sender').fetchone()[0] == 'codex'
        assert db.execute('SELECT message_id FROM hub_delivery_receipts').fetchone()[0]


@pytest.mark.asyncio
async def test_closed_owner_denies_before_any_bus_io(owned):
    owner, repo = owned
    bus = FakeBus()
    publisher = HubOutboxPublisher(repo, bus)
    owner.close()
    with pytest.raises(HubError, match='pilot_ownership_required'):
        await publisher.drain_batch()
    assert bus.info_requests == bus.requests == []
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_delivery_receipts').fetchone()[0] == 0
    with pytest.raises(HubError, match='pilot_ownership_required'):
        HubOutboxPublisher(repo, bus)


@pytest.mark.asyncio
@pytest.mark.parametrize('loss_at', ['identity', 'send'])
async def test_loss_during_external_await_never_persists_its_result(owned, loss_at):
    owner, repo = owned

    class Bus(FakeBus):
        async def bus_info(self, **payload):
            result = await super().bus_info(**payload)
            if loss_at == 'identity':
                owner.close()
            return result

        async def send_message(self, **payload):
            result = await super().send_message(**payload)
            owner.close()
            return result

    bus = Bus()
    publisher = HubOutboxPublisher(repo, bus, recipients=('hermes',))
    with pytest.raises(HubError, match='pilot_ownership_required'):
        await publisher.drain_batch()
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_delivery_receipts WHERE message_id IS NOT NULL').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM hub_delivery_sender').fetchone()[0] == (0 if loss_at == 'identity' else 1)
    assert len(bus.requests) == (0 if loss_at == 'identity' else 1)


@pytest.mark.asyncio
async def test_owner_lost_after_prepare_denies_send(owned, monkeypatch):
    owner, repo = owned
    bus = FakeBus()
    publisher = HubOutboxPublisher(repo, bus)
    original = publisher._prepare

    def prepare(limit):
        rows = original(limit)
        owner.close()
        return rows

    monkeypatch.setattr(publisher, '_prepare', prepare)
    with pytest.raises(HubError, match='pilot_ownership_required'):
        await publisher.drain_batch()
    assert len(bus.info_requests) == 1 and bus.requests == []


@pytest.mark.asyncio
async def test_unowned_late_enrollment_denies_schema_and_bus_io(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite')
    repo.create_task('human:1234', 'engineering-hub', 'Event', 'event')
    bus = FakeBus()
    publisher = HubOutboxPublisher(repo, bus)
    with closing(sqlite3.connect(repo.path)) as db, db:
        db.execute('CREATE TABLE hub_pilot_owner(id INTEGER)')
    with pytest.raises(HubError, match='pilot_ownership_required'):
        await publisher.drain_batch()
    with pytest.raises(HubError, match='pilot_ownership_required'):
        HubOutboxPublisher(repo, bus)
    assert bus.info_requests == bus.requests == []
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_delivery_receipts').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_late_enrollment_during_identity_await_denies_sender_pin(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite')
    repo.create_task('human:1234', 'engineering-hub', 'Event', 'event')

    class Bus(FakeBus):
        async def bus_info(self, **payload):
            result = await super().bus_info(**payload)
            with closing(sqlite3.connect(repo.path)) as db, db:
                db.execute('CREATE TABLE hub_pilot_owner(id INTEGER)')
            return result

    bus = Bus()
    publisher = HubOutboxPublisher(repo, bus)
    with pytest.raises(HubError, match='pilot_ownership_required'):
        await publisher.drain_batch()
    assert len(bus.info_requests) == 1 and bus.requests == []
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_delivery_sender').fetchone()[0] == 0
