"""One-shot staging validation with durable intent and trusted host receipts.

Enrollment is a trusted local operator action, not image-label attestation or
an agent API. This module starts no listener/thread/loop and executes no shell.
The injected backend must enforce the absolute deadline and independently
observe container identity, profile, exit and quiescence. Mock backend tests do
not establish that real Docker isolation works.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Protocol
from uuid import UUID, uuid4

from engineering_hub.repository import AGENTS, PROJECTS
from engineering_hub.work_queue import WorkQueue, WorkTarget, WorkWorker


class ValidationError(RuntimeError):
    """Safe bounded error code; never includes commands or backend errors."""


def _sha(value):
    return type(value) is str and re.fullmatch(r'sha256:[a-f0-9]{64}', value) is not None


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def _key(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class ValidationProfile:
    uid: int = 10001
    gid: int = 10001
    memory_bytes: int = 1073741824
    cpu_millicores: int = 1000
    pids_limit: int = 128
    tmpfs_bytes: int = 134217728

    def __post_init__(self):
        bounds = {'uid': (10001, 10001), 'gid': (10001, 10001),
                  'memory_bytes': (67108864, 4294967296), 'cpu_millicores': (100, 4000),
                  'pids_limit': (16, 512), 'tmpfs_bytes': (1048576, 536870912)}
        if any(type(getattr(self, key)) is not int or not low <= getattr(self, key) <= high
               for key, (low, high) in bounds.items()):
            raise ValidationError('invalid_validation_profile')
        if self.tmpfs_bytes > self.memory_bytes:
            raise ValidationError('invalid_validation_profile')

    def digest(self):
        # These flags are fixed, not caller-selectable profile options.
        data = {**asdict(self), 'network': 'none', 'read_only': True,
                'cap_drop': ['ALL'], 'no_new_privileges': True,
                'privileged': False, 'host_mounts': False, 'published_ports': False}
        return 'sha256:' + hashlib.sha256(_json(data).encode()).hexdigest()


@dataclass(frozen=True)
class ValidationEnrollment:
    action_key: str
    project: str
    machine_id: str
    source_commit: str
    artifact_digest: str
    image_config_id: str
    platform: str
    argv: tuple[str, ...]
    profile: ValidationProfile
    timeout_seconds: int

    def __post_init__(self):
        if (not _key(self.action_key) or not _key(self.machine_id)
                or type(self.project) is not str or self.project not in PROJECTS
                or type(self.source_commit) is not str or re.fullmatch(r'[a-f0-9]{40}', self.source_commit) is None
                or not _sha(self.artifact_digest) or not _sha(self.image_config_id)
                or type(self.platform) is not str or self.platform not in {'linux/amd64', 'linux/arm64'}
                or type(self.argv) is not tuple or not 1 <= len(self.argv) <= 32
                or any(type(arg) is not str or not 1 <= len(arg) <= 512
                       or any(ord(character) < 32 or ord(character) > 126 for character in arg) for arg in self.argv)
                or type(self.profile) is not ValidationProfile
                or type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 300):
            raise ValidationError('invalid_validation_enrollment')


@dataclass(frozen=True)
class ValidationJob:
    job_id: str
    action_key: str
    project: str
    machine_id: str
    environment: str
    target: WorkTarget
    worker: WorkWorker
    fence: int

    def __post_init__(self):
        if (not _uuid(self.job_id) or not _key(self.action_key) or not _key(self.machine_id)
                or type(self.project) is not str or self.project not in PROJECTS
                or type(self.environment) is not str or self.environment != 'staging'
                or type(self.target) is not WorkTarget or type(self.worker) is not WorkWorker
                or type(self.fence) is not int or not 1 <= self.fence <= 2**63 - 1):
            raise ValidationError('invalid_validation_job')

    @classmethod
    def from_claim(cls, row):
        try:
            agent, instance = row['worker'].rsplit(':', 1)
            if row['requires_execution_proof'] is not True:
                raise ValueError()
            return cls(row['id'], row['action_key'], row['project'], row['machine_id'],
                       row['environment'], WorkTarget(**row['target']), WorkWorker(agent, instance), row['fence'])
        except Exception:
            raise ValidationError('invalid_validation_claim') from None

    def canonical_bytes(self):
        return _json(asdict(self)).encode()


@dataclass(frozen=True)
class ValidationObservation:
    container_id: str
    image_config_id: str
    profile_digest: str
    exit_code: int
    quiescent: bool

    def __post_init__(self):
        if (type(self.container_id) is not str or re.fullmatch(r'[a-f0-9]{64}', self.container_id) is None
                or not _sha(self.image_config_id) or not _sha(self.profile_digest)
                or type(self.exit_code) is not int or not 0 <= self.exit_code <= 255
                or type(self.quiescent) is not bool):
            raise ValidationError('invalid_validation_observation')


class ValidationBackend(Protocol):
    def execute(self, enrollment: ValidationEnrollment, job: ValidationJob, intent_id: str,
                *, deadline_monotonic: float) -> ValidationObservation: ...


class ValidationLedger:
    """Dedicated private SQLite intent/receipt store, never the Hub database.

    Only trusted local composition can instantiate/write this provider. No
    endpoint accepts a receipt. Existing files require explicit create=False and
    the exact schema marker. Private directory trust excludes a hostile same-UID
    administrator; this does not pretend to defend against that administrator.
    """
    SCHEMA = 'hub_validation_receipts_v1'

    def __init__(self, path, *, create, allow_windows_test_storage=False):
        if type(create) is not bool or type(allow_windows_test_storage) is not bool:
            raise ValidationError('invalid_validation_storage')
        if os.name != 'posix' and not allow_windows_test_storage:
            raise ValidationError('linux_private_storage_required')
        self.path = Path(path).absolute()
        parent = self.path.parent
        parent_info = parent.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise ValidationError('invalid_validation_storage')
        if os.name == 'posix' and (parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) != 0o700):
            raise ValidationError('private_validation_directory_required')
        self._parent_identity = (parent_info.st_dev, parent_info.st_ino)
        if create:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or os.name == 'posix' and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600)):
            raise ValidationError('private_validation_database_required')
        self._identity = (info.st_dev, info.st_ino)
        if create:
            with self._connection(write=True) as db:
                db.execute('CREATE TABLE validation_marker (schema TEXT NOT NULL)')
                db.execute('INSERT INTO validation_marker VALUES(?)', (self.SCHEMA,))
                db.execute('''CREATE TABLE validation_receipts (
                    job_id TEXT NOT NULL, fence INTEGER NOT NULL, intent_id TEXT NOT NULL UNIQUE,
                    job TEXT NOT NULL, enrollment TEXT NOT NULL, state TEXT NOT NULL,
                    evidence_id TEXT UNIQUE, observation TEXT, outcome TEXT,
                    PRIMARY KEY(job_id,fence))''')
            self._fsync_parent()
        with self._connection() as db:
            rows = db.execute('SELECT schema FROM validation_marker').fetchall()
            if rows != [(self.SCHEMA,)]:
                raise ValidationError('validation_schema_mismatch')
            if {row[1] for row in db.execute('PRAGMA table_info(validation_receipts)')} != {
                    'job_id', 'fence', 'intent_id', 'job', 'enrollment', 'state', 'evidence_id', 'observation', 'outcome'}:
                raise ValidationError('validation_schema_mismatch')

    def _guard(self):
        parent = self.path.parent.lstat()
        info = self.path.lstat()
        if ((parent.st_dev, parent.st_ino) != self._parent_identity
                or (info.st_dev, info.st_ino) != self._identity
                or not stat.S_ISDIR(parent.st_mode) or not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(parent.st_mode) or info.st_nlink != 1
                or os.name == 'posix' and (parent.st_uid != os.geteuid() or info.st_uid != os.geteuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700 or stat.S_IMODE(info.st_mode) != 0o600)):
            raise ValidationError('validation_storage_changed')

    def _fsync_parent(self):
        if os.name == 'posix':
            fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @contextmanager
    def _connection(self, *, write=False):
        self._guard()
        db = sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=1)
        try:
            if write:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            yield db
            self._guard()
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _begin(self, job, enrollment):
        intent_id = str(uuid4())
        try:
            with self._connection(write=True) as db:
                db.execute('INSERT INTO validation_receipts VALUES(?,?,?,?,?,?,NULL,NULL,NULL)',
                           (job.job_id, job.fence, intent_id, job.canonical_bytes().decode(),
                            _json(asdict(enrollment)), 'intent'))
        except sqlite3.IntegrityError:
            raise ValidationError('validation_intent_already_exists') from None
        return intent_id

    def _hold(self, job):
        with self._connection(write=True) as db:
            db.execute("UPDATE validation_receipts SET state='held' WHERE job_id=? AND fence=? AND state='intent'", (job.job_id, job.fence))

    def _record(self, job, enrollment, intent_id, observation):
        if (type(observation) is not ValidationObservation or not observation.quiescent
                or observation.image_config_id != enrollment.image_config_id
                or observation.profile_digest != enrollment.profile.digest()):
            raise ValidationError('validation_observation_unverified')
        evidence_id = str(uuid4())
        outcome = 'succeeded' if observation.exit_code == 0 else 'failed'
        with self._connection(write=True) as db:
            row = db.execute('SELECT job,enrollment,state FROM validation_receipts WHERE job_id=? AND fence=? AND intent_id=?', (job.job_id, job.fence, intent_id)).fetchone()
            if row != (job.canonical_bytes().decode(), _json(asdict(enrollment)), 'intent'):
                raise ValidationError('validation_intent_mismatch')
            db.execute("UPDATE validation_receipts SET state='terminal',evidence_id=?,observation=?,outcome=? WHERE job_id=? AND fence=?", (evidence_id, _json(asdict(observation)), outcome, job.job_id, job.fence))
        return evidence_id, outcome

    def verify_completion(self, job_public, evidence_id, outcome):
        try:
            job = ValidationJob.from_claim(job_public)
            if not _uuid(evidence_id) or outcome not in {'succeeded', 'failed'}:
                return False
            with self._connection() as db:
                row = db.execute('SELECT job,enrollment,state,observation,outcome FROM validation_receipts WHERE job_id=? AND fence=? AND evidence_id=?', (job.job_id, job.fence, evidence_id)).fetchone()
            if row is None or row[0] != job.canonical_bytes().decode() or row[2] != 'terminal' or row[4] != outcome:
                return False
            raw = json.loads(row[1])
            raw['argv'] = tuple(raw['argv'])
            raw['profile'] = ValidationProfile(**raw['profile'])
            enrolled = ValidationEnrollment(**raw)
            observation = ValidationObservation(**json.loads(row[3]))
            return (enrolled.action_key == job.action_key and enrolled.project == job.project
                    and enrolled.machine_id == job.machine_id
                    and enrolled.source_commit == job.target.source_commit
                    and enrolled.artifact_digest == job.target.artifact_digest
                    and observation.quiescent and observation.image_config_id == enrolled.image_config_id
                    and observation.profile_digest == enrolled.profile.digest()
                    and outcome == ('succeeded' if observation.exit_code == 0 else 'failed'))
        except Exception:
            return False

    def inspect(self, *, job_id, fence):
        if not _uuid(job_id) or type(fence) is not int or fence < 1:
            raise ValidationError('invalid_validation_identity')
        with self._connection() as db:
            row = db.execute('SELECT intent_id,state,evidence_id,outcome,observation FROM validation_receipts WHERE job_id=? AND fence=?', (job_id, fence)).fetchone()
        if row is None:
            return None
        result = dict(zip(('intent_id', 'state', 'evidence_id', 'outcome'), row[:4]))
        result['container_id'] = (ValidationObservation(**json.loads(row[4])).container_id
                                  if row[1] == 'terminal' and row[4] is not None else None)
        return result


class ValidationWorker:
    """One already-claimed job, one effect attempt, no automatic retry."""
    def __init__(self, queue, ledger, backend, enrollments, *, monotonic=time.monotonic):
        if type(queue) is not WorkQueue or type(ledger) is not ValidationLedger or not callable(monotonic):
            raise ValidationError('invalid_validation_composition')
        if Path(queue.repo.path).absolute() == ledger.path:
            raise ValidationError('separate_validation_ledger_required')
        if (getattr(queue.completion_verifier, '__self__', None) is not ledger
                or getattr(queue.completion_verifier, '__func__', None) is not ValidationLedger.verify_completion):
            raise ValidationError('validation_receipt_verifier_required')
        if type(enrollments) is not tuple or not 1 <= len(enrollments) <= 32 or any(type(item) is not ValidationEnrollment for item in enrollments):
            raise ValidationError('invalid_validation_enrollments')
        self.enrollments = {item.action_key: item for item in enrollments}
        if len(self.enrollments) != len(enrollments) or not callable(getattr(backend, 'execute', None)):
            raise ValidationError('invalid_validation_composition')
        self.queue, self.ledger, self.backend, self.monotonic = queue, ledger, backend, monotonic

    def run_once(self, *, job_id, worker, fence):
        row = self.queue.live_claim(job_id=job_id, worker=worker, fence=fence)
        job = ValidationJob.from_claim(row)
        enrolled = self.enrollments.get(job.action_key)
        if (enrolled is None or enrolled.project != job.project or enrolled.machine_id != job.machine_id
                or enrolled.source_commit != job.target.source_commit
                or enrolled.artifact_digest != job.target.artifact_digest):
            raise ValidationError('validation_enrollment_mismatch')
        remaining = row['lease_until'] - self.queue.clock()
        if not math.isfinite(remaining) or remaining < enrolled.timeout_seconds + 10:
            raise ValidationError('validation_lease_budget_required')
        started = self.monotonic()
        if type(started) not in {int, float} or not math.isfinite(started) or started < 0:
            raise ValidationError('invalid_validation_deadline')
        intent_id = self.ledger._begin(job, enrolled)
        deadline = started + enrolled.timeout_seconds
        try:
            # No SQLite transaction spans external I/O. Recheck a pause, review
            # change or fencing loss after durable admission and before effect.
            current = self.queue.live_claim(job_id=job_id, worker=worker, fence=fence)
            if ValidationJob.from_claim(current) != job:
                raise ValidationError('validation_claim_changed')
            observation = self.backend.execute(enrolled, job, intent_id, deadline_monotonic=deadline)
            finished = self.monotonic()
            if type(finished) not in {int, float} or not math.isfinite(finished) or finished < started or finished >= deadline:
                raise ValidationError('validation_deadline_exceeded')
            # Must still own the original live instance before recording success.
            current = self.queue.live_claim(job_id=job_id, worker=worker, fence=fence)
            if ValidationJob.from_claim(current) != job:
                raise ValidationError('validation_claim_changed')
            evidence_id, outcome = self.ledger._record(job, enrolled, intent_id, observation)
            completed = self.queue.complete(job_id=job_id, worker=worker, fence=fence,
                                            outcome=outcome, evidence_id=evidence_id)
            return {'job_id': job_id, 'state': completed['state'], 'evidence_id': evidence_id,
                    'execution_attempted': True}
        except Exception:
            self.ledger._hold(job)
            try:
                self.queue.complete(job_id=job_id, worker=worker, fence=fence, outcome='held')
            except Exception:
                pass  # Expired/lost claims already block machine reuse.
            return {'job_id': job_id, 'state': 'held', 'evidence_id': None,
                    'execution_attempted': True}
