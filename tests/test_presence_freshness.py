"""A retained/blocked stream is not indefinitely fresh transport evidence."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent_message_bus.presence import MCPStreamPresenceMiddleware
from agent_message_bus.push import PRESENCE_FRESHNESS_SECONDS, PushPublisher
from engineering_hub.bus_connection import project_bus_connections


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr('agent_message_bus.push.monotonic', lambda: value[0])
    monkeypatch.setattr('agent_message_bus.push._utc_now', lambda: epoch + timedelta(seconds=value[0]))
    return value, epoch


async def projected(publisher, epoch, seconds):
    return project_bus_connections(await publisher.presence_status(), now=epoch + timedelta(seconds=seconds))['codex']


@pytest.mark.asyncio
async def test_status_reads_cannot_renew_no_first_write_or_old_heartbeat(clock):
    value, epoch = clock
    publisher = PushPublisher(enabled=True)
    stream = await publisher.open_presence_stream('private-session', 'codex')
    await publisher.subscribe('private-session', 'codex', object())
    # Initial connection gets a bounded grace period for the first SSE heartbeat.
    assert (await projected(publisher, epoch, value[0]))['state'] == 'connected'
    value[0] += PRESENCE_FRESHNESS_SECONDS - 1
    near_expiry = await projected(publisher, epoch, value[0])
    assert near_expiry['state'] == 'connected'
    assert datetime.fromisoformat(near_expiry['valid_until'].replace('Z', '+00:00')) - epoch == timedelta(seconds=1045)
    snapshot = await publisher.presence_status()
    delayed = project_bus_connections(snapshot, now=epoch + timedelta(seconds=value[0] + 2))
    assert delayed['codex']['state'] == 'unavailable'
    value[0] += 1
    for _ in range(3):
        result = await projected(publisher, epoch, value[0])
        assert result['state'] == 'stale'
        assert result['active_streams'] == result['inbox_subscriptions'] == 0
        assert result['stale_streams'] == 1 and result['subscribed'] is False
        value[0] += 86400
    # A real completed write, not a status read, can renew transport evidence.
    await publisher.record_presence_stream_write(stream)
    assert (await projected(publisher, epoch, value[0]))['state'] == 'connected'
    value[0] += PRESENCE_FRESHNESS_SECONDS
    assert (await projected(publisher, epoch, value[0]))['state'] == 'stale'
    await publisher.close_presence_stream(stream)
    assert (await projected(publisher, epoch, value[0]))['state'] == 'not_observed'


@pytest.mark.asyncio
async def test_fresh_replacement_and_old_generation_cleanup(clock):
    value, epoch = clock
    publisher = PushPublisher(enabled=True)
    old = await publisher.open_presence_stream('private-session', 'codex')
    value[0] += PRESENCE_FRESHNESS_SECONDS
    replacement = await publisher.open_presence_stream('private-session', 'codex')
    await publisher.subscribe('private-session', 'codex', object())
    result = await projected(publisher, epoch, value[0])
    assert result['state'] == 'connected'
    assert result['active_streams'] == result['stale_streams'] == result['inbox_subscriptions'] == 1
    await publisher.close_presence_stream(old)
    # Late writes/cleanup on the retired token cannot resurrect/remove replacement.
    await publisher.record_presence_stream_write(old)
    result = await projected(publisher, epoch, value[0])
    assert result['active_streams'] == 1 and result['stale_streams'] == 0
    for _ in range(5):
        value[0] += 15
        await publisher.record_presence_stream_write(replacement)
        assert (await projected(publisher, epoch, value[0]))['state'] == 'connected'


@pytest.mark.asyncio
async def test_blocked_send_times_out_and_removes_only_its_stream(monkeypatch):
    monkeypatch.setattr('agent_message_bus.presence.PRESENCE_SEND_TIMEOUT_SECONDS', 0.01)
    publisher = PushPublisher(enabled=True)
    replacement = await publisher.open_presence_stream('private-session', 'codex')

    async def downstream(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/event-stream')]})
        await send({'type': 'http.response.body', 'body': b': ping\n\n', 'more_body': True})

    async def blocked_send(message):
        if message['type'] == 'http.response.body':
            await asyncio.Event().wait()

    scope = {'type': 'http', 'method': 'GET', 'path': '/mcp', 'state': {
        'mcp_presence_agent': 'codex', 'mcp_presence_session_id': 'private-session'}}
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(MCPStreamPresenceMiddleware(downstream, publisher)(scope, None, blocked_send), 1)
    result = project_bus_connections(await publisher.presence_status())['codex']
    assert result['active_streams'] == 1 and result['last_stream_write_at'] is None
    await publisher.close_presence_stream(replacement)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('valid_for_seconds', float('nan')), ('valid_for_seconds', True),
    ('valid_for_seconds', 31), ('valid_for_seconds', 0), ('stale_streams', True), ('stale_streams', -1)])
async def test_invalid_freshness_fails_closed(field, value):
    publisher = PushPublisher(enabled=True)
    snapshot = await publisher.presence_status()
    snapshot['agents'][0][field] = value
    assert {item['state'] for item in project_bus_connections(snapshot).values()} == {'unavailable'}
