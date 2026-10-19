"""Explicit private core lifecycle; synthetic remote effects, not deployment."""
import asyncio
import sys

import httpx
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubError
from tests.test_hub_queued_pilot import rig as pilot_rig, submit, machine_job

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux owner/native feed')


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
async def rig(pilot_rig):
    rig = pilot_rig
    rig.settings = Settings(rig.repo.path,
        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
        AuthConfig('https://hub.invalid'))
    rig.feed = NativeHubEventFeed(rig.repo.path)
    rig.runtime = QueuedPilotRuntime(rig.bridge, rig.feed)
    rig.releases = []
    try:
        yield rig
    finally:
        for release in rig.releases:
            release.set()
        await rig.runtime.stop()
        await rig.feed.stop()


def build(rig, **kwargs):
    return create_app(rig.settings, repository=rig.repo, work_queue=rig.queue,
        event_feed=rig.feed, queued_pilot_runtime=rig.runtime, **kwargs)


def test_constructor_is_inert_and_single_use_binding_rejects_before_session_write(rig):
    app = build(rig)
    assert app.state.queued_pilot_runtime is rig.runtime
    assert not rig.feed.available and rig.feed._source is None
    assert rig.client.submissions == [] and not rig.runtime.status['running']
    with rig.repo._connection() as db:
        before = db.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0]
    with pytest.raises(ValueError, match='application_binding'):
        build(rig)
    with rig.repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0] == before


@pytest.mark.parametrize('mismatch', ['feed', 'queue', 'runtime', 'legacy'])
def test_explicit_binding_mismatches_fail_closed(rig, mismatch):
    kwargs = dict(repository=rig.repo, work_queue=rig.queue,
        event_feed=rig.feed, queued_pilot_runtime=rig.runtime)
    if mismatch == 'feed':
        kwargs['event_feed'] = NativeHubEventFeed(rig.repo.path)
    elif mismatch == 'queue':
        kwargs['work_queue'] = None
    elif mismatch == 'runtime':
        kwargs['queued_pilot_runtime'] = object()
    else:
        kwargs['container_pilot_workflow'] = rig.workflow
    with pytest.raises(ValueError):
        create_app(rig.settings, **kwargs)
    assert not rig.feed.available and not rig.runtime.status['running']
    assert rig.client.submissions == []


@pytest.mark.asyncio
async def test_private_core_lifespan_runs_queue_and_exposes_sanitized_runtime(rig):
    _, job = submit(rig)
    app = build(rig)
    async with app.router.lifespan_context(app):
        await until(lambda: rig.runtime.status['last_job'] is not None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://hub.invalid') as client:
            response = await client.get('/api/hub/v1/autonomy', headers={'Authorization': 'Bearer test-codex'})
            assert response.status_code == 200
            data = response.json()
            assert data['configured'] is True and data['state'] == 'active'
            assert data['queued_pilot']['last_job'] == {'id': job['id'], 'state': 'succeeded'}
            assert data['receipts'] == []
            denied = await client.post('/api/hub/v1/container-pilot/runs',
                headers={'Authorization': 'Bearer test-codex'}, json={})
            assert denied.status_code in {422, 423}
        assert len(rig.client.submissions) == 2
    assert rig.runtime.status['state'] == 'stopped' and not rig.feed.available
    # Application composes startup/shutdown; it never takes the caller's owner.
    rig.owner.assert_active(rig.repo.path)


@pytest.mark.asyncio
async def test_native_source_unavailable_prevents_application_readiness(rig):
    rig.feed._support_check = lambda _: (False, 'test unsupported')
    app = build(rig)
    entered = False
    # MCP's AnyIO task group wraps startup exceptions without making the app
    # ready. Inspect the actual cause rather than requiring an unwrapped error.
    with pytest.raises(ExceptionGroup) as failed:
        async with app.router.lifespan_context(app):
            entered = True
    errors = failed.value.subgroup(HubError)
    assert errors is not None and len(errors.exceptions) == 1
    assert errors.exceptions[0].code == 'queued_pilot_startup_unavailable'
    assert entered is False and not rig.feed.available
    assert not rig.runtime.status['running'] and rig.client.submissions == []
    rig.owner.assert_active(rig.repo.path)


@pytest.mark.asyncio
async def test_repeated_lifespan_cancellation_drains_accepted_phase_before_feed_stop(rig):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rig.releases.append(release)
    original = rig.client.submit_command
    async def blocked(command):
        entered.set()
        await release.wait()
        result = await original(command)
        finished.set()
        return result
    rig.client.submit_command = blocked
    _, job = submit(rig)
    app = build(rig)
    async def serve():
        async with app.router.lifespan_context(app):
            await asyncio.Event().wait()
    serving = asyncio.create_task(serve())
    await asyncio.wait_for(entered.wait(), 5)
    serving.cancel()
    await until(lambda: not rig.runtime.admission_allowed())
    serving.cancel()
    await asyncio.sleep(0)
    assert not serving.done() and rig.feed.available
    assert machine_job(rig) == job['id']
    rig.owner.assert_active(rig.repo.path)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(serving, 5)
    assert finished.is_set() and len(rig.client.submissions) == 1
    assert not rig.feed.available and not rig.runtime.status['running']
    assert rig.queue.get_job(job['id'])['state'] == 'held'
    assert machine_job(rig) == job['id']
    rig.owner.assert_active(rig.repo.path)
