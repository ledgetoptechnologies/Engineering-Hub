"""Current cold enrollment -> real inactive owner/native streams -> safe drain.

Disposable loopback and synthetic credentials only. The held POST is a synthetic
handler with no database effect. This does not test a protected NAS mount,
credential provisioning, enrolled restore transfer, executor or machine reboot.
"""
import asyncio
from contextlib import closing
from io import BytesIO
import shutil
import sqlite3
import sys
from types import SimpleNamespace
import uuid

import httpx
from PIL import Image
import pytest
from starlette.responses import JSONResponse
from sse_starlette.sse import _shutdown_watcher

from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import MessageRepository
from engineering_hub import owned_inactive_hub as service
from engineering_hub import pilot_enrollment as enrollment
from engineering_hub.app import Settings, create_app
from engineering_hub.attachments import EntityImageRepository
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from tests.test_hub_gateway_queued_pilot import signed_access, BASE
from tests.test_hub_pilot_enrollment import ledger, snapshot


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux owner and native event sources')


@pytest.mark.asyncio
async def test_current_enrollment_then_owned_native_stream_shutdown_retains_owner_and_source(ledger, tmp_path, monkeypatch):
    source, repo, *_ = ledger
    auth, _ = signed_access(monkeypatch)
    agent_auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude',
                               allowed_ips=frozenset({'127.0.0.1'}))
    original_app = create_app(Settings(source, agent_auth, auth), repository=repo)
    project = original_app.state.project_registry.create_project('human:42', 'client-notes', 'Notes', 'project')
    task = repo.create_task('human:42', project['id'], 'Retain context', 'task', details='Complete context',
                             links=[{'label': 'Reference', 'url': 'https://example.invalid'}])
    question = repo.create_decision('claude', project['id'], 'Explain?', ['Now', 'Later'], allow_text=True)
    content = BytesIO()
    with Image.new('RGB', (3, 3), 'green') as image:
        image.save(content, format='PNG')
    image_id = str(uuid.uuid4())
    images = EntityImageRepository(repo)
    images.put(actor='codex', image_id=image_id, kind='decision', entity_id=question['id'],
                expected_revision=question['revision'], content=content.getvalue(), media_type='image/png')
    expected_image = images.image(image_id=image_id, actor='human:42')
    before = snapshot(source)
    original_bytes = source.read_bytes()
    private = tmp_path / 'enrolled'
    private.mkdir(mode=0o700)
    target = private / 'hub.sqlite3'
    shutil.copyfile(source, target)
    target.chmod(0o600)
    plan = enrollment.inspect_plan(target)
    assert plan['snapshot'] == before
    enrolled = enrollment.apply_plan(plan, enrollment.plan_digest(plan), writers_stopped=True, backup_verified=True)

    bus_root = tmp_path / 'bus'
    bus_root.mkdir()
    bus_repo = MessageRepository(bus_root / 'messages.sqlite3')
    retained_message = bus_repo.insert(sender='hermes', recipient='codex', message_type='info', body={'fixture': True})
    (bus_root / 'audit.jsonl').touch()
    (bus_root / 'files').mkdir()
    paths = {'AGENT_BUS_DB': bus_root / 'messages.sqlite3', 'AGENT_BUS_AUDIT': bus_root / 'audit.jsonl',
             'AGENT_BUS_FILE_ROOT': bus_root / 'files'}
    state = SimpleNamespace(owner=None, core=None)
    entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ready = asyncio.get_running_loop().create_future()
    previous_tasks = set(asyncio.all_tasks())
    actual_owner = service.PilotOwner
    actual_app = service.create_app
    monkeypatch.delenv('HUB_BUS_STARTUP_CLEANUP', raising=False)

    def acquire(path, *, require_existing):
        assert require_existing is True
        state.owner = actual_owner(path, require_existing=True, allow_ephemeral_test_storage=True)
        return state.owner

    def build(*args, **kwargs):
        state.core = actual_app(*args, **kwargs)

        @state.core.middleware('http')
        async def synthetic_post(request, call_next):
            if request.method == 'POST' and request.url.path == BASE + '/tasks':
                assert await request.body() == b'accepted'
                entered.set()
                await release.wait()
                state.owner.assert_active(target)
                assert state.core.state.hub_frontends.state == 'stopping'
                return JSONResponse({'synthetic_completed': True})
            return await call_next(request)
        return state.core

    monkeypatch.setattr(service, 'PilotOwner', acquire)
    monkeypatch.setattr(service, 'create_app', build)
    serving = asyncio.create_task(service.serve_owned_inactive_hub(Settings(target, agent_auth, auth),
        bus_paths=paths, agent_host='127.0.0.1', agent_port=0, browser_host='127.0.0.1', browser_port=0,
        stop_event=stop, install_signals=False, on_ready=lambda a, b: ready.set_result((a, b))))
    mutation = None
    try:
        addresses = await asyncio.wait_for(asyncio.shield(ready), 5)
        url = 'http://127.0.0.1:' + str(addresses[0][1])
        headers = {'Authorization': 'Bearer synthetic-codex', 'Host': 'hub.example',
                   'Accept': 'application/json, text/event-stream'}
        async with httpx.AsyncClient(trust_env=False, headers=headers, timeout=5) as client:
            initialized = await client.post(url + '/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                           'clientInfo': {'name': 'enrollment-drain-fixture', 'version': '1'}}})
            assert initialized.status_code == 200
            client.headers['Mcp-Session-Id'] = initialized.headers['Mcp-Session-Id']
            client.headers['MCP-Protocol-Version'] = '2025-06-18'
            assert (await client.post(url + '/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})).status_code == 202
            async with client.stream('GET', url + BASE + '/events') as events, client.stream('GET', url + '/mcp') as mcp:
                assert events.status_code == mcp.status_code == 200
                lines = events.aiter_lines()
                assert (await anext(lines)).startswith('data: ')
                bus = state.core.state.embedded_bus
                assert bus.state.push_event_source.running
                assert bus.state.push_publisher._presence_streams
                mutation = asyncio.create_task(client.post(url + BASE + '/tasks', content=b'accepted'))
                await asyncio.wait_for(entered.wait(), 3)
                stop.set()
                # Idle GET subscriptions finish while the accepted mutation and
                # exact inode-bound owner remain live; no source is failed early.
                async def finish_events():
                    return [line async for line in lines]
                await asyncio.wait_for(asyncio.gather(finish_events(), mcp.aread()), 3)
                serving.cancel()
                await asyncio.sleep(.05)
                serving.cancel()
                await asyncio.sleep(.05)
                assert not serving.done() and not mutation.done()
                state.owner.assert_active(target)
                assert state.owner.previous_epoch == enrolled['epoch']
                assert bus.state.push_event_source.running
                assert not bus.state.push_publisher._presence_streams
                with pytest.raises(PilotOwnershipError):
                    PilotOwner(target, require_existing=True, allow_ephemeral_test_storage=True)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.core.state.hub_frontends.agents),
                                               base_url='http://hub.example', headers=headers) as direct:
                    assert (await direct.get(BASE + '/tasks')).status_code == 503
                release.set()
                assert (await mutation).json() == {'synthetic_completed': True}
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(serving, 3)
        assert state.core.state.hub_frontends.state == 'closed'
        assert not bus.state.push_event_source.running
        with pytest.raises(PilotOwnershipError):
            state.owner.assert_active(target)
        with PilotOwner(target, require_existing=True, allow_ephemeral_test_storage=True) as successor:
            from engineering_hub.repository import HubRepository
            reopened = HubRepository(target, pilot_owner=successor)
            assert reopened.get_task(task['id']) == task
            assert EntityImageRepository(reopened).image(image_id=image_id, actor='human:42') == expected_image
            with closing(sqlite3.connect(target)) as db:
                assert enrollment._snapshot(db, enrolled=True) == before
        assert bus_repo.get(retained_message.id) == retained_message
        assert source.read_bytes() == original_bytes and snapshot(source) == before
        with pytest.raises(enrollment.EnrollmentError):
            enrollment.inspect_plan(target)
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(asyncio.gather(serving, return_exceptions=True), 5)
        if mutation is not None:
            await asyncio.gather(mutation, return_exceptions=True)
        # Only this SDK's newly created watcher coroutine is fixture cleanup.
        watchers = [task for task in asyncio.all_tasks() if task not in previous_tasks and not task.done()
                    and getattr(task.get_coro(), 'cr_code', None) is _shutdown_watcher.__code__]
        for task in watchers:
            task.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
