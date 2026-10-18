"""Explicit operator-injected pilot lifecycle; not a production entrypoint.

The caller supplies an already approved manifest/client. No token, host, Docker
capability, watcher or deployment authorization is discovered or created here.
"""
import asyncio
import threading
import weakref

from .container_pilot import ContainerPilotWorkflow
from .container_pilot_client import ContainerPilotClient
from .dispatcher import StagingDispatcher
from .owned_tasks import OwnedPilotTasks, finish_owned
from .pilot_ownership import PilotOwner
from .pilot_recovery import PilotRecovery
from .repository import HubError, HubRepository


_TRANSFERRED_OWNERS = weakref.WeakSet()
_TRANSFER_LOCK = threading.Lock()
# A recovered run may inspect all four original phases and then execute all
# four bounded phases. Cleanup has its own conservative operator budget. This
# is a deployment precondition, not an internal deadline that kills work.
REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS = int(8 * ContainerPilotClient.MAX_TIMEOUT_SECONDS + 120)


class OwnedPilotService:
    def __init__(self, settings, client, targets, *, owner=None, supervisor_stop_grace_seconds=None):
        if (type(supervisor_stop_grace_seconds) is not int
                or supervisor_stop_grace_seconds < REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS):
            raise ValueError('owned_pilot_supervisor_stop_grace_required')
        self.supervisor_stop_grace_seconds = supervisor_stop_grace_seconds
        if not callable(getattr(client, 'aclose', None)):
            raise ValueError('owned_pilot_client_close_required')
        # A provided capability transfers ownership only after exact validation.
        # No ephemeral-storage override is exposed by this factory.
        if owner is not None:
            if type(owner) is not PilotOwner:
                raise ValueError('owned_pilot_capability_required')
            owner.assert_active(settings.database)
        self.owner = owner if owner is not None else PilotOwner(settings.database)
        # Transfer is single-use for the capability object's lifetime. Reject
        # before entering construction cleanup: a duplicate caller must never
        # close the first service's live owner, even if its own setup would fail.
        # Weak references avoid keeping closed capabilities alive indefinitely.
        with _TRANSFER_LOCK:
            if self.owner in _TRANSFERRED_OWNERS:
                raise ValueError('owned_pilot_capability_already_transferred')
            _TRANSFERRED_OWNERS.add(self.owner)
        self._started = self._closed = self._closing = False
        self._shutdown_task = None
        self.tasks = OwnedPilotTasks()
        try:
            self.repo = HubRepository(settings.database, execution_principal='executor:container-pilot', pilot_owner=self.owner)
            self.workflow = ContainerPilotWorkflow(self.repo, client, targets, pilot_owner=self.owner)
            self.dispatcher = StagingDispatcher(self.repo, self.workflow)
            self.recovery = PilotRecovery(self.workflow)
        except BaseException:
            self.owner.close()
            # Client ownership transfers only on successful construction.
            raise
        self.client = client

    @property
    def status(self):
        # Supplied configuration is not evidence about a running Docker/ASGI
        # supervisor; actual installed stop-grace acceptance remains separate.
        return {
            'state': ('stopped' if self._closed else 'stopping' if self._closing
                      else 'active' if self._started else 'not_started'),
            'required_supervisor_stop_grace_seconds': REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS,
            'configured_supervisor_stop_grace_seconds': self.supervisor_stop_grace_seconds,
            'supervisor_configuration_verified': False,
            'configuration_source': 'operator_assertion',
            'tasks': self.tasks.status,
        }

    def assert_active(self):
        try:
            if self._closed or self._closing or not self._started:
                raise ValueError()
            self.owner.assert_active(self.repo.path)
        except Exception:
            raise HubError('owned_pilot_unavailable', 503) from None

    async def start(self, feed):
        if self._started or self._closed or self._closing or not feed.available:
            raise HubError('owned_pilot_startup_unavailable', 503)
        self.owner.assert_active(self.repo.path)
        await self.tasks.start()
        self._started = True

    async def advance(self, run_id):
        self.assert_active()
        return await self.tasks.run('advance', run_id, lambda: self.workflow.advance(run_id))

    async def recover(self, run_id):
        self.assert_active()
        return await self.tasks.run('recover', run_id, lambda: self.recovery.recover(run_id))

    async def stop(self, runtime, feed):
        if self._shutdown_task is None:
            self._closing = True
            self._shutdown_task = asyncio.create_task(self._shutdown(runtime, feed))
        # Repeated ASGI cancellation cannot release ownership while accepted
        # work is still writing or awaiting an executor result.
        await finish_owned(self._shutdown_task)

    async def _shutdown(self, runtime, feed):
        failed = False
        try:
            stops = [self.tasks.stop()]
            if runtime is not None:
                stops.append(runtime.stop())
            results = await asyncio.gather(*stops, return_exceptions=True)
            failed = any(isinstance(result, BaseException) for result in results)
        finally:
            try:
                await feed.stop()
            except BaseException:
                failed = True
            try:
                await self.client.aclose()
            except BaseException:
                failed = True
            finally:
                self._closed = True
                try:
                    self.owner.close()
                except Exception:
                    failed = True
        if failed:
            raise HubError('owned_pilot_shutdown_failed', 503)


def create_owned_pilot_app(settings, client, targets, *, owner=None, event_feed=None,
                           github_client=None, registry_bridge=None, supervisor_stop_grace_seconds=None):
    """Transfer the selected client/capability to one explicit ASGI lifecycle.

    A constructor failure leaves an unaccepted client with the caller. An app
    construction failure closes the owner; the caller still must close its
    supplied asynchronous client. No environment/CLI activation is added.
    The operator must explicitly assert a sufficient supervisor stop grace;
    this value neither configures nor verifies an installed supervisor.
    """
    from .app import create_app
    service = OwnedPilotService(settings, client, targets, owner=owner,
                                supervisor_stop_grace_seconds=supervisor_stop_grace_seconds)
    try:
        return create_app(settings, repository=service.repo, github_client=github_client,
                          event_feed=event_feed, staging_dispatcher=service.dispatcher,
                          container_pilot_workflow=service.workflow,
                          registry_bridge=registry_bridge, owned_pilot_service=service)
    except BaseException:
        service.owner.close()
        raise
