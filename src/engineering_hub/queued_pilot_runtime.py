"""Explicit native-event lifecycle for the private queue-to-pilot bridge.

This is not an application factory or an activated worker. The caller owns the
live PilotOwner and already-started native feed, and must stop this runtime
before closing either. Invalidations drain eligible new work only: held jobs and
uncertain commands are never retried or recovered merely because a file changes.
"""
import asyncio
import os
from pathlib import Path

from .events import NativeHubEventFeed
from .owned_tasks import finish_owned
from .queued_pilot import QueuedContainerPilot
from .repository import HubError


class QueuedPilotRuntime:
    """One event-loop-owned, serial, explicitly started private controller."""
    def __init__(self, bridge, feed):
        if (type(bridge) is not QueuedContainerPilot or type(feed) is not NativeHubEventFeed
                or Path(feed.path).absolute() != Path(bridge.repo.path).absolute()
                or getattr(bridge, '_runtime', None) is not None):
            raise HubError('queued_pilot_runtime_binding_required', 409)
        self.bridge, self.feed = bridge, feed
        self._pid = os.getpid()
        self._loop = None
        self._task = None
        self._active = None
        self._closed = False
        self._source_failed = False
        self._wakeup = asyncio.Event()
        self._state = 'not_started'
        self._last_job = None
        # This is only a veto capability. It cannot grant a claim, manifest,
        # host permission or production approval that the bridge lacks.
        bridge._runtime = self

    @property
    def status(self):
        return dict(state=self._state,
            running=self._task is not None and not self._task.done(),
            active=self._active is not None and not self._active.done(),
            last_job=None if self._last_job is None else dict(self._last_job))

    def _identity(self):
        if os.getpid() != self._pid or getattr(self.bridge, '_runtime', None) is not self:
            raise HubError('queued_pilot_runtime_identity_lost', 409)
        if self._loop is not None and self._loop is not asyncio.get_running_loop():
            raise HubError('queued_pilot_runtime_wrong_loop', 409)
        if Path(self.feed.path).absolute() != Path(self.bridge.repo.path).absolute():
            raise HubError('queued_pilot_runtime_binding_required', 409)

    def admission_allowed(self):
        """A synchronous, sticky admission veto; never an authority issuer."""
        try:
            self._identity()
            return (self._state == 'active' and self._loop is not None
                and self._task is not None and not self._task.done()
                and self._task.cancelling() == 0
                and not self._closed and not self._source_failed and self.feed.available
                and self.feed._loop is self._loop)
        except Exception:
            return False

    async def start(self):
        self._identity()
        if self._state != 'not_started' or self._task is not None or self._closed:
            raise HubError('queued_pilot_runtime_already_started', 409)
        loop = asyncio.get_running_loop()
        self.bridge._guard()
        if not self.feed.available:
            self._unavailable()
            self._state = 'native_source_unavailable'
            return self.status
        if self.feed._loop is not loop:
            raise HubError('queued_pilot_runtime_wrong_loop', 409)
        self._loop = loop
        self._state = 'active'
        self._task = asyncio.create_task(self._run())
        return self.status

    async def stop(self):
        self._identity()
        # Never cancel a task which may be awaiting an accepted remote phase.
        # The bridge veto closes any following phase immediately; the accepted
        # phase drains before the idle observer and subscription are closed.
        self._closed = True
        self._wakeup.set()
        if self._state not in {'failed', 'native_source_unavailable', 'stopped'}:
            self._state = 'stopping'
        try:
            if self._task is not None:
                await finish_owned(self._task)
        finally:
            # finish_owned propagates cancellation only after accepted work and
            # observer cleanup are actually terminal, including repeated cancel.
            if self._task is None or self._task.done():
                if self._source_failed:
                    self._state = 'native_source_unavailable'
                elif self._state != 'failed':
                    self._state = 'stopped'
        return self.status

    def _unavailable(self):
        self._source_failed = True
        self._closed = True
        self._state = 'native_source_unavailable'
        self._wakeup.set()

    async def _observe(self, source):
        try:
            async for pointer in source:
                if pointer['kind'] == 'source_unavailable' or not self.feed.available:
                    self._unavailable()
                    return
                self._wakeup.set()
            self._unavailable()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._unavailable()

    def _save_result(self, result):
        if result is None:
            return False
        if (type(result) is not dict or type(result.get('id')) is not str
                or result.get('state') not in {'succeeded', 'failed', 'held'}):
            raise HubError('queued_pilot_runtime_result_unverified', 409)
        # Detailed target, proof, request and exception content belongs to the
        # existing authenticated records, never this lifecycle status.
        self._last_job = dict(id=result['id'], state=result['state'])
        return True

    async def _one(self):
        self._identity()
        if not self.admission_allowed():
            return False
        self.bridge._guard()
        self._active = asyncio.create_task(self.bridge.run_once())
        try:
            result = await finish_owned(self._active)
        except HubError as exc:
            # Admission may close after this supervisor creates the task but
            # before its first claim. That expected stop race is not a failure.
            if self._closed and exc.code == 'queued_pilot_runtime_unavailable':
                return False
            raise
        except asyncio.CancelledError:
            if self._active.done() and not self._active.cancelled():
                try:
                    self._save_result(self._active.result())
                except Exception:
                    pass
            raise
        finally:
            # No cancellation is sent to this task. finish_owned does not return
            # until its actual result/exception is available.
            if self._active.done():
                self._active = None
        return self._save_result(result)

    async def _run(self):
        source = self.feed.broker.events()
        observer = None
        try:
            # Calling events() alone does not subscribe. The first anext does;
            # only then may we scan the durable queue for startup/missed work.
            initial = await anext(source)
            if initial['kind'] == 'source_unavailable' or not self.feed.available:
                self._unavailable()
                return
            observer = asyncio.create_task(self._observe(source))
            self._wakeup.set()
            while not self._closed:
                await self._wakeup.wait()
                self._wakeup.clear()
                while not self._closed:
                    if not self.feed.available:
                        self._unavailable()
                        break
                    if not await self._one():
                        break
                    await asyncio.sleep(0)  # Fairness, not a timer or poll.
        except asyncio.CancelledError:
            self._closed = True
            # _one has already drained the accepted actual job before raising.
            raise
        except Exception:
            self._closed = True
            self._state = 'failed'
        finally:
            if observer is not None:
                observer.cancel()  # Only the idle subscription, never effects.
                await asyncio.gather(observer, return_exceptions=True)
            await source.aclose()
            self._closed = True
            if self._source_failed:
                self._state = 'native_source_unavailable'
            elif self._state != 'failed':
                self._state = 'stopped'
