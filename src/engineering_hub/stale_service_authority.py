"""Owner-controlled, default-off authority for a future stale evaluator.

This module starts no scheduler and sends no messages. It is an in-process
admission boundary, not a sandbox for hostile code already inside the Hub.
"""
import math
import os
import time
import uuid
from contextlib import contextmanager

from .pilot_ownership import PilotOwner, PilotOwnershipError
from .repository import HubError, HubRepository

_GRANT_ID = str(uuid.UUID('723f2139-7b6c-465f-a784-d7034bbd2990'))


def _boot_id():
    try:
        with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as stream:
            value = stream.read(64).strip()
        if str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise HubError('stale_service_boot_unavailable', 503) from None


def _boot_clock():
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError, ValueError):
        raise HubError('stale_service_clock_untrusted', 503) from None


class StaleServiceGrant:
    """A single owner-controlled project scope, expiring and CAS-revocable."""
    def __init__(self, repo, *, clock=time.time, monotonic=_boot_clock, boot_id=_boot_id):
        if type(repo) is not HubRepository or not callable(clock) or not callable(monotonic) or not callable(boot_id):
            raise ValueError('exact Hub repository and clock required')
        self.repo, self.clock, self.monotonic, self.boot_id = repo, clock, monotonic, boot_id

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise HubError('stale_service_clock_untrusted', 409)
        return value

    def _monotonic(self):
        value = self.monotonic()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise HubError('stale_service_clock_untrusted', 409)
        return value

    @staticmethod
    def _read(db):
        row = db.execute("SELECT data FROM hub_entities WHERE id=? AND kind='stale_service_grant'", (_GRANT_ID,)).fetchone()
        if row is None:
            return None
        import json
        try:
            item = json.loads(row['data'])
        except (ValueError, TypeError):
            raise HubError('stale_service_grant_corrupt', 409) from None
        if type(item) is not dict:
            raise HubError('stale_service_grant_corrupt', 409)
        if (item.get('id') != _GRANT_ID or type(item.get('revision')) is not int or item['revision'] < 1
                or type(item.get('enabled')) is not bool or type(item.get('projects')) is not list
                or not 1 <= len(item['projects']) <= 32 or any(type(p) is not str for p in item['projects'])
                or len(set(item['projects'])) != len(item['projects']) or item.get('purpose') != 'stale_evaluation_only'
                or item.get('execution_authorization') is not False or item.get('scheduler') != 'not_installed'
                or item.get('delivery') != 'not_configured' or type(item.get('boot_id')) is not str
                or type(item.get('owner_epoch')) is not str
                or any(type(item.get(k)) not in (int, float) or not math.isfinite(item[k]) for k in
                       ('approved_at', 'expires_at', 'approved_monotonic', 'expires_monotonic'))
                or item['expires_at'] <= item['approved_at'] or item['expires_monotonic'] <= item['approved_monotonic']):
            raise HubError('stale_service_grant_corrupt', 409)
        return item

    def owner_get(self, actor):
        self.repo._actor(actor, human=True)
        with self.repo._connection() as db:
            item = self._read(db)
            if item is None:
                return dict(revision=0, state='disabled', projects=[], scheduler='not_installed',
                            delivery='not_configured', execution_authorization=False)
            owner = self.repo.pilot_owner
            owner_current = False
            if type(owner) is PilotOwner and owner.epoch == item['owner_epoch']:
                try:
                    owner.assert_active(self.repo.path)
                    owner_current = True
                except PilotOwnershipError:
                    pass
            return {**item, 'state': ('revoked' if not item['enabled'] else
                    'boot_changed' if self.boot_id() != item['boot_id'] else
                    'owner_changed' if not owner_current else
                    'expired' if self._now() < item['approved_at'] or self._now() >= item['expires_at']
                    or self._monotonic() < item['approved_monotonic'] or self._monotonic() >= item['expires_monotonic'] else 'authorized'),
                    'scheduler': 'not_installed', 'delivery': 'not_configured'}

    def owner_configure(self, actor, expected_revision, projects, lifetime_seconds):
        self.repo._actor(actor, human=True)
        if (type(expected_revision) is not int or expected_revision < 0 or type(projects) is not list
                or not 1 <= len(projects) <= 32 or any(type(p) is not str for p in projects)
                or len(set(projects)) != len(projects) or type(lifetime_seconds) is not int
                or not 60 <= lifetime_seconds <= 86400):
            raise HubError('invalid_stale_service_grant', 422)
        owner = self.repo.pilot_owner
        if type(owner) is not PilotOwner:
            raise HubError('stale_service_owner_required', 409)
        try:
            owner.assert_active(self.repo.path)
        except PilotOwnershipError:
            raise HubError('stale_service_owner_required', 409) from None
        with self.repo._transaction() as db:
            for project in projects:
                self.repo._project(project, db)
            old = self._read(db)
            if expected_revision != (old['revision'] if old else 0):
                raise HubError('revision_conflict', 409)
            now = self._now()
            mono = self._monotonic()
            boot = self.boot_id()
            try:
                if type(boot) is not str or str(uuid.UUID(boot)) != boot:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise HubError('stale_service_boot_unavailable', 503) from None
            item = dict(id=_GRANT_ID, revision=expected_revision, enabled=True, projects=sorted(projects),
                        purpose='stale_evaluation_only', approved_by=actor, approved_at=now,
                        expires_at=now + lifetime_seconds, approved_monotonic=mono,
                        expires_monotonic=mono + lifetime_seconds, boot_id=boot, owner_epoch=owner.epoch, execution_authorization=False,
                        scheduler='not_installed', delivery='not_configured')
            return self.repo._save(db, actor, 'stale_service.authorized', 'stale_service_grant', item,
                                   new=old is None, publish=False)

    def owner_revoke(self, actor, expected_revision):
        self.repo._actor(actor, human=True)
        with self.repo._transaction() as db:
            item = self._read(db)
            if item is None:
                raise HubError('stale_service_grant_absent', 404)
            self.repo._cas(item, expected_revision)
            item['enabled'] = False
            return self.repo._save(db, actor, 'stale_service.revoked', 'stale_service_grant', item, publish=False)

    def bind(self):
        """Bind to the current exact live PilotOwner; no work is started."""
        owner = self.repo.pilot_owner
        if type(owner) is not PilotOwner:
            raise HubError('stale_service_owner_required', 409)
        try:
            owner.assert_active(self.repo.path)
        except PilotOwnershipError:
            raise HubError('stale_service_owner_required', 409) from None
        return StaleServiceAuthority(self, owner)


class StaleServiceAuthority:
    """Internal PID/owner-epoch bound capability, not a general external grant."""
    __slots__ = ('_grant', '_owner', '_pid', '_epoch')

    def __init__(self, grant, owner):
        if type(grant) is not StaleServiceGrant or type(owner) is not PilotOwner or grant.repo.pilot_owner is not owner:
            raise HubError('stale_service_owner_required', 409)
        owner.assert_active(grant.repo.path)
        self._grant, self._owner, self._pid, self._epoch = grant, owner, os.getpid(), owner.epoch

    def _check(self, db, project, expected_revision):
        repo = self._grant.repo
        try:
            if os.getpid() != self._pid or repo.pilot_owner is not self._owner or self._owner.epoch != self._epoch:
                raise PilotOwnershipError()
            self._owner.assert_active(repo.path)
        except PilotOwnershipError:
            raise HubError('stale_service_owner_required', 409) from None
        item = self._grant._read(db)
        now, mono = self._grant._now(), self._grant._monotonic()
        if (item is None or type(expected_revision) is not int or item['revision'] != expected_revision
                or not item['enabled'] or self._grant.boot_id() != item['boot_id']
                or item['owner_epoch'] != self._epoch
                or not item['approved_at'] <= now < item['expires_at']
                or not item['approved_monotonic'] <= mono < item['expires_monotonic']):
            raise HubError('stale_service_grant_inactive', 403)
        if type(project) is not str or project not in item['projects']:
            raise HubError('stale_service_project_forbidden', 403)
        repo._project(project, db)
        if repo._paused(db):
            raise HubError('stale_service_paused', 423)
        return item

    @contextmanager
    def _admit(self, project, expected_revision):
        """Internal trusted-code scope; verify grant before a cooperative precommit check.

        The SQLite writer serializes revoke/configure with the full operation.
        The scoped process must not yield/await or dispatch external effects while
        holding this transaction. A stalled SQLite commit can outlast a clock
        deadline; this check is not an interruptible real-time fence. No
        evaluator currently calls this method.
        """
        with self._grant.repo._transaction() as db:
            self._check(db, project, expected_revision)
            yield db
            self._check(db, project, expected_revision)
