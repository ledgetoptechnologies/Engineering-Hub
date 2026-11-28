"""Atomic process reservation tests; no staging, credentials, or listeners."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from engineering_hub import app, queued_pilot_service, unified_queued_pilot_service, unified_pilot_startup
from engineering_hub.unproved_drain import UnprovedDrainError
from hub_container_executor import installed_coordinator

ENTRYPOINTS = [
    (installed_coordinator, 'run_installed_coordinator'),
    (unified_pilot_startup, 'serve_configuration'),
    (queued_pilot_service, 'serve_owned_queued_pilot'),
    (unified_queued_pilot_service, 'serve_owned_unified_queued_pilot'),
]

@pytest.mark.asyncio
@pytest.mark.parametrize('module,name', ENTRYPOINTS)
async def test_each_entrypoint_atomically_excludes_all_other_starts(module, name, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()
    async def occupied(*args, _drain_handoff, **kwargs):
        entered.set()
        await finish.wait()
    monkeypatch.setattr(module, '_reserved_' + name, occupied)
    running = asyncio.create_task(getattr(module, name)())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for other, other_name in ENTRYPOINTS:
            with pytest.raises((UnprovedDrainError, queued_pilot_service.QueuedPilotServiceError)):
                await getattr(other, other_name)()
        with pytest.raises(UnprovedDrainError):
            app.create_app(shared_staging=object())
    finally:
        finish.set()
        await running
    # A completed outer lifecycle really releases, rather than poisoning starts.
    await getattr(module, name)()

@pytest.mark.asyncio
async def test_explicit_nested_handoff_does_not_reserve_twice(monkeypatch):
    called = []
    async def nested(*args, _drain_handoff, **kwargs):
        called.append(_drain_handoff)
    async def outer(*args, _drain_handoff, **kwargs):
        await queued_pilot_service.serve_owned_queued_pilot(_drain_handoff=_drain_handoff)
    monkeypatch.setattr(installed_coordinator, '_reserved_run_installed_coordinator', outer)
    monkeypatch.setattr(queued_pilot_service, '_reserved_serve_owned_queued_pilot', nested)
    await installed_coordinator.run_installed_coordinator()
    assert len(called) == 1
    with pytest.raises(queued_pilot_service.QueuedPilotServiceError):
        await queued_pilot_service.serve_owned_queued_pilot(_drain_handoff=called[0])

@pytest.mark.asyncio
async def test_generic_app_reserves_at_construction_and_releases_after_lifespan(monkeypatch):
    @asynccontextmanager
    async def lifespan(_):
        yield
    monkeypatch.setattr(app, '_reserved_create_app', lambda *args, **kwargs: SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan)))
    application = app.create_app(shared_staging=object())
    with pytest.raises(UnprovedDrainError):
        app.create_app(shared_staging=object())
    with pytest.raises(UnprovedDrainError):
        await installed_coordinator.run_installed_coordinator()
    async with application.router.lifespan_context(application):
        with pytest.raises(queued_pilot_service.QueuedPilotServiceError):
            await queued_pilot_service.serve_owned_queued_pilot()
    with pytest.raises(UnprovedDrainError):
        async with application.router.lifespan_context(application):
            pass
    application = app.create_app(shared_staging=object())
    async with application.router.lifespan_context(application):
        pass

def test_two_threads_cannot_reserve_at_same_time():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from engineering_hub.unproved_drain import acquire_reservation, release_reservation
    barrier = threading.Barrier(2)
    attempted = threading.Barrier(2)
    def race():
        barrier.wait(timeout=2)
        try:
            lease = acquire_reservation()
        except UnprovedDrainError:
            lease = None
        try:
            attempted.wait(timeout=2)
            return lease is not None
        finally:
            if lease is not None:
                release_reservation(lease)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: race(), range(2)))
    assert sorted(results) == [False, True]
