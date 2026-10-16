import asyncio

import pytest

from agent_message_bus.push import PushPublisher


@pytest.mark.asyncio
@pytest.mark.parametrize('old_fails', [False, True])
@pytest.mark.parametrize('unsubscribe_first', [False, True])
async def test_old_notification_cannot_modify_replacement_registration(old_fails, unsubscribe_first):
    publisher = PushPublisher(enabled=True)
    old_entered, old_release = asyncio.Event(), asyncio.Event()
    new_entered, new_release = asyncio.Event(), asyncio.Event()

    class Session:
        calls = 0

        async def send_resource_updated(self, uri):
            self.calls += 1
            call = self.calls
            if call == 1:
                old_entered.set()
                await old_release.wait()
                if old_fails:
                    raise OSError('old stream failed')
            elif call == 2:
                new_entered.set()
                await new_release.wait()

    session = Session()
    stream = await publisher.open_presence_stream('same-session', 'codex')
    await publisher.subscribe('same-session', 'codex', session)
    old = asyncio.create_task(publisher.publish_committed('codex'))
    new = None
    try:
        await asyncio.wait_for(old_entered.wait(), 1)
        if unsubscribe_first:
            await publisher.unsubscribe('same-session', 'codex')
        await publisher.subscribe('same-session', 'codex', session)
        new = asyncio.create_task(publisher.publish_committed('codex'))
        await asyncio.wait_for(new_entered.wait(), 1)
        old_release.set()
        await asyncio.wait_for(old, 1)
        row = next(row for row in (await publisher.presence_status())['agents'] if row['agent'] == 'codex')
        assert row['active_streams'] == 1 and row['inbox_subscriptions'] == 1 and row['subscribed']
        assert publisher.subscription_count == 1
        assert publisher.pending_count == 1, 'old completion must not erase the replacement pending send'
        new_release.set()
        assert await asyncio.wait_for(new, 1) == 1
        assert publisher.pending_count == 0
        assert await publisher.publish_committed('codex') == 1
    finally:
        old_release.set(); new_release.set()
        await asyncio.gather(old, *([new] if new else []), return_exceptions=True)
        await publisher.close_presence_stream(stream)
