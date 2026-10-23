"""Synthetic authenticated MCP concurrency, not real-model or host acceptance.

Run each timing scenario in a fresh interpreter with a hard parent deadline.
An external observer timestamps submission while model/index work is blocked;
the inline negative control must fail the same unchanged 250 ms budget.
"""
import asyncio
import hashlib
import json
import itertools
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import pytest


async def scenario(root, operation, inline):
    import httpx
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.knowledge import KnowledgeRepository
    from agent_message_bus.semantic import SemanticIndex
    from agent_message_bus import mcp_server
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from engineering_hub.frontends import HubFrontends
    from tests.test_hub_unified_mcp import AUTH, BASE, headers, result, security

    source_root = Path(__file__).resolve().parent.parent
    source_paths = (Path(mcp_server.__file__).resolve(),
                    Path(sys.modules[SemanticIndex.__module__].__file__).resolve())
    assert source_paths == (source_root/'src/agent_message_bus/mcp_server.py',
                            source_root/'src/agent_message_bus/semantic.py')
    provenance = {str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in source_paths}

    entered, release = threading.Event(), threading.Event()
    work_threads = []
    completed = []

    class Embedder:
        model_name = 'synthetic-concurrency-only'

        def embed(self, texts):
            work_threads.append(threading.get_ident())
            if operation == 'semantic_search' and not entered.is_set():
                entered.set()
                assert release.wait(4), 'bounded synthetic embedder was not released'
            for _ in texts:
                yield [1., 0., 0.]

    class Index(SemanticIndex):
        def status(self):
            if operation == 'semantic_status':
                work_threads.append(threading.get_ident())
                entered.set()
            value = super().status()
            completed.append(time.monotonic())
            return value

        def search(self, *args, **kwargs):
            value = super().search(*args, **kwargs)
            completed.append(time.monotonic())
            return value

    if inline:
        async def inline_work(function, *args, **kwargs):
            return function(*args, **kwargs)
        mcp_server._semantic_work = inline_work

    bus = create_bus(db_path=root/'bus.db', audit_path=root/'audit.jsonl',
        file_root=root/'files', auth=AUTH, push_events_operational=False,
        transport_security_settings=security())
    index = Index(root/'bus.db', root/'files', embedder=Embedder())
    bus.state.service.semantic = index
    KnowledgeRepository(root/'bus.db').put(project='demo', path='notes.md',
        title='Synthetic note', content='A synthetic automobile note.',
        author='test', expected_version=0)
    core = create_app(Settings(root/'hub.db', AUTH, AuthConfig('https://hub.test',
        human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://synthetic.cloudflareaccess.com',
        cloudflare_audience='test-only', cloudflare_allowed_emails=('test@example.test',)),
        agent_allowed_hosts=('127.0.0.1:8000',)), bus_app=bus)
    pair = HubFrontends(core)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    samples, submitted, overlap = [], [], []
    request_ids = itertools.count(1)
    observers = []
    observer_ready = asyncio.Event()
    holder_ready = threading.Event()

    def hold_index():
        with index._lock:
            holder_ready.set()
            assert release.wait(4), 'bounded synthetic status lock was not released'

    holder = threading.Thread(target=hold_index) if operation == 'semantic_status' else None
    if holder:
        holder.start()
        assert await asyncio.to_thread(holder_ready.wait, 2)

    async with pair.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents),
                                     base_url=BASE, timeout=5) as client:
            async def rpc(method, params=None, session=None):
                return await client.post('/mcp', headers=headers(session=session), json={
                    'jsonrpc': '2.0', 'id': next(request_ids), 'method': method, 'params': params or {}})

            denied = await client.post('/mcp', headers=headers(token=None), json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': operation, 'arguments': {}}})
            assert denied.status_code == 401 and not work_threads
            initial = await rpc('initialize', {'protocolVersion': '2025-06-18',
                'capabilities': {}, 'clientInfo': {'name': 'semantic-test', 'version': '1'}})
            result(initial)
            session = initial.headers['mcp-session-id']
            assert (await client.post('/mcp', headers=headers(session=session), json={
                'jsonrpc': '2.0', 'method': 'notifications/initialized'})).status_code == 202

            async def observe(scheduled):
                try:
                    for number in range(5):
                        started = scheduled if number == 0 else time.monotonic()
                        ping, read = await asyncio.gather(rpc('ping', session=session),
                            client.get('/api/hub/v1/overview', headers=headers()))
                        samples.append(time.monotonic() - started)
                        assert ping.status_code == read.status_code == 200
                        result(ping)
                        read.json()
                finally:
                    observer_ready.set()

            def submit_observer():
                assert entered.wait(3), 'semantic work did not enter'
                scheduled = time.monotonic()
                submitted.append(scheduled)
                def schedule():
                    observers.append(asyncio.create_task(observe(scheduled)))
                loop.call_soon_threadsafe(schedule)
                # Release from outside the event loop, including in the negative
                # control. A stalled loop cannot accidentally extend the fixture.
                time.sleep(.5)
                release.set()

            watcher = threading.Thread(target=submit_observer)
            watcher.start()
            arguments = {'query': 'automobile', 'project': 'demo', 'limit': 1} if operation == 'semantic_search' else {}
            work = asyncio.create_task(rpc('tools/call', {'name': operation,
                'arguments': arguments}, session=session))
            try:
                response = result(await work)
                assert response.get('isError') is not True
                structured = response['structuredContent']
                if operation == 'semantic_search':
                    assert structured['mode'] == 'semantic'
                    assert structured['results'][0]['path'] == 'notes.md'
                else:
                    assert structured['ready'] is True
                await observer_ready.wait()
                await asyncio.gather(*observers)
                assert submitted[0] < completed[0], 'observer was not submitted during blocked work'
                overlap.append(True)
                assert (await client.delete('/mcp', headers=headers(session=session))).status_code == 200
            finally:
                release.set()
                await asyncio.to_thread(watcher.join, 2)
                assert not watcher.is_alive()
                if holder:
                    await asyncio.to_thread(holder.join, 2)
                    assert not holder.is_alive()
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, *observers, return_exceptions=True)
    assert len(samples) == 5 and overlap == [True]
    assert work_threads and all((thread == loop_thread) == inline for thread in work_threads)
    return {'operation': operation, 'inline': inline, 'samples': samples,
        'max_seconds': max(samples), 'latency_limit': .25,
        'accepted': max(samples) < .25, 'functional': True,
        'overlap': True, 'session_deleted': True, 'lifespan_drained': True, 'source': provenance}


@pytest.mark.parametrize('operation', ['semantic_search', 'semantic_status'])
@pytest.mark.parametrize('inline', [False, True], ids=['off-loop', 'inline-negative'])
def test_authenticated_semantic_work_keeps_other_requests_responsive(operation, inline):
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root/'src'), str(root))),
               PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.run([sys.executable, '-B', '-m', 'tests.test_semantic_async_mcp',
        operation, str(int(inline))], cwd=root, env=env, capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stdout + child.stderr
    records = [line.removeprefix('SEMANTIC_PROBE ') for line in child.stdout.splitlines()
               if line.startswith('SEMANTIC_PROBE ')]
    assert len(records) == 1
    report = json.loads(records[0])
    assert report['functional'] and report['overlap'] and report['session_deleted'] and report['lifespan_drained']
    assert report['latency_limit'] == .25 and len(report['samples']) == 5
    assert report['max_seconds'] == max(report['samples'])
    for relative, digest in report['source'].items():
        assert digest == hashlib.sha256((root/relative).read_bytes()).hexdigest()
    assert report['accepted'] is (not inline), report
    if inline:
        assert report['max_seconds'] >= .5, report


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_semantic_work_retains_thread_after_repeated_direct_cancellation(fails):
    from agent_message_bus.mcp_server import _semantic_work
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocking():
        entered.set()
        try:
            assert release.wait(3)
            if fails:
                raise ValueError('synthetic worker failure')
        finally:
            finished.set()

    task = asyncio.create_task(_semantic_work(blocking))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('fails', [False, True])
async def test_semantic_work_retains_thread_after_anyio_scope_cancellation(fails):
    import anyio
    from agent_message_bus.mcp_server import _semantic_work
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    completed = []

    def blocking():
        entered.set()
        try:
            assert release.wait(3)
            if fails:
                raise ValueError('synthetic worker failure after scope cancellation')
        finally:
            finished.set()

    async def run():
        await _semantic_work(blocking)
        completed.append(finished.is_set())

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        assert await asyncio.to_thread(entered.wait, 2)
        group.cancel_scope.cancel()
        # A separate thread releases work despite the cancelled caller scope.
        threading.Timer(.05, release.set).start()
    assert finished.is_set() and completed == []


@pytest.mark.asyncio
async def test_semantic_admission_queues_before_allocating_threads_and_cancels_waiters(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent_message_bus import mcp_server
    from agent_message_bus.api import Service
    from agent_message_bus.audit import AuditLog
    from agent_message_bus.auth import AuthSettings
    from agent_message_bus.database import MessageRepository
    from agent_message_bus.retention import RetentionPolicy
    from tests.test_shared_mcp import context

    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def held_work(operation, *args, **kwargs):
        calls.append(operation)
        entered.set()
        await release.wait()
        return operation(*args, **kwargs)

    monkeypatch.setattr(mcp_server, '_semantic_work', held_work)
    service = Service(MessageRepository(tmp_path/'bus.db'), AuditLog(tmp_path/'audit.jsonl'),
        AuthSettings('h', 'c', claude_token='a'), RetentionPolicy())
    service.semantic = SimpleNamespace(search=lambda **kwargs: [], status=lambda: {'ready': True})
    tools = {tool.name: tool for tool in mcp_server.build_mcp(service)._tool_manager.list_tools()}
    active = asyncio.create_task(tools['semantic_search'].fn(query='synthetic', ctx=context('c')))
    queued = []
    try:
        await entered.wait()
        queued = [asyncio.create_task(tools['semantic_status'].fn(ctx=context('c'))) for _ in range(5)]
        await asyncio.sleep(.01)
        assert len(calls) == 1
        for task in queued:
            task.cancel()
        await asyncio.gather(*queued, return_exceptions=True)
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(active, *queued, return_exceptions=True)
    assert await active == {'mode': 'semantic', 'results': []}
    assert await tools['semantic_status'].fn(ctx=context('c')) == {'ready': True}
    assert len(calls) == 2


async def cancellation_scenario(root, action):
    """Actual loopback RPC interruption while a bounded synthetic thread runs."""
    import httpx
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.semantic import SemanticIndex
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from engineering_hub.frontends import HubFrontends
    from tests.test_hub_frontends import Feed
    from tests.test_hub_ordinary_shutdown import start
    from tests.test_hub_unified_mcp import AUTH, headers, result, security

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    order = []

    class Embedder:
        model_name = 'synthetic-cancellation-only'

        def embed(self, texts):
            entered.set()
            try:
                assert release.wait(4), 'bounded synthetic worker was not released'
                for _ in texts:
                    yield [1., 0., 0.]
            finally:
                order.append('worker_finished')
                finished.set()

    class ObservedFeed(Feed):
        async def stop(self):
            order.append('feed_stop')
            await super().stop()

    bus = create_bus(db_path=root/'bus.db', audit_path=root/'audit.jsonl',
        file_root=root/'files', auth=AUTH, push_events_operational=False,
        transport_security_settings=security())
    bus.state.service.semantic = SemanticIndex(root/'bus.db', root/'files', embedder=Embedder())
    tool = bus.state.mcp._tool_manager.get_tool('semantic_search')
    original = tool.fn

    async def observed_tool(**kwargs):
        try:
            return await original(**kwargs)
        finally:
            order.append('handler_finished')

    tool.fn = observed_tool
    feed, stop = ObservedFeed(), asyncio.Event()
    core = create_app(Settings(root/'hub.db', AUTH, AuthConfig('https://hub.test',
        human_auth_mode='cloudflare_access', cloudflare_issuer='https://synthetic.cloudflareaccess.com',
        cloudflare_audience='test-only', cloudflare_allowed_emails=('test@example.test',)),
        agent_allowed_hosts=('127.0.0.1:8000',)), bus_app=bus, event_feed=feed)
    pair = HubFrontends(core)
    owner, url = await start(pair, stop)
    work = delete = None
    releaser = None
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5,
            headers={**headers(), 'Host': '127.0.0.1:8000'}) as client:
            initial = await client.post(url+'/mcp', json={'jsonrpc': '2.0', 'id': 1,
                'method': 'initialize', 'params': {'protocolVersion': '2025-06-18',
                    'capabilities': {}, 'clientInfo': {'name': 'semantic-cancellation-test', 'version': '1'}}})
            result(initial)
            client.headers['Mcp-Session-Id'] = initial.headers['mcp-session-id']
            assert (await client.post(url+'/mcp', json={'jsonrpc': '2.0',
                'method': 'notifications/initialized'})).status_code == 202
            work = asyncio.create_task(client.post(url+'/mcp', json={'jsonrpc': '2.0', 'id': 2,
                'method': 'tools/call', 'params': {'name': 'semantic_search',
                    'arguments': {'query': 'synthetic'}}}))
            assert await asyncio.to_thread(entered.wait, 2)
            assert not finished.is_set()
            if action == 'cancel-rpc':
                assert (await client.post(url+'/mcp', json={'jsonrpc': '2.0',
                    'method': 'notifications/cancelled', 'params': {'requestId': 2,
                    'reason': 'synthetic cancellation'}})).status_code == 202
                work.cancel()  # Also detach the real HTTP caller.
                await asyncio.gather(work, return_exceptions=True)
            else:
                delete = asyncio.create_task(client.delete(url+'/mcp'))
            await asyncio.sleep(.03)
            assert 'handler_finished' not in order and not finished.is_set()
            # Bounded release is independent of the event loop and remains safe
            # if cancellation regresses into a blocking loop implementation.
            releaser = threading.Timer(.3, release.set)
            releaser.start()
            owner.cancel()
            await asyncio.sleep(.03)
            owner.cancel()
            await asyncio.sleep(.03)
            assert not owner.done() and feed.stops == 0
            assert 'handler_finished' not in order and not finished.is_set()
            with pytest.raises(asyncio.CancelledError):
                await owner
            order.append('owner_finished')
            if delete is not None:
                assert (await delete).status_code == 200
            await asyncio.gather(work, return_exceptions=True)
        assert order.index('worker_finished') < order.index('handler_finished') < order.index('owner_finished')
        assert feed.stops == 1 and pair.state == 'failed'
        return {'action': action, 'order': order, 'worker_owned': True,
                'owner_finished_after_worker': True, 'owner_cancelled': True,
                'feed_stopped_before_worker': order.index('feed_stop') < order.index('worker_finished')}
    finally:
        release.set()
        stop.set()
        await asyncio.gather(owner, *(task for task in (work, delete) if task), return_exceptions=True)
        if releaser:
            await asyncio.to_thread(releaser.join, 2)
            assert not releaser.is_alive()


@pytest.mark.parametrize('action', ['cancel-rpc', 'delete-session'])
def test_active_semantic_rpc_remains_owned_until_thread_finishes(action):
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root/'src'), str(root))),
               PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.run([sys.executable, '-B', '-m', 'tests.test_semantic_async_mcp',
        action, '0'], cwd=root, env=env, capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stdout + child.stderr
    records = [line.removeprefix('SEMANTIC_PROBE ') for line in child.stdout.splitlines()
               if line.startswith('SEMANTIC_PROBE ')]
    assert len(records) == 1
    report = json.loads(records[0])
    assert report['worker_owned'] and report['owner_finished_after_worker'] and report['owner_cancelled']
    assert 'Request already responded to' not in child.stderr
    assert 'crashed' not in child.stderr


if __name__ == '__main__':
    import faulthandler
    from tests.responsiveness_probe import strict_resource_audit
    faulthandler.dump_traceback_later(10)
    try:
        with strict_resource_audit():
            with tempfile.TemporaryDirectory(prefix='eh-semantic-probe-') as directory:
                if sys.argv[1] in {'cancel-rpc', 'delete-session'}:
                    value = asyncio.run(cancellation_scenario(Path(directory), sys.argv[1]))
                else:
                    value = asyncio.run(scenario(Path(directory), sys.argv[1], bool(int(sys.argv[2]))))
        print('SEMANTIC_PROBE ' + json.dumps(value))
    finally:
        faulthandler.cancel_dump_traceback_later()
