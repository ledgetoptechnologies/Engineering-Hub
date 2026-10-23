"""Mock providers; real drafted runtime task, accepted phase and observer cleanup."""
import asyncio
import inspect
from types import SimpleNamespace

import pytest

from engineering_hub import queued_pilot_runtime as draft
from engineering_hub import unified_queued_pilot_service as service

GEN = '38d7cd63-08c5-4213-95f9-f4999ce85c75'
REQ = 'cf90a001-ab26-4406-b858-9611d7c26769'
OTHER = '81423e32-4e49-4fe9-8196-ebbb47a49432'


class Owner:
    epoch = '069d2d4f-3e02-41ca-97af-b7cdfe040975'
    active = True
    def assert_active(self, path):
        if not self.active: raise RuntimeError('owner lost')


class Source:
    def __init__(self):
        self.initial = True
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.closed = False
    def __aiter__(self): return self
    async def __anext__(self):
        if self.initial:
            self.initial = False
            return {'kind': 'changed'}
        await asyncio.Future()
    async def aclose(self):
        self.close_entered.set()
        await self.close_release.wait()
        self.closed = True


class Feed:
    def __init__(self, source):
        self.path = '/test/hub.sqlite3'
        self._loop = asyncio.get_running_loop()
        self.available = True
        self.broker = SimpleNamespace(events=lambda: source)


class Bridge:
    # Explicit synthetic admission state for portable lifecycle-only tests.
    # Real mandatory owner/gate enforcement is tested in queued_manual_session.
    def __init__(self, owner):
        self.reconciliation_state = 'accepted'
        self.owner = owner
        self.repo = SimpleNamespace(path='/test/hub.sqlite3', pilot_owner=owner)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False
        self.calls = 0
    def _guard(self): self.owner.assert_active(self.repo.path)
    def _reconciliation_admission(self):
        if self.reconciliation_state != 'accepted':
            raise RuntimeError('synthetic manual session closed')
    def close_admission(self): self.reconciliation_state = 'closed'
    async def run_once(self):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        self.finished = True
        return {'id': REQ, 'state': 'held'}


async def rig(monkeypatch):
    monkeypatch.setattr(draft, 'NativeHubEventFeed', Feed)
    monkeypatch.setattr(draft, 'QueuedContainerPilot', Bridge)
    owner, source = Owner(), Source()
    bridge = Bridge(owner)
    runtime = draft.QueuedPilotRuntime(bridge, Feed(source))
    capability = runtime.bind_generation_drain(owner, GEN)
    await runtime.start()
    await bridge.entered.wait()
    return owner, source, bridge, runtime, capability


@pytest.mark.asyncio
async def test_ack_waits_for_accepted_phase_and_observer_cleanup(monkeypatch):
    owner, source, bridge, runtime, cap = await rig(monkeypatch)
    task = asyncio.create_task(cap.drain(generation_id=GEN, request_id=REQ))
    await asyncio.sleep(0)
    assert not runtime.admission_allowed() and not task.done()
    bridge.release.set()
    await source.close_entered.wait()
    assert bridge.finished and not source.closed and not task.done()
    source.close_release.set()
    ack = await task
    assert source.closed and bridge.calls == 1
    assert ack.generation_id == GEN and ack.request_id == REQ and ack.owner_epoch == owner.epoch
    assert ack.whole_workflow_completed is False and ack.native_executor_stopped is False
    assert ack.remote_authenticated is False
    assert await cap.drain(generation_id=GEN, request_id=REQ) is ack
    with pytest.raises(draft.HubError): await cap.drain(generation_id=GEN, request_id=OTHER)
    with pytest.raises(draft.HubError): await cap.drain(generation_id=OTHER, request_id=REQ)
    await cap.revoke()
    with pytest.raises(draft.HubError): await cap.drain(generation_id=GEN, request_id=REQ)


@pytest.mark.asyncio
async def test_repeated_caller_cancel_does_not_cancel_owned_drain(monkeypatch):
    _, source, bridge, runtime, cap = await rig(monkeypatch)
    task = asyncio.create_task(cap.drain(generation_id=GEN, request_id=REQ))
    await asyncio.sleep(0)
    task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
    assert not task.done() and not cap._task.cancelled() and not bridge.finished
    bridge.release.set(); await source.close_entered.wait()
    assert not task.done()
    source.close_release.set()
    with pytest.raises(asyncio.CancelledError): await task
    ack = await cap.drain(generation_id=GEN, request_id=REQ)
    assert ack.request_id == REQ and not runtime.status['running']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['epoch', 'owner', 'pid', 'binding'])
async def test_identity_loss_denies_ack_after_drain(monkeypatch, mutation):
    owner, source, bridge, runtime, cap = await rig(monkeypatch)
    task = asyncio.create_task(cap.drain(generation_id=GEN, request_id=REQ))
    await asyncio.sleep(0)
    if mutation == 'epoch': owner.epoch = OTHER
    elif mutation == 'owner': bridge.repo.pilot_owner = Owner()
    elif mutation == 'pid': cap._pid = -1
    else: runtime._generation_drain = None
    bridge.release.set(); source.close_release.set()
    with pytest.raises((draft.HubError, RuntimeError)): await task
    assert source.closed and cap._ack is None


@pytest.mark.asyncio
async def test_source_failure_is_not_successful_drain(monkeypatch):
    _, source, bridge, runtime, cap = await rig(monkeypatch)
    runtime._source_failed = True
    task = asyncio.create_task(cap.drain(generation_id=GEN, request_id=REQ))
    bridge.release.set(); source.close_release.set()
    with pytest.raises(draft.HubError): await task
    assert source.closed and cap._ack is None


@pytest.mark.asyncio
async def test_wrong_generation_no_side_effect_before_request(monkeypatch):
    _, source, bridge, runtime, cap = await rig(monkeypatch)
    with pytest.raises(draft.HubError): await cap.drain(generation_id=OTHER, request_id=REQ)
    assert runtime.admission_allowed() and cap._task is None and cap._request is None
    bridge.release.set(); source.close_release.set()
    await runtime.stop()


@pytest.mark.asyncio
async def test_one_pending_request_and_same_request_share_one_drain(monkeypatch):
    _, source, bridge, runtime, cap = await rig(monkeypatch)
    first = asyncio.create_task(cap.drain(generation_id=GEN, request_id=REQ))
    second = asyncio.create_task(cap.drain(generation_id=GEN, request_id=REQ))
    await asyncio.sleep(0)
    owned = cap._task
    with pytest.raises(draft.HubError): await cap.drain(generation_id=GEN, request_id=OTHER)
    assert cap._task is owned and not first.done() and not second.done()
    bridge.release.set(); source.close_release.set()
    a, b = await asyncio.gather(first, second)
    assert a is b and bridge.calls == 1


@pytest.mark.asyncio
async def test_observer_source_close_failure_never_acknowledges(monkeypatch):
    _, source, bridge, runtime, cap = await rig(monkeypatch)
    async def failed_close():
        source.close_entered.set()
        raise RuntimeError('synthetic source cleanup failure')
    source.aclose = failed_close
    task = asyncio.create_task(cap.drain(generation_id=GEN, request_id=REQ))
    bridge.release.set()
    with pytest.raises(RuntimeError): await task
    assert cap._ack is None and runtime._task.done()


@pytest.mark.asyncio
async def test_binding_rejects_wrong_owner_and_second_binding(monkeypatch):
    _, source, bridge, runtime, cap = await rig(monkeypatch)
    with pytest.raises(draft.HubError): runtime.bind_generation_drain(Owner(), GEN)
    with pytest.raises(draft.HubError): runtime.bind_generation_drain(bridge.owner, GEN)
    with pytest.raises(draft.HubError): await cap.drain(generation_id=GEN, request_id='not-a-uuid')
    assert runtime.admission_allowed() and cap._task is None
    bridge.release.set(); source.close_release.set(); await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_rejection', [False, True])
async def test_invalid_generation_callback_task_drains_despite_repeated_cancellation(cancel_rejection):
    entered, cleanup_entered, release, finished = (asyncio.Event() for _ in range(4))
    async def work():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            cleanup_entered.set()
            await release.wait()
            finished.set()
    returned = asyncio.create_task(work())
    await entered.wait()
    rejected = asyncio.create_task(service._reject_async_generation_result(returned))
    await cleanup_entered.wait()
    if cancel_rejection:
        rejected.cancel(); await asyncio.sleep(0); rejected.cancel(); await asyncio.sleep(0)
    assert not rejected.done() and not returned.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError if cancel_rejection else service.QueuedPilotServiceError):
        await rejected
    assert returned.cancelled() and finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('done', [False, True])
async def test_invalid_generation_callback_future_is_retrieved(done):
    future = asyncio.get_running_loop().create_future()
    if done:
        future.set_exception(RuntimeError('synthetic callback failure'))
    with pytest.raises(service.QueuedPilotServiceError):
        await service._reject_async_generation_result(future)
    assert future.done() and (done or future.cancelled())


@pytest.mark.asyncio
async def test_invalid_generation_callback_current_task_rejected_without_self_cancel():
    current = asyncio.current_task()
    cancelling = current.cancelling()
    with pytest.raises(service.QueuedPilotServiceError):
        await service._reject_async_generation_result(current)
    assert not current.cancelled() and current.cancelling() == cancelling
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_invalid_generation_callback_foreign_future_is_not_cancelled():
    loop = asyncio.new_event_loop()
    try:
        foreign = loop.create_future()
        with pytest.raises(service.QueuedPilotServiceError):
            await service._reject_async_generation_result(foreign)
        assert not foreign.done()
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_invalid_generation_callback_coroutine_closed_without_start():
    started = False
    async def work():
        nonlocal started
        started = True
    returned = work()
    with pytest.raises(service.QueuedPilotServiceError):
        await service._reject_async_generation_result(returned)
    assert not started and inspect.getcoroutinestate(returned) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_invalid_generation_callback_custom_awaitable_is_not_scheduled():
    class Custom:
        def __await__(self):
            raise AssertionError('custom awaitable must not run')
    with pytest.raises(service.QueuedPilotServiceError):
        await service._reject_async_generation_result(Custom())
    assert await service._reject_async_generation_result(None) is None
