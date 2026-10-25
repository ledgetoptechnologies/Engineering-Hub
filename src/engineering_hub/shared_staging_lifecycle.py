"""Explicit one-owner recovery barrier; no factory discovery or auto activation.

The native feed, pilot client and PilotOwner belong to the outer service. This coordinator
starts no claim loops while pending. Unknown or outstanding original work keeps
this first version closed; a returned held row is not a successful recovery proof.
"""
import asyncio
import os
from pathlib import Path

from .events import NativeHubEventFeed
from .execution_policy import SharedExecutionPolicies
from .lifecycle import finish_owned
from .pilot_ownership import PilotOwner
from .queued_pilot import QueuedContainerPilot
from .queued_pilot_runtime import QueuedPilotRuntime
from .repository import HubError, HubRepository
from .staging_client import StagingConnectionConfig
from .staging_controller import StagingController
from .staging_module import StagingModule
from .work_queue import CONTAINER_PILOT_EXECUTOR, WorkQueue


MAX_RECOVERY_JOBS = 256


def _require(value):
    if not value:
        raise HubError('shared_staging_lifecycle_changed', 409)


class SharedStagingLifecycle:
    def __init__(self, owner, repo, queue, policies, pilot_runtime, staging_module):
        _require(type(owner) is PilotOwner and type(repo) is HubRepository
                 and type(queue) is WorkQueue and type(policies) is SharedExecutionPolicies
                 and type(pilot_runtime) is QueuedPilotRuntime and type(staging_module) is StagingModule)
        bridge, controller, config = pilot_runtime.bridge, staging_module.controller, staging_module.config
        _require(type(bridge) is QueuedContainerPilot and type(controller) is StagingController
                 and type(config) is StagingConnectionConfig and config.enabled is True
                 and type(pilot_runtime.feed) is NativeHubEventFeed)
        self.owner, self.repo, self.queue, self.policies = owner, repo, queue, policies
        self.pilot_runtime, self.staging_module = pilot_runtime, staging_module
        self.bridge, self.controller, self.feed = bridge, controller, pilot_runtime.feed
        self._snapshot = (owner, repo, queue, policies, pilot_runtime, staging_module,
                          bridge, controller, pilot_runtime.feed, config,
                          controller.client, bridge.workflow, bridge.workflow.client,
                          bridge.reconciliation, bridge._execution_binding, controller._policy_binding)
        self._config_state = (config.enabled, config.auto_run, config.projects, config.origin,
                              config.credential_file, config.ca_file, config.preview_origin, config.admin_actors)
        self._pid, self._loop = os.getpid(), None
        self._state = 'not_started'
        self._unresolved = ()
        self._recovery_original = None
        self._lock = asyncio.Lock()
        self._recovery_task = self._close_task = None
        self._closing = False
        self._is_drained = False
        self._veto_failed = False
        self._shutdown_tasks = ()
        self._pilot_task = self._modular_task = None
        self._inflight = controller.inflight
        _require(not hasattr(queue, '_shared_staging_lifecycle')
                 and not hasattr(pilot_runtime, '_shared_staging_lifecycle'))
        self._guard(construction=True)
        _require(policies._installed and not policies._open and not policies._closed and not policies._ready
                 and pilot_runtime._state == 'not_started' and pilot_runtime._task is None
                 and controller.background is None and not controller.inflight and not controller.stopping)
        queue._shared_staging_lifecycle = self
        pilot_runtime._shared_staging_lifecycle = self

    @property
    def is_drained(self):
        # Captured lifecycle tasks and modular transport only. The outer ASGI
        # service must independently drain its accepted HTTP requests.
        return self._is_drained is True

    @property
    def status(self):
        reasons = {'pending': 'operator_acceptance_required',
                   'recovering': 'original_operations_pending',
                   'recovery_required': 'original_operations_unverified',
                   'shutdown_failed': 'owned_drain_failed'}
        return {'state': self._state, 'reason': reasons.get(self._state)}

    def _guard(self, *, feed=False, construction=False):
        try:
            _require(os.getpid() == self._pid and len(self._snapshot) == 16)
            (owner, repo, queue, policies, runtime, module, bridge, controller, native,
             config, client, workflow, pilot_client, gate, pilot_binding, modular_binding) = self._snapshot
            _require(all(current is original for current, original in zip(
                (self.owner, self.repo, self.queue, self.policies, self.pilot_runtime, self.staging_module,
                 self.bridge, self.controller, self.feed), self._snapshot[:9])))
            _require(repo.pilot_owner is owner and repo.execution_principal == CONTAINER_PILOT_EXECUTOR
                     and queue.repo is repo and policies.repo is repo and policies.queue is queue
                     and runtime.bridge is bridge and runtime.feed is native and bridge._runtime is runtime
                     and bridge.owner is owner and bridge.repo is repo and bridge.queue is queue
                     and bridge.workflow is workflow and workflow.repo is repo and workflow.pilot_owner is owner
                     and workflow.client is pilot_client and bridge.reconciliation is gate
                     and bridge._shared_mode is True and bridge._execution_policies is policies
                     and bridge._execution_binding is pilot_binding
                     and module.controller is controller and module.config is config and module.client is client
                     and controller.repo is repo and controller.queue is queue and controller.client is client
                     and controller.execution_policies is policies and controller._execution_policies is policies
                     and controller._policy_binding is modular_binding
                     and controller.inflight is self._inflight
                     and Path(native.path).absolute() == Path(repo.path).absolute())
            if not construction:
                _require(getattr(queue, '_shared_staging_lifecycle', None) is self
                         and getattr(runtime, '_shared_staging_lifecycle', None) is self)
            _require((config.enabled, config.auto_run, config.projects, config.origin,
                      config.credential_file, config.ca_file, config.preview_origin, config.admin_actors)
                     == self._config_state)
            owner.assert_active(repo.path)
            policies.assert_binding('pilot', pilot_binding)
            policies.assert_binding('modular', modular_binding)
            bridge._guard()
            controller._shared_guard()
            if self._loop is not None:
                _require(self._loop is asyncio.get_running_loop())
            if feed:
                _require(native.available and native._loop is asyncio.get_running_loop())
        except Exception:
            raise HubError('shared_staging_lifecycle_changed', 409) from None

    def validate_application(self, repo, queue, runtime, feed):
        self._guard()
        _require(repo is self.repo and queue is self.queue
                 and runtime is self.pilot_runtime and feed is self.feed)
        return None

    async def start_pending(self):
        self._guard(feed=True)
        _require(self._state == 'not_started' and not self._closing)
        self._loop = asyncio.get_running_loop()
        self._state = 'pending'
        return self.status

    async def recover_and_open(self):
        self._guard(feed=True)
        async with self._lock:
            _require(self._state in {'pending', 'recovering', 'recovery_required'} and not self._closing
                     and not self.policies._closed and not self.policies._open)
            if self.bridge.reconciliation_state != 'accepted':
                return self.status
            if self._recovery_task is None or self._recovery_task.done() and self._state == 'recovery_required':
                self._state = 'recovering'
                self._recovery_task = asyncio.create_task(self._recover_and_open())
            elif self._recovery_task.done():
                raise HubError('shared_staging_recovery_already_attempted', 409)
            task = self._recovery_task
        return await finish_owned(task)

    def _original_jobs(self):
        # No lease expiration or row mutation merely to enumerate recovery work.
        with self.repo._connection() as db:
            rows = db.execute("SELECT j.* FROM hub_work_jobs j JOIN hub_work_machines m "
                "ON m.job_id=j.id AND m.machine_id=j.machine_id "
                "WHERE j.state IN ('claimed','held') ORDER BY j.sequence LIMIT ?",
                (MAX_RECOVERY_JOBS + 1,)).fetchall()
            _require(len(rows) <= MAX_RECOVERY_JOBS)
            # Missing machine links or unknown reservations are not ignored.
            count = db.execute("SELECT COUNT(*) FROM hub_work_jobs WHERE state IN ('claimed','held')").fetchone()[0]
            reservations = db.execute('SELECT COUNT(*) FROM hub_work_machines WHERE job_id IS NOT NULL').fetchone()[0]
            _require(count == len(rows) == reservations)
        return [dict(row) for row in rows]

    async def _recover_and_open(self):
        try:
            self._guard(feed=True)
            _require(not self._closing and self.bridge.reconciliation_state == 'accepted')
            jobs = self._original_jobs()
            identity_keys = ('id', 'actor', 'request_id', 'action_key', 'environment', 'machine_id',
                             'project', 'worker', 'fence', 'target', 'fingerprint', 'requires_execution_proof')
            current = {job['id']: tuple(job[key] for key in identity_keys) for job in jobs}
            if self._recovery_original is None:
                self._recovery_original = current
            _require(all(self._recovery_original.get(identity) == value for identity, value in current.items()))
            for job in jobs:
                self._guard(feed=True)
                _require(not self._closing and self.bridge.reconciliation_state == 'accepted')
                action = job['action_key']
                if action == self.bridge.action_key:
                    worker = self.bridge.worker
                    recover = self.bridge.recover
                elif action in self.controller.actions.values():
                    worker = self.controller.worker
                    recover = self.controller.reconcile
                else:
                    raise HubError('shared_staging_recovery_required', 409)
                # Another live generation's lease may be observed, never adopted.
                if (job['state'] == 'claimed' and job['lease_until'] > self.queue._now()
                        and job['worker'] != worker.encoded()):
                    continue
                await recover(job['id'])
                self._guard(feed=True)
            remaining = self._original_jobs()
            self._unresolved = tuple(row['id'] for row in remaining)
            if remaining:
                self._state = 'recovery_required'
                return self.status
            self._guard(feed=True)
            _require(not self._closing and self.bridge.reconciliation_state == 'accepted')
            self.policies.ready('pilot')
            self.policies.ready('modular')
            self.policies.open()
            await self.pilot_runtime.start()
            self._pilot_task = self.pilot_runtime._task
            self._guard(feed=True)
            _require(self.pilot_runtime.status['running'] and not self._closing)
            if self._snapshot[9].auto_run:
                await self.controller.start()
                self._modular_task = self.controller.background
                self._guard(feed=True)
            self._state = 'active'
            return self.status
        except BaseException:
            self._snapshot[3].close()
            self._state = 'recovery_required'
            raise HubError('shared_staging_recovery_required', 409) from None

    def close_admission(self):
        """Listener-stop veto only; the outer server still owns HTTP drain.

        No client or shutdown task is started here: accepted HTTP requests may
        not yet be represented in controller.inflight. Retain veto failures for
        the later owned shutdown rather than abandoning the remaining vetoes.
        """
        self._closing = True
        if self._state not in {'stopped', 'shutdown_failed'}:
            self._state = 'stopping'
        policies, runtime, controller = self._snapshot[3], self._snapshot[4], self._snapshot[7]
        for veto in (policies.close, runtime.suspend_claims, controller.close_admission):
            try:
                veto()
            except BaseException:
                self._veto_failed = True

    async def close(self):
        # The outer ASGI service must first finish its accepted HTTP requests.
        # Retain the shutdown task BEFORE identity-sensitive vetoes so a failed
        # veto cannot abandon accepted operations or transfer cleanup to a caller.
        if self._close_task is None:
            runtime, controller = self._snapshot[4], self._snapshot[7]
            tasks = (self._recovery_task, self._pilot_task or runtime._task,
                     runtime._active, self._modular_task or controller.background, *tuple(self._inflight))
            self._shutdown_tasks = tuple(dict.fromkeys(task for task in tasks if task is not None))
            self._close_task = asyncio.create_task(self._close())
            self.close_admission()
        await finish_owned(self._close_task)
        return self.status

    async def _close(self):
        failed = self._veto_failed
        runtime, controller, client = self._snapshot[4], self._snapshot[7], self._snapshot[10]
        if self._recovery_task is not None:
            result = await asyncio.gather(self._recovery_task, return_exceptions=True)
            failed = any(isinstance(item, BaseException) for item in result) or failed
        # Do not rely on a mutable runtime identity check to find the actual
        # accepted handles. Drain those captured handles even if the check fails.
        active = tuple(task for task in self._shutdown_tasks
                       if task is not self._pilot_task and task is not runtime._task)
        if active:
            result = await asyncio.gather(*active, return_exceptions=True)
            failed = any(isinstance(item, BaseException) for item in result) or failed
        result = await asyncio.gather(runtime.drain_current(), controller.drain(), return_exceptions=True)
        failed = any(isinstance(item, BaseException) for item in result) or failed
        try:
            await runtime.stop()
        except BaseException:
            failed = True
        # The original supervisor must actually terminate. There is deliberately
        # no timeout/force-cancel which could let an outer owner close underneath
        # a still-running accepted task. is_drained remains False until proof.
        if self._shutdown_tasks:
            result = await asyncio.gather(*self._shutdown_tasks, return_exceptions=True)
            failed = any(isinstance(item, BaseException) for item in result) or failed
        current = tuple(self._inflight)
        if current:
            result = await asyncio.gather(*current, return_exceptions=True)
            failed = any(isinstance(item, BaseException) for item in result) or failed
        transport_closed = False
        try:
            # Never call controller.close(): its public client may have drifted.
            # The captured modular client transfers to this lifecycle; the pilot
            # client, native feed and PilotOwner remain outer-service owned.
            await client.aclose()
            transport_closed = True
        except BaseException:
            failed = True
        self._is_drained = (transport_closed and all(task.done() for task in self._shutdown_tasks)
                            and all(task.done() for task in tuple(self._inflight))
                            and (self._pilot_task is None or self._pilot_task.done())
                            and (self._modular_task is None or self._modular_task.done()))
        self._state = 'stopped' if not failed and self._is_drained else 'shutdown_failed'
        if failed or not self._is_drained:
            raise HubError('shared_staging_shutdown_failed', 503)
