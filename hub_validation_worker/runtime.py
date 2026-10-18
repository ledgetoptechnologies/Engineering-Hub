"""Explicit native-invalidation consumer for one private software executor.

No import side effects, timer, polling, model call or service activation. The
owner starts the existing native feed first and stops this runtime before
stopping that feed. Accepted backend work is shielded to its safe boundary;
cancelling an await is never treated as cancellation of an executor thread.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from contextvars import copy_context
from pathlib import Path
import sqlite3
import threading

from engineering_hub.events import NativeHubEventFeed
from engineering_hub.owned_tasks import finish_owned
from engineering_hub.work_queue import WorkQueue, WorkWorker
from .core import ValidationError, ValidationLedger, ValidationWorker


EXECUTOR = 'executor:software-validation'


class ValidationRuntime:
    def __init__(self, queue, worker, identity, feed, *, owner_guard=None):
        if (type(queue) is not WorkQueue or type(worker) is not ValidationWorker
                or worker.queue is not queue or type(worker.ledger) is not ValidationLedger
                or type(identity) is not WorkWorker or identity.agent != EXECUTOR
                or type(feed) is not NativeHubEventFeed
                or Path(feed.path).absolute() != Path(queue.repo.path).absolute()
                or worker.ledger.path == Path(queue.repo.path).absolute()):
            raise ValidationError('validation_runtime_identity_mismatch')
        if owner_guard is not None and not callable(owner_guard):
            raise ValidationError('validation_runtime_identity_mismatch')
        self.queue, self.worker, self.identity, self.feed = queue, worker, identity, feed
        self._owner_guard = owner_guard
        self.action_keys = tuple(sorted(worker.enrollments))
        if not self.action_keys:
            raise ValidationError('validation_runtime_enrollment_required')
        self._task = None
        self._active = None
        self._closed = threading.Event()
        self._wakeup = asyncio.Event()
        self._source_failed = False
        self._loop = None
        self.state = 'not_started'
        self.last_job = None

    @property
    def status(self):
        return {'state': self.state,
                'running': self._task is not None and not self._task.done(),
                'active': self._active is not None and not self._active.done(),
                'last_job': None if self.last_job is None else dict(self.last_job)}

    def _scope(self):
        # Explicitly reject owner-enrolled deployment ledgers, even if a caller
        # happens to possess a live PilotOwner capability. This runtime is not
        # an additional controller for the existing container-pilot lifecycle.
        if self._owner_guard is not None:
            self._owner_guard()
        self.queue.repo._assert_pilot_owner(None)
        with closing(sqlite3.connect(Path(self.queue.repo.path).absolute().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_pilot_owner'").fetchone():
                raise ValidationError('validation_runtime_pilot_ledger_forbidden')

    async def start(self):
        if self.state != 'not_started' or self._task is not None:
            raise ValidationError('validation_runtime_already_started')
        self._loop = asyncio.get_running_loop()
        self._scope()
        if not self.feed.available:
            self.state = 'native_source_unavailable'
            self._closed.set()
            return self.status
        self.state = 'active'
        self._task = asyncio.create_task(self._run())
        return self.status

    async def stop(self):
        if self._loop is not None and self._loop is not asyncio.get_running_loop():
            raise ValidationError('validation_runtime_wrong_loop')
        # Close admission immediately; no cancellation is sent to a backend.
        self._closed.set()
        self._wakeup.set()
        if self.state not in {'failed', 'native_source_unavailable', 'stopped'}:
            self.state = 'stopping'
        if self._task is not None:
            await finish_owned(self._task)
        if self.state not in {'failed', 'native_source_unavailable'}:
            self.state = 'stopped'
        return self.status

    def _unavailable(self):
        self._source_failed = True
        self._closed.set()
        self._wakeup.set()

    async def _observe(self, source):
        try:
            async for pointer in source:
                if pointer['kind'] == 'source_unavailable' or not self.feed.available:
                    self._unavailable()
                    return
                self._wakeup.set()  # Coalesce pointers, not durable queue rows.
            self._unavailable()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._unavailable()

    def _claim_and_run(self):
        if self._closed.is_set() or not self.feed.available:
            return None
        self._scope()
        if self._closed.is_set() or not self.feed.available:
            return None
        claim = self.queue.claim(worker=self.identity, lease_seconds=600, action_keys=self.action_keys)
        if claim is None:
            return None
        try:
            self._scope()
            if self._closed.is_set() or not self.feed.available:
                raise ValidationError('validation_runtime_admission_closed')
            return self.worker.run_once(job_id=claim['id'], worker=self.identity, fence=claim['fence'])
        except Exception:
            # A claim whose validation cannot begin is still a durable
            # reservation. Never let a runtime exception turn it into a retry.
            try:
                self.queue.complete(job_id=claim['id'], worker=self.identity,
                                    fence=claim['fence'], outcome='held')
            except Exception:
                pass
            return {'job_id': claim['id'], 'state': 'held'}

    def _save_result(self, result):
        if result is None:
            return False
        if type(result) is not dict or result.get('state') not in {'succeeded', 'failed', 'held'}:
            raise ValidationError('validation_runtime_result_unverified')
        # No evidence IDs, target contents, commands or exception strings in
        # lifecycle status; the authenticated queue API owns detailed records.
        self.last_job = {'job_id': result['job_id'], 'state': result['state']}
        return True

    async def _one(self):
        # Equivalent thread/context behavior to to_thread, but preserve the raw
        # executor Future rather than exposing a separate cancellable Task.
        # Global shutdown can cancel BOTH this task and its parent; shielding
        # only the parent would otherwise abandon an accepted executor thread.
        future = asyncio.get_running_loop().run_in_executor(None, copy_context().run, self._claim_and_run)
        try:
            result = await finish_owned(future)
        except asyncio.CancelledError:
            # finish_owned does not propagate cancellation until this Future is
            # terminal. Retain the sanitized actual result before propagating.
            if future.done() and not future.cancelled():
                try:
                    self._save_result(future.result())
                except Exception:
                    pass
            raise
        return self._save_result(result)

    async def _run(self):
        source = self.feed.broker.events()
        observer = None
        try:
            # First anext registers the subscriber BEFORE the initial drain.
            # A write across this snapshot/drain boundary remains queued.
            initial = await anext(source)
            if initial['kind'] == 'source_unavailable' or not self.feed.available:
                self._unavailable()
                return
            observer = asyncio.create_task(self._observe(source))
            self._wakeup.set()
            while not self._closed.is_set():
                await self._wakeup.wait()
                self._wakeup.clear()
                while not self._closed.is_set():
                    if not self.feed.available:
                        self._unavailable()
                        break
                    self._active = asyncio.create_task(self._one())
                    worked = await finish_owned(self._active)
                    self._active = None
                    if not worked:
                        break
                    await asyncio.sleep(0)  # Cooperative fairness, not polling.
        except asyncio.CancelledError:
            self._closed.set()
            # finish_owned above has already completed any accepted thread.
            raise
        except Exception:
            self._closed.set()
            self.state = 'failed'
        finally:
            if observer is not None:
                observer.cancel()  # Cancels only an idle event subscription.
                await asyncio.gather(observer, return_exceptions=True)
            await source.aclose()
            self._active = None
            if self._source_failed:
                self.state = 'native_source_unavailable'
            elif self.state != 'failed':
                self.state = 'stopped'
