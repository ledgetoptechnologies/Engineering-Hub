"""Explicit native-event lifecycle for the private queue-to-pilot bridge.

This is not an application factory or an activated worker. The caller owns the
live PilotOwner and already-started native feed, and must stop this runtime
before closing either. Invalidations drain eligible new work only: held jobs and
uncertain commands are never retried or recovered merely because a file changes.
"""
import asyncio
import os
from pathlib import Path
from dataclasses import dataclass
from uuid import UUID

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
        self._generation_drain = None
        # This is only a veto capability. It cannot grant a claim, manifest,
        # host permission or production approval that the bridge lacks.
        bridge._runtime = self

    def bind_generation_drain(self, owner, generation_id):
        """Local possession capability only; no HTTP or remote authentication."""
        self._identity()
        if (self._generation_drain is not None or self._state != 'not_started'
                or getattr(self.bridge, 'owner', None) is not owner
                or getattr(self.bridge.repo, 'pilot_owner', None) is not owner):
            raise HubError('generation_drain_binding_required', 409)
        owner.assert_active(self.bridge.repo.path)
        capability = OwnedGenerationDrain(self, owner, generation_id)
        self._generation_drain = capability
        return capability

    @property
    def status(self):
        state = self._state
        if state == 'active':
            gate = self.bridge.reconciliation_state
            if gate != 'accepted':
                state = ('startup_reconciliation_required' if gate == 'pending'
                         else 'manual_session_closed')
        return dict(state=state,
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
            if self.bridge.async_admission_required:
                self.bridge._metadata_admission()
            else:
                self.bridge._reconciliation_admission()
            return (self._state == 'active' and self._loop is not None
                and self._task is not None and not self._task.done()
                and self._task.cancelling() == 0
                and not self._closed and not self._source_failed and self.feed.available
                and self.feed._loop is self._loop)
        except Exception:
            return False

    def claim_preflight_stopped(self):
        """Recognize our exact not-yet-claimed task during an ordinary stop.

        This is not a permission issuer or a general failure normalizer. Owner,
        process and loop failures still propagate, and unavailable/failed native
        sources are never turned into a successful generation drain.
        """
        self._identity()
        self.bridge._guard()
        return (self._closed and self._state == 'stopping' and not self._source_failed
                and self.feed.available
                and self._active is asyncio.current_task())

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

    def close_admission(self):
        """Synchronously veto new phases/claims before listener drain begins.

        Does not cancel or wait for accepted work, close the source, or release
        ownership. The lifecycle owner must still await stop() afterward.
        """
        self._identity()
        self.bridge.close_admission()
        # Never cancel a task which may be awaiting an accepted remote phase.
        # The bridge veto closes any following phase immediately; the accepted
        # phase drains before the idle observer and subscription are closed.
        self._closed = True
        self._wakeup.set()
        if self._state not in {'failed', 'native_source_unavailable', 'stopped'}:
            self._state = 'stopping'
        return self.status

    async def stop(self):
        self.close_admission()
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
        self.bridge.close_admission()
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
            self.bridge.close_admission()
            if observer is not None:
                observer.cancel()  # Only the idle subscription, never effects.
                await asyncio.gather(observer, return_exceptions=True)
            await source.aclose()
            self._closed = True
            if self._source_failed:
                self._state = 'native_source_unavailable'
            elif self._state != 'failed':
                self._state = 'stopped'

def _canonical_uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


@dataclass(frozen=True)
class PhaseDrainAcknowledgement:
    schema: str
    generation_id: str
    request_id: str
    owner_epoch: str
    scope: str = 'local_queue_accepted_phase_and_observer_only'
    whole_workflow_completed: bool = False
    native_executor_stopped: bool = False
    remote_authenticated: bool = False


class OwnedGenerationDrain:
    """In-process, PID/loop/owner-bound veto and drain capability.

    Possession is authority ONLY within this trusted process. Neither a JSON
    representation nor these public IDs authorize a remote request or stop.
    """
    def __init__(self, runtime, owner, generation_id):
        if not _canonical_uuid(generation_id) or not _canonical_uuid(owner.epoch):
            raise HubError('generation_drain_binding_required', 409)
        self._runtime, self._owner = runtime, owner
        self._generation, self._epoch = generation_id, owner.epoch
        self._pid = os.getpid()
        self._request = self._task = self._ack = None
        self._revoked = False

    def _guard(self, generation_id):
        runtime = self._runtime
        if (self._revoked or os.getpid() != self._pid
                or generation_id != self._generation
                or runtime._generation_drain is not self
                or runtime.bridge.owner is not self._owner
                or runtime.bridge.repo.pilot_owner is not self._owner
                or self._owner.epoch != self._epoch):
            raise HubError('generation_drain_identity_lost', 409)
        runtime._identity()
        self._owner.assert_active(runtime.bridge.repo.path)
        if runtime._loop is not asyncio.get_running_loop():
            raise HubError('generation_drain_not_started', 409)

    async def drain(self, *, generation_id, request_id):
        self._guard(generation_id)
        if not _canonical_uuid(request_id):
            raise HubError('generation_drain_request_required', 409)
        if self._request is not None and self._request != request_id:
            raise HubError('generation_drain_request_conflict', 409)
        if self._task is None:
            # No await between request binding and sticky veto.
            self._request = request_id
            self._runtime.close_admission()
            self._task = asyncio.create_task(self._complete())
        # Cancelling/repeatedly cancelling a caller cannot cancel the one owned
        # drain task. Cancellation is propagated only once that task terminates.
        await finish_owned(self._task)
        self._guard(generation_id)
        if self._ack is None:
            raise HubError('generation_drain_unverified', 409)
        return self._ack

    async def _complete(self):
        runtime = self._runtime
        await runtime.stop()
        self._guard(self._generation)
        if (runtime._state != 'stopped' or runtime._source_failed or not runtime.feed.available
                or runtime._task is None or not runtime._task.done()
                or runtime._task.cancelled() or runtime._task.exception() is not None
                or runtime._active is not None
                or runtime.status['running'] or runtime.status['active']
                or not runtime._closed):
            raise HubError('generation_drain_unverified', 409)
        # _run completes only AFTER idle observer gather and source.aclose.
        self._ack = PhaseDrainAcknowledgement(
            schema='engineering-hub.local-generation-phase-drain.v1',
            generation_id=self._generation, request_id=self._request,
            owner_epoch=self._epoch)

    async def revoke(self):
        """Service teardown invalidates all future acknowledgement retrieval."""
        try:
            if self._task is not None:
                await finish_owned(self._task)
        finally:
            self._revoked = True
