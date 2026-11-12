"""Bounded child-process lifetime checks; no Docker or remote effects."""
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


def run_held_child(tmp_path, script):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(root / 'src'), str(root), env.get('PYTHONPATH', '')))
    ready = tmp_path / 'ready'
    with (tmp_path / 'child.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                                   cwd=root, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.025)
            log.flush()
            log.seek(0)
            assert ready.exists(), log.read()
            assert process.poll() is None
            if sys.platform == 'linux' and (tmp_path / 'private' / 'hub.db').exists():
                from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
                with pytest.raises(PilotOwnershipError):
                    PilotOwner(tmp_path / 'private' / 'hub.db', allow_ephemeral_test_storage=True)
        finally:
            # Only this exact disposable child is terminated. A deliberate hold
            # cannot be released by cancellation, including asyncio.run shutdown.
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            assert process.poll() is not None


def test_hold_retains_resource_and_event_loop_after_gc_and_repeated_cancellation(tmp_path):
    run_held_child(tmp_path, r'''
import asyncio, gc, pathlib, sys, weakref
from engineering_hub.unproved_drain import retain_unproved_drain
root = pathlib.Path(sys.argv[1])
class Resource: pass
async def main():
    resource = Resource()
    reference = weakref.ref(resource)
    task = asyncio.create_task(retain_unproved_drain(resource))
    del resource
    await asyncio.sleep(0)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        gc.collect()
        assert not task.done() and reference() is not None
    (root / 'ready').write_text('held')
    await task
asyncio.run(main())
''')


@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux owner and unified listeners')
def test_unproved_shared_drain_retains_bus_feed_client_and_owner(tmp_path):
    run_held_child(tmp_path, r'''
import asyncio, gc, pathlib, sys
from pytest import MonkeyPatch
from tests.test_hub_unified_queued_pilot_service import rig, start
from tests.test_shared_staging_application import shared_app
root = pathlib.Path(sys.argv[1])
async def main():
    monkeypatch = MonkeyPatch()
    state = rig.__wrapped__(root, monkeypatch)
    state = await shared_app.__wrapped__(state, root, monkeypatch)
    stop, serving, _ = await start(state)
    shared = state.core.state.shared_staging
    accepted = asyncio.create_task(asyncio.Event().wait())
    shared._inflight.add(accepted)
    async def unproved():
        raise RuntimeError('synthetic drain proof failure')
    shared._close = unproved
    stop.set()
    for _ in range(200):
        from engineering_hub.unproved_drain import _held
        if _held:
            break
        await asyncio.sleep(.01)
    assert _held
    for _ in range(3):
        serving.cancel()
        await asyncio.sleep(0)
        gc.collect()
        assert not serving.done() and not accepted.done()
        assert not shared.policies._open
        assert state.core.state.event_feed.available
        assert 'bus-stop' not in state.events
        assert state.owner.close_count == state.clients[0].close_count == 0
        assert state.modular_clients[0].close_count == 0
        state.owner.assert_active(state.settings.database)
    (root / 'ready').write_text('held')
    await serving
asyncio.run(main())
''')


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux queued service imports')
@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ('controller', 'client'))
async def test_partial_draft_close_failure_never_allows_owner_release(kind):
    from engineering_hub.queued_pilot_service import _cleanup_shared_staging
    async def failed():
        raise RuntimeError('synthetic close failure')
    resource = SimpleNamespace(close=failed, aclose=failed)
    draft = SimpleNamespace(module=SimpleNamespace(
        controller=resource if kind == 'controller' else None,
        client=resource if kind == 'client' else None))
    assert await _cleanup_shared_staging(None, draft) == (False, True)
