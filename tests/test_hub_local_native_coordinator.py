import asyncio
import types
import pytest
from scripts import hub_local_native_coordinator as m

@pytest.mark.asyncio
async def test_failed_preparer_never_retries():
    calls=[]
    def fail(bound): calls.append(bound); raise ValueError('private')
    client=m._LocalClient(fail)
    client.binding=types.SimpleNamespace(bind_first_deploy=lambda command:'exact-bound')
    command=types.SimpleNamespace(action='deploy_container')
    with pytest.raises(ValueError): await client.submit_command(command)
    with pytest.raises(RuntimeError): await client.submit_command(command)
    assert calls==['exact-bound']

@pytest.mark.asyncio
async def test_non_adapter_preparer_denied_and_never_retried():
    client=m._LocalClient(lambda bound:object())
    client.binding=types.SimpleNamespace(bind_first_deploy=lambda command:'exact-bound')
    command=types.SimpleNamespace(action='deploy_container')
    with pytest.raises(RuntimeError,match='HostAdapter'): await client.submit_command(command)
    with pytest.raises(RuntimeError): await client.submit_command(command)

@pytest.mark.asyncio
async def test_non_deploy_cannot_prepare():
    client=m._LocalClient(lambda bound:pytest.fail('unexpected'))
    with pytest.raises(RuntimeError): await client.submit_command(types.SimpleNamespace(action='smoke_container'))

@pytest.mark.asyncio
async def test_cancel_waits_for_feed_stop_before_owner_close():
    entered=asyncio.Event(); release=asyncio.Event(); events=[]
    class Feed:
        async def stop(self):
            entered.set(); await release.wait(); events.append('feed-stopped')
    class Close:
        def __init__(self,name): self.name=name
        def close(self): events.append(self.name)
    coordinator=object.__new__(m.LocalNativeCoordinator)
    coordinator._pid=m.os.getpid(); coordinator._closed=False; coordinator._close_task=None
    coordinator._feed_start_task=None; coordinator._runtime_start_task=None
    coordinator._started=False; coordinator._feed_started=True; coordinator.feed=Feed()
    coordinator.client=types.SimpleNamespace(adapter=Close('adapter'))
    coordinator.reconciliation=Close('gate'); coordinator.owner=Close('owner')
    task=asyncio.create_task(coordinator.close()); await asyncio.wait_for(entered.wait(),2); task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and events==[]
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert events==['feed-stopped','adapter','gate','owner']


@pytest.mark.asyncio
async def test_concurrent_closes_share_drain_before_closed():
    entered=asyncio.Event(); release=asyncio.Event(); events=[]
    class Feed:
        async def stop(self):
            entered.set(); await release.wait(); events.append('feed-stopped')
    class Close:
        def __init__(self,name): self.name=name
        def close(self): events.append(self.name)
    coordinator=object.__new__(m.LocalNativeCoordinator)
    coordinator._pid=m.os.getpid(); coordinator._closed=False; coordinator._close_task=None
    coordinator._feed_start_task=None; coordinator._runtime_start_task=None
    coordinator._started=False; coordinator._feed_started=True; coordinator.feed=Feed()
    coordinator.client=types.SimpleNamespace(adapter=Close('adapter'))
    coordinator.reconciliation=Close('gate'); coordinator.owner=Close('owner')
    first=asyncio.create_task(coordinator.close()); second=None
    try:
        await asyncio.wait_for(entered.wait(),2)
        second=asyncio.create_task(coordinator.close())
        await asyncio.sleep(0)
        assert not first.done() and not second.done() and coordinator._closed is False and events == []
    finally:
        release.set()
        await asyncio.wait_for(first,2)
        if second is not None: await asyncio.wait_for(second,2)
    assert coordinator._closed is True and events==['feed-stopped','adapter','gate','owner']


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_start',[False,True])
async def test_close_waits_for_inflight_feed_start_and_prevents_runtime(cancel_start):
    entered=asyncio.Event(); release=asyncio.Event(); events=[]
    class Feed:
        async def start(self):
            entered.set(); await release.wait(); events.append('feed-started'); return {'available':True}
        async def stop(self): events.append('feed-stopped')
    class Runtime:
        async def start(self): events.append('runtime-started')
        async def stop(self): events.append('runtime-stopped')
    class Close:
        def __init__(self,name): self.name=name
        def close(self): events.append(self.name)
    coordinator=object.__new__(m.LocalNativeCoordinator)
    coordinator._pid=m.os.getpid(); coordinator._closed=False; coordinator._close_task=None
    coordinator._feed_start_task=None; coordinator._runtime_start_task=None
    coordinator._started=False; coordinator._feed_started=False
    coordinator.feed=Feed(); coordinator.runtime=Runtime()
    coordinator.client=types.SimpleNamespace(adapter=None)
    coordinator.reconciliation=Close('gate'); coordinator.owner=Close('owner')
    start=asyncio.create_task(coordinator._start_feed_and_runtime()); closer=None
    try:
        await asyncio.wait_for(entered.wait(),2)
        if cancel_start: start.cancel()
        closer=asyncio.create_task(coordinator.close())
        await asyncio.sleep(0)
        assert not start.done() and not closer.done() and coordinator._closed is False
    finally:
        release.set()
        outcomes=await asyncio.wait_for(asyncio.gather(start,closer,return_exceptions=True),2)
    assert isinstance(outcomes[0], (RuntimeError,asyncio.CancelledError)) and outcomes[1] is None
    assert coordinator._closed is True and events==['feed-started','feed-stopped','gate','owner']

@pytest.mark.asyncio
async def test_cancelled_preparer_closes_result_after_thread_finishes(monkeypatch):
    import threading
    entered=threading.Event(); release=threading.Event(); events=[]
    class Adapter:
        def close(self): events.append('adapter-closed')
    monkeypatch.setattr(m,'HostAdapter',Adapter)
    def prepare(bound): entered.set(); release.wait(5); return Adapter()
    client=m._LocalClient(prepare)
    client.binding=types.SimpleNamespace(bind_first_deploy=lambda command:'exact-bound')
    task=asyncio.create_task(client.submit_command(types.SimpleNamespace(action='deploy_container')))
    assert await asyncio.to_thread(entered.wait,2)
    task.cancel(); await asyncio.sleep(0); assert not task.done() and events==[]
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert events==['adapter-closed'] and client.adapter is None
