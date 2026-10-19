"""Bounded real-thread ASGI lifecycle tests; no daemon or external service."""
import asyncio
from contextvars import ContextVar
import json
import threading
import time
from uuid import uuid4

import pytest

pytest.importorskip('fcntl', reason='Optional host adapter package requires Linux')

from engineering_hub.container_pilot_client import ContainerPilotRequest, ContainerPilotResponse
from hub_container_executor import asgi as module
from hub_container_executor.asgi import COMMANDS, create_app


CONTEXT = ContextVar('asgi_test_context', default='missing')


def command():
    return ContainerPilotRequest(schema_version='container_pilot_v1', command_id=str(uuid4()),
        run_id=str(uuid4()), target_id='engineering-hub:staging', action='deploy_container',
        manifest_digest='sha256:' + 'a' * 64, candidate_commit='b' * 40,
        policy_revision=1, fencing_generation=1)


class BlockingAdapter:
    def __init__(self):
        self.release, self.started, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.block = True
        self.fail = False
        self.calls = []
        self.contexts = []
        self.thread_ids = []

    def invoke(self, operation, request=None):
        self.calls.append(operation)
        self.contexts.append(CONTEXT.get())
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        if self.block:
            # Hard unit-test bound: broken event-loop offloading cannot hang CI.
            if not self.release.wait(2):
                raise RuntimeError('unit-test backend release timed out')
        self.finished.set()
        if self.fail:
            raise RuntimeError('synthetic adapter failure')
        if request is None:
            return None
        return ContainerPilotResponse(**request.model_dump(), state='held', reason='inspect_required',
            evidence=dict(container_id=None, image_config_id=None, health='unknown', evidence_verified=False),
            replayed=False)

    def get(self, identity): return self.invoke('get')
    def reconcile(self, identity): return self.invoke('reconcile')
    def submit(self, request): return self.invoke('submit', request)


async def invoke(app, operation, *, authenticated=True):
    item = command()
    path = COMMANDS if operation == 'submit' else COMMANDS + '/' + item.command_id
    if operation == 'reconcile': path += '/reconcile'
    raw = json.dumps(item.model_dump()).encode() if operation == 'submit' else b''
    headers = [(b'authorization', b'Bearer synthetic-test-token')] if authenticated else []
    if operation == 'submit': headers.extend([(b'content-type', b'application/json'),
                                             (b'content-length', str(len(raw)).encode())])
    events = []
    async def receive(): return dict(type='http.request', body=raw, more_body=False)
    async def send(event): events.append(event)
    await app(dict(type='http', method='GET' if operation == 'get' else 'POST', path=path,
                   query_string=b'', headers=headers), receive, send)
    return events[0]['status'], json.loads(events[-1]['body'])


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate(): await asyncio.sleep(0.001)


@pytest.mark.parametrize('operation', ['submit', 'get', 'reconcile'])
async def test_real_thread_does_not_block_loop_and_busy_rejects_without_waiter(operation):
    adapter = BlockingAdapter()
    app = create_app(adapter, bearer_token='synthetic-test-token')
    marker = CONTEXT.set('request-context')
    task = asyncio.create_task(invoke(app, operation))
    try:
        began = time.monotonic()
        await until(adapter.started.is_set)
        assert time.monotonic() - began < 0.5
        assert not task.done() and not adapter.finished.is_set()
        assert adapter.thread_ids == [adapter.thread_ids[0]]
        assert adapter.thread_ids[0] != threading.get_ident()
        assert adapter.contexts == ['request-context']
        # All operation types share ONE admission gate; none queues a worker.
        for other in ('submit', 'get', 'reconcile'):
            status, body = await asyncio.wait_for(invoke(app, other), 0.5)
            assert status == 503 and body == {'error': {'code': 'command_busy'}}
        assert adapter.calls == [operation]
        assert (await invoke(app, 'get', authenticated=False))[0] == 401
        assert adapter.calls == [operation]
    finally:
        CONTEXT.reset(marker)
        adapter.release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert adapter.finished.is_set()
    adapter.block = False
    assert (await invoke(app, 'get'))[0] == 404
    assert adapter.calls == [operation, 'get']


@pytest.mark.parametrize('operation', ['submit', 'get', 'reconcile'])
async def test_repeated_request_cancellation_keeps_gate_until_actual_thread_finishes(operation):
    adapter = BlockingAdapter()
    app = create_app(adapter, bearer_token='synthetic-test-token')
    task = asyncio.create_task(invoke(app, operation))
    try:
        await until(adapter.started.is_set)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not adapter.finished.is_set()
        assert (await invoke(app, 'get')) == (503, {'error': {'code': 'command_busy'}})
        assert adapter.calls == [operation]
        adapter.release.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 1)
        assert adapter.finished.is_set()
    finally:
        adapter.release.set()
        await asyncio.gather(task, return_exceptions=True)
    adapter.block = False
    assert (await invoke(app, 'get'))[0] == 404


async def test_adapter_exception_releases_gate_only_after_thread_termination():
    adapter = BlockingAdapter()
    adapter.fail = True
    app = create_app(adapter, bearer_token='synthetic-test-token')
    task = asyncio.create_task(invoke(app, 'submit'))
    try:
        await until(adapter.started.is_set)
        assert (await invoke(app, 'get'))[0] == 503
        adapter.release.set()
        with pytest.raises(RuntimeError, match='synthetic adapter failure'): await task
        assert adapter.finished.is_set()
    finally:
        adapter.release.set()
        await asyncio.gather(task, return_exceptions=True)
    adapter.fail = False
    adapter.block = False
    assert (await invoke(app, 'get'))[0] == 404


async def test_wrong_process_denied_before_adapter_or_inherited_lock(monkeypatch):
    adapter = BlockingAdapter()
    adapter.block = False
    app = create_app(adapter, bearer_token='synthetic-test-token')
    class ForbiddenLock:
        def acquire(self, *args, **kwargs): pytest.fail('inherited lock must not be touched')
    app._operation = ForbiddenLock()
    pid = module.os.getpid()
    monkeypatch.setattr(module.os, 'getpid', lambda: pid + 1)
    assert (await invoke(app, 'get')) == (503, {'error': {'code': 'executor_unavailable'}})
    assert adapter.calls == []


async def test_active_operation_does_not_block_other_request_body_timeout():
    adapter = BlockingAdapter()
    app = create_app(adapter, bearer_token='synthetic-test-token', request_body_timeout_seconds=0.02)
    task = asyncio.create_task(invoke(app, 'submit'))
    events = []
    async def receive(): await asyncio.Future()
    async def send(event): events.append(event)
    try:
        await until(adapter.started.is_set)
        scope = dict(type='http', method='POST', path=COMMANDS, query_string=b'', headers=[
            (b'authorization', b'Bearer synthetic-test-token'), (b'content-type', b'application/json'),
            (b'content-length', b'10')])
        await asyncio.wait_for(app(scope, receive, send), 0.5)
        assert events[0]['status'] == 408
        assert not task.done() and not adapter.finished.is_set()
        assert adapter.calls == ['submit']
    finally:
        adapter.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_executor_scheduling_failure_does_not_leak_admission(monkeypatch):
    adapter = BlockingAdapter()
    adapter.block = False
    app = create_app(adapter, bearer_token='synthetic-test-token')
    def scheduling_failed(*args, **kwargs): raise RuntimeError('synthetic scheduling failure')
    with monkeypatch.context() as patch:
        patch.setattr(asyncio.get_running_loop(), 'run_in_executor', scheduling_failed)
        with pytest.raises(RuntimeError, match='synthetic scheduling failure'):
            await invoke(app, 'get')
    assert adapter.calls == []
    assert (await invoke(app, 'get'))[0] == 404


async def test_admission_gate_is_shared_across_explicit_event_loops():
    adapter = BlockingAdapter()
    app = create_app(adapter, bearer_token='synthetic-test-token')
    task = asyncio.create_task(invoke(app, 'submit'))
    try:
        await until(adapter.started.is_set)
        result = await asyncio.wait_for(asyncio.to_thread(lambda: asyncio.run(invoke(app, 'get'))), 0.5)
        assert result == (503, {'error': {'code': 'command_busy'}})
        assert adapter.calls == ['submit']
    finally:
        adapter.release.set()
        await asyncio.gather(task, return_exceptions=True)
    adapter.block = False
    assert await asyncio.to_thread(lambda: asyncio.run(invoke(app, 'get'))) == (404, {'error': {'code': 'command_not_found'}})
