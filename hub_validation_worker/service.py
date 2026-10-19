"""Explicit private executor composition, not a deployed entrypoint.

Only trusted local code supplies enrollment, backend, ownership and native feed.
No agent token, SSH credential, endpoint or host command is discovered here.
Construction may initialize queue tables; imports and construction start no work.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio
import os
from pathlib import Path
import stat
import threading
import weakref

from engineering_hub.events import NativeHubEventFeed
from engineering_hub.owned_tasks import finish_owned
from engineering_hub.pilot_ownership import _local_filesystem
from engineering_hub.repository import HubRepository
from engineering_hub.work_queue import SOFTWARE_VALIDATION_EXECUTOR, WorkTarget, WorkWorker
from engineering_hub.work_queue_config import configured_work_queue
from .core import ValidationEnrollment, ValidationError, ValidationLedger, ValidationWorker
from .ownership import ValidationOwner
from .runtime import ValidationRuntime


_TRANSFERRED = weakref.WeakSet()
_TRANSFER_LOCK = threading.Lock()


@dataclass(frozen=True)
class EnrolledValidation:
    enrollment: ValidationEnrollment
    target: WorkTarget

    def __post_init__(self):
        if (type(self.enrollment) is not ValidationEnrollment or type(self.target) is not WorkTarget
                or self.enrollment.source_commit != self.target.source_commit
                or self.enrollment.artifact_digest != self.target.artifact_digest):
            raise ValidationError('validation_enrollment_target_mismatch')


class OwnedValidationService:
    """One private owner, native feed and bounded sequential executor runtime.

    The ordinary Hub API remains a separate process with closed claim admission.
    Both databases must be local, never shared SQLite over SMB. A successful
    constructor transfers the owner's lifecycle; stop/lifespan drains accepted
    work before release. It never enrolls a container-pilot database.
    """
    def __init__(self, repo, ledger, backend, admissions, owner, feed, *,
                 supervisor_stop_grace_seconds, allow_ephemeral_test_storage=False):
        if (type(repo) is not HubRepository or type(ledger) is not ValidationLedger
                or type(owner) is not ValidationOwner or type(feed) is not NativeHubEventFeed
                or type(admissions) is not tuple or not 1 <= len(admissions) <= 32
                or any(type(item) is not EnrolledValidation for item in admissions)
                or type(allow_ephemeral_test_storage) is not bool):
            raise ValidationError('validation_service_composition_invalid')
        owner.assert_active()
        if ledger.path.parent != owner.directory or ledger.path.name == 'validation-owner.lock':
            raise ValidationError('validation_private_ledger_scope_required')
        self.repo, self.ledger, self.owner, self.feed = repo, ledger, owner, feed
        self.path = Path(repo.path).absolute()
        if (Path(feed.path).absolute() != self.path or ledger.path == self.path
                or repo.pilot_owner is not None or repo.execution_principal is not None):
            raise ValidationError('validation_service_database_scope_invalid')
        info, parent = self.path.lstat(), self.path.parent.lstat()
        if (self.path.resolve(strict=True) != self.path or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or not stat.S_ISDIR(parent.st_mode)):
            raise ValidationError('validation_service_database_scope_invalid')
        self._database_identity = info.st_dev, info.st_ino
        self._parent_identity = parent.st_dev, parent.st_ino
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            _local_filesystem(fd, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
        finally:
            os.close(fd)  # A directory FD, never a raw SQLite database FD.
        mapping = {item.enrollment.action_key: item for item in admissions}
        machines = {item.enrollment.machine_id for item in admissions}
        if len(mapping) != len(admissions) or len(machines) != 1:
            raise ValidationError('validation_service_one_physical_machine_required')
        self.required_stop_grace_seconds = max(item.enrollment.timeout_seconds for item in admissions) + 30
        if (type(supervisor_stop_grace_seconds) is not int
                or supervisor_stop_grace_seconds < self.required_stop_grace_seconds):
            raise ValidationError('validation_supervisor_stop_grace_required')
        self.configured_stop_grace_seconds = supervisor_stop_grace_seconds
        self.identity = WorkWorker(SOFTWARE_VALIDATION_EXECUTOR, owner.instance_id)
        self._shutdown_task = None
        self.state = 'not_started'
        self._guard()
        with _TRANSFER_LOCK:
            if owner in _TRANSFERRED:
                raise ValidationError('validation_owner_already_transferred')
            _TRANSFERRED.add(owner)
        try:
            def admit(worker, action, environment, machine, target):
                self._guard()
                item = mapping.get(action)
                return (worker == self.identity and item is not None
                        and environment == 'staging' and machine == item.enrollment.machine_id
                        and target == item.target
                        and getattr(self.queue.completion_verifier, '__self__', None) is ledger
                        and getattr(self.queue.completion_verifier, '__func__', None)
                        is ValidationLedger.verify_completion)

            self.queue = configured_work_queue(repo, machine_id=next(iter(machines)), claim_verifier=admit)
            for item in admissions:
                if self.queue.action_project(item.enrollment.action_key) != item.enrollment.project:
                    raise ValidationError('validation_service_action_scope_invalid')
            self.queue.completion_verifier = ledger.verify_completion
            self.worker = ValidationWorker(self.queue, ledger, backend,
                                           tuple(item.enrollment for item in admissions))
            self.runtime = ValidationRuntime(self.queue, self.worker, self.identity, feed,
                                             owner_guard=self._guard)
        except BaseException:
            with _TRANSFER_LOCK:
                _TRANSFERRED.discard(owner)
            raise  # Construction did not accept owner lifecycle or external work.

    def _guard(self):
        self.owner.assert_active()
        info, parent = self.path.lstat(), self.path.parent.lstat()
        if (self.path.resolve(strict=True) != self.path or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or (info.st_dev, info.st_ino) != self._database_identity
                or not stat.S_ISDIR(parent.st_mode)
                or (parent.st_dev, parent.st_ino) != self._parent_identity):
            raise ValidationError('validation_service_database_changed')
        with self.repo._connection() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('hub_pilot_owner','hub_dispatch_cursor')").fetchone():
                raise ValidationError('validation_service_legacy_dispatch_forbidden')

    @property
    def status(self):
        return {'state': self.state, 'runtime': self.runtime.status,
                'required_supervisor_stop_grace_seconds': self.required_stop_grace_seconds,
                'configured_supervisor_stop_grace_seconds': self.configured_stop_grace_seconds,
                'supervisor_configuration_verified': False,
                'configuration_source': 'operator_assertion'}

    async def start(self):
        if self.state != 'not_started':
            raise ValidationError('validation_service_already_started')
        self.state = 'starting'
        try:
            self._guard()
            await self.feed.start()
            if not self.feed.available:
                raise ValidationError('validation_native_source_unavailable')
            await self.runtime.start()
            self.state = 'active'
            return self.status
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self._shutdown_task is None:
            self.state = 'stopping'
            self._shutdown_task = asyncio.create_task(self._shutdown())
        return await finish_owned(self._shutdown_task)

    async def _shutdown(self):
        failed = False
        try:
            await self.runtime.stop()
        except BaseException:
            failed = True
        if self.runtime.status['active']:
            self.state = 'failed'
            raise ValidationError('validation_shutdown_not_quiescent')
        try:
            await self.feed.stop()
        except BaseException:
            failed = True
        finally:
            self.owner.close()
        self.state = 'failed' if failed else 'stopped'
        if failed:
            raise ValidationError('validation_service_shutdown_failed')
        return self.status

    @asynccontextmanager
    async def lifespan(self):
        try:
            await self.start()
            yield self
        finally:
            await self.stop()
