"""Single-run native coordinator for disposable local test fixtures only.

Reconciliation identity and attestations here are intentionally synthetic; this
is never staging enrollment, host-custody evidence or production authority.
The caller owns and drains the separately constructed broker/Engine resources.
"""
import asyncio
import math
import os
from uuid import uuid4

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.lifecycle import finish_owned
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubRepository
from engineering_hub.startup_reconciliation import ExecutorSessionIdentity, OperatorReconciliation, StartupReconciliation
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget
from hub_container_executor.adapter import HostAdapter
from scripts.hub_local_native_binding import LocalNativeBinding

ACTION='engineering-hub:container-pilot'
MACHINE='staging-pc'
PRINCIPAL='executor:container-pilot'


class _LocalClient:
    def __init__(self, prepare):
        self.timeout_seconds=125.0
        self.prepare=prepare; self.binding=None; self.adapter=None; self.attempted=False
        self.calls=[]; self.responses=[]; self.errors=[]; self.bound=None
    async def submit_command(self, command):
        if self.adapter is None:
            if self.attempted or command.action != 'deploy_container':
                raise RuntimeError('Local fixture binding unavailable')
            self.attempted=True
            bound=self.binding.bind_first_deploy(command)
            self.bound=bound
            task = asyncio.create_task(asyncio.to_thread(self.prepare,bound))
            try:
                adapter=await finish_owned(task)
            except asyncio.CancelledError:
                if task.done() and not task.cancelled():
                    try:
                        orphan=task.result()
                        if type(orphan) is HostAdapter: orphan.close()
                    except Exception:
                        pass
                raise
            if type(adapter) is not HostAdapter:
                raise RuntimeError('Actual local HostAdapter required')
            self.adapter=adapter
        self.calls.append(command.action)
        try:
            response=await finish_owned(asyncio.to_thread(self.adapter.submit,command))
            self.responses.append((response.state,response.reason,response.evidence.container_id,response.evidence.health))
            return response
        except Exception as error:
            self.errors.append(type(error).__name__)
            raise
    async def get_command(self, command):
        if self.adapter is None: raise RuntimeError('Local fixture binding unavailable')
        return await finish_owned(asyncio.to_thread(self.adapter.get,command.command_id))


class LocalNativeCoordinator:
    """Own one synthetic-attestation queue lifecycle and one admitted run."""
    def __init__(self, *, database, manifest, task_owner, prepare_bound, timeout_seconds=30):
        if (not callable(prepare_bound) or type(timeout_seconds) not in (int,float)
                or not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 600):
            raise ValueError('Local fixture preparer required')
        self.timeout_seconds=float(timeout_seconds)
        self._pid=os.getpid(); self._used=False; self._started=False; self._feed_started=False; self._closed=False
        self._close_task=None; self._feed_start_task=None; self._runtime_start_task=None
        self.reconciliation=None
        self.owner=PilotOwner(database, require_existing=True, allow_ephemeral_test_storage=True)
        try:
            self.repo=HubRepository(database, execution_principal=PRINCIPAL,pilot_owner=self.owner)
            self.client=_LocalClient(prepare_bound)
            self.workflow=ContainerPilotWorkflow(self.repo,self.client,[ContainerPilotTarget.from_manifest(manifest)],pilot_owner=self.owner)
            self.queue=WorkQueue(self.repo,{ACTION:WorkAction(MACHINE,project='engineering-hub')})
            target=self.workflow.targets['engineering-hub:staging']
            executor=ExecutorSessionIdentity(str(uuid4()),'sha256:'+'e'*64)
            self.reconciliation=StartupReconciliation(owner=self.owner,database=database,target_id=target.target_id,
                manifest_digest=target.manifest_digest,executor=executor,lifetime_seconds=3600,
                current_executor_identity=lambda:executor)
            self.reconciliation.accept_operator_reconciliation(owner=self.owner,report=OperatorReconciliation(
                self.reconciliation.challenge,self.owner.epoch,target.target_id,target.manifest_digest,executor,
                'sha256:'+'f'*64,True,True,True))
            self.bridge=QueuedContainerPilot(self.queue,self.workflow,action_key=ACTION,machine_id=MACHINE,reconciliation=self.reconciliation)
            self.client.binding=LocalNativeBinding(bridge=self.bridge,manifest=manifest,owner=task_owner)
            self.feed=NativeHubEventFeed(database); self.runtime=QueuedPilotRuntime(self.bridge,self.feed)
            self.manifest=manifest
        except BaseException:
            try:
                if self.reconciliation is not None: self.reconciliation.close()
            finally:
                self.owner.close()
            raise

    async def run_once(self):
        if os.getpid()!=self._pid or self._used or self._close_task is not None: raise RuntimeError('Local fixture already used')
        self._used=True
        task=self.repo.create_task('human:1234','engineering-hub','Synthetic local native broker','native-broker')
        task=self.repo.claim_task('codex',task['id'],task['revision'])
        task=self.repo.set_candidate('codex',task['id'],task['revision'],self.manifest.candidate.source_commit)
        task=self.repo.review_task('claude',task['id'],task['revision'],self.manifest.candidate.source_commit,'approve')
        job=self.queue.submit(actor='codex',request_id='native-broker',action_key=ACTION,environment='staging',
            target=WorkTarget(task['id'],self.manifest.candidate.source_commit,self.manifest.candidate.image_config_id))
        await self._start_feed_and_runtime()
        async with asyncio.timeout(self.timeout_seconds):
            while self.runtime.status['last_job'] is None: await asyncio.sleep(.01)
        return job,self.runtime.status['last_job']

    async def _start_feed_and_runtime(self):
        self._feed_started=True
        self._feed_start_task=asyncio.create_task(self.feed.start())
        if not (await finish_owned(self._feed_start_task))['available']:
            raise RuntimeError('Local native feed unavailable')
        if self._close_task is not None: raise RuntimeError('Local coordinator is closing')
        self._started=True
        self._runtime_start_task=asyncio.create_task(self.runtime.start())
        await finish_owned(self._runtime_start_task)
        if self._close_task is not None: raise RuntimeError('Local coordinator is closing')

    async def close(self):
        if os.getpid()!=self._pid: raise RuntimeError('Local coordinator process changed')
        if self._close_task is None:
            self._close_task=asyncio.create_task(self._close_owned())
        await finish_owned(self._close_task)

    async def _close_owned(self):
        try:
            try:
                if self._feed_start_task is not None: await finish_owned(self._feed_start_task)
            finally:
                try:
                    if self._runtime_start_task is not None: await finish_owned(self._runtime_start_task)
                finally:
                    if self._started: await self.runtime.stop()
        finally:
            try:
                if self._feed_started: await finish_owned(self.feed.stop())
            finally:
                try:
                    if self.client.adapter is not None: self.client.adapter.close()
                finally:
                    try: self.reconciliation.close()
                    finally: self.owner.close()
        self._closed=True
