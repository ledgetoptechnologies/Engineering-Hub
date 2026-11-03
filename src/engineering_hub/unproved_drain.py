"""In-process retention when an owned staging drain cannot be proved.

This is not a restart lock or a recovery authority. Process death releases OS
locks; the existing durable journal and reconciliation gate remain necessary.
"""
import asyncio
import logging
import os
import threading


class UnprovedDrainError(RuntimeError):
    def __init__(self):
        super().__init__('An owned staging drain remains unproved')


_lock = threading.Lock()
_held = set()
_reservation = None


def require_no_unproved_drains():
    """Reject new selectors while any original owned session is retained."""
    with _lock:
        if _held or _reservation is not None:
            raise UnprovedDrainError()


def acquire_reservation():
    """Atomically reserve the process before any staging construction."""
    global _reservation
    with _lock:
        if _reservation is not None or _held:
            raise UnprovedDrainError()
        handoff = OwnedDrainHandoff()
        _reservation = handoff
        return handoff


def validate_reservation(handoff):
    with _lock:
        if type(handoff) is not OwnedDrainHandoff or _reservation is not handoff or handoff._pid != os.getpid() or handoff.state != 'ready':
            raise UnprovedDrainError()


def release_reservation(handoff):
    global _reservation
    with _lock:
        if _reservation is not handoff:
            raise UnprovedDrainError()
        if handoff.state == 'ready':
            handoff._state = 'released'
            _reservation = None
        # A held handoff has no release path.


class OwnedDrainHandoff:
    """Retain one exact resource graph and its live event loop until process exit.

    There is intentionally no public release, timeout or automatic retry. The
    original drain did not establish safety and a caller's assertion is not
    proof. Operator recovery after a process exit must inspect the journal.
    """
    def __init__(self):
        self._pid = os.getpid()
        self._state = 'ready'
        self._resources = None
        self._loop = None

    @property
    def state(self):
        return self._state

    async def retain_unproved(self, *resources):
        # Publish a strong process-wide reference before the first await. The
        # active coroutine itself also retains the original service stack.
        if self._state != 'ready' or self._pid != os.getpid():
            raise UnprovedDrainError()
        self._loop = asyncio.get_running_loop()
        self._resources = tuple(resources)
        self._state = 'held'
        with _lock:
            _held.add(self)
        logging.getLogger(__name__).error('owned_staging_drain_unproved_resources_retained')
        waiter = self._loop.create_future()
        while True:
            try:
                await asyncio.shield(waiter)
            except asyncio.CancelledError:
                # A caller or asyncio.run shutdown is not evidence that the
                # original operation stopped. Keep this loop and owner alive.
                continue
