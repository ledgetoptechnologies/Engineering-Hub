"""Owned successor lifecycle tests; no daemon is contacted."""
from contextlib import ExitStack
import threading
import os

import pytest

from scripts.hub_local_acceptance_components import LocalSuccessor
from scripts.hub_local_native_coordinator import LocalNativeCoordinator


def test_close_waits_for_preparation_and_prevents_reuse(monkeypatch):
    successor = object.__new__(LocalSuccessor)
    successor._lock, successor._closed, successor._stack = threading.RLock(), False, ExitStack()
    successor._pid = os.getpid()
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    successor._stack.callback(closed.set)
    def prepare(_bound):
        entered.set()
        assert release.wait(5)
        return 'done'
    monkeypatch.setattr(successor, '_prepare', prepare)
    results = []
    worker = threading.Thread(target=lambda: results.append(successor.prepare(object())))
    closer = threading.Thread(target=successor.close)
    worker.start()
    try:
        assert entered.wait(5)
        closer.start()
        assert not closed.wait(.1)
    finally:
        release.set()
        worker.join(timeout=5)
        if closer.ident is not None: closer.join(timeout=5)
    assert not worker.is_alive() and not closer.is_alive()
    assert results == ['done'] and closed.is_set()
    with pytest.raises(RuntimeError,match='closed'): successor.prepare(object())


@pytest.mark.parametrize('closed,same_adapter', [(False,True),(True,False)])
def test_cleanup_denies_unclosed_or_unrelated_coordinator(closed, same_adapter):
    from types import SimpleNamespace
    successor = object.__new__(LocalSuccessor)
    successor._lock, successor._closed = threading.RLock(), False
    successor._pid = os.getpid()
    successor._adapter = object(); successor.broker = object(); successor.thread = object()
    coordinator = object.__new__(LocalNativeCoordinator)
    coordinator._closed = closed
    coordinator._pid = os.getpid()
    coordinator.client = SimpleNamespace(adapter=successor._adapter if same_adapter else object())
    with pytest.raises(RuntimeError,match='not drained'):
        successor.seal_and_cleanup(coordinator)


def test_cleanup_denies_inherited_process_before_lock(monkeypatch):
    successor = object.__new__(LocalSuccessor)
    successor._pid = os.getpid() + 1
    with pytest.raises(RuntimeError,match='process changed'):
        successor.seal_and_cleanup(object())
