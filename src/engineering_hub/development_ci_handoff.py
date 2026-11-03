"""Disabled-by-default durable CI handoff between the Hub queue and a CI host.

Nothing here is activated by import, environment, MCP or REST. Every entry
point refuses unless trusted operator code passes ``enable_durable_handoff=True``
and every external authority below is explicitly supplied. All defaults refuse.

Protocol:

* Host per-attempt states ``intent -> running -> terminal_held ->
  finalized_reserved``. ``finalized_reserved`` is NOT free capacity; the host
  never self-frees. A new start is admitted only when (a) its fence exceeds every
  fence known locally AND the fence confirmed by an external non-rollback anchor,
  (b) every earlier attempt is ``finalized_reserved``, and (c) it carries a
  Hub-signed *release* statement for exactly the host's latest attempt tuple,
  which the Hub signs only after its queue released that job.
* Hub: ``receipt_committed`` (one txn) binds the authenticated host inspection to
  the ORIGINAL claim/attempt and stores a random ``hub_ack``. An authenticated
  host ``finalized_reserved`` ack is then recorded and re-verifiable
  (``host_acked``, with its credential). Only then can ONE queue transaction
  release the machine; a ledger trigger marks the handoff ``finalized`` in it.

Authorities NOT supplied by this repository (each defaults to refusing, so every
attempt stays held until it exists): an authenticated Hub<->host channel and
keys (``RefusingAuthenticator``), an external non-rollback fence anchor
(``RefusingExternalAnchor``), a host->Hub current-claim confirmation path
(``RefusingClaimConfirmer``), a terminal verifier that re-inspects the original
container and pinned cgroup (``RefusingTerminalVerifier``), and the operator
attestation that legacy controllers are uninstalled (``enroll_host``).

Writer exclusion is ONLY the shared validated engine admission flock. The
``HANDOFF_ENROLLED.json`` marker is advisory to new code; older installed
controllers do not read it and must be uninstalled before enrollment. SQL
triggers cannot verify cryptography: they stop old queue writers and accidental
SQL, not a direct SQLite writer, which must be excluded externally.

Every admission and every guarded effect needs a fresh single-use host nonce
answered by a Hub-signed statement of the CURRENT live claim and lease (read
from the real WorkQueue at signing time), so a delayed or replayed start is
refused.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid

from .development_ci import DevelopmentCIAttempt, QueuedDevelopmentCI, _canonical
from .repository import AGENTS, HubError
from .work_queue import SOFTWARE_VALIDATION_EXECUTOR, WorkTarget, WorkWorker

HANDOFF_MARKER = 'HANDOFF_ENROLLED.json'
ADMISSION_LOCK = 'admission.lock'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_HEX64 = re.compile(r'[a-f0-9]{64}')
_GENERATION = re.compile(r'[a-f0-9]{32}')
_DIGEST = re.compile(r'sha256:[a-f0-9]{64}')
_COMMIT = re.compile(r'[a-f0-9]{40}')


def receipt_digest(record):
    return 'sha256:' + hashlib.sha256(_canonical(record).encode()).hexdigest()


class HandoffRefused(RuntimeError):
    """Refusal with a fixed code; never carries caller or host text."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------
# External authorities: interfaces whose defaults refuse
# --------------------------------------------------------------------------

class RefusingAuthenticator:
    """No authenticated channel exists: sign nothing, verify nothing."""
    def sign(self, operation, payload):
        return None

    def verify(self, operation, payload, credential):
        return False


class RefusingExternalAnchor:
    """No external non-rollback fence store exists.

    The local JSON anchor lives beside the journal and can be rolled back with
    it, so it is only a local hint. A real implementation must be outside the
    host's restorable storage (e.g. a monotonic counter service).
    ``highest_fence(generation)`` returns the authoritative int or None;
    ``advance(generation, fence)`` returns True only once durably recorded.
    """
    def highest_fence(self, generation):
        return None

    def advance(self, generation, fence):
        return False


class RefusingTerminalVerifier:
    """No trusted re-inspection of the original container/cgroup is wired.

    ``verify(start_request, terminal)`` must independently re-inspect the exact
    original container and pinned cgroup; a runner's report is not proof.
    """
    def verify(self, start_request, terminal):
        return False


class RefusingClaimConfirmer:
    """No host->Hub channel exists to confirm the CURRENT claim per effect.

    ``confirm(challenge, credential)`` must deliver the host's fresh challenge
    to the Hub and return the Hub's signed ``(statement, credential)`` answer
    (``DurableDevelopmentCIHandoff.answer_challenge``), or None.
    """
    def confirm(self, challenge, credential):
        return None


@dataclass(frozen=True)
class LegacyControllersRemoved:
    """Operator attestation required by ``enroll_host``.

    Installed controllers older than this change do not read the enrollment
    marker. The operator must have uninstalled/disabled each of them; this
    object records that statement, it does not verify it.
    """
    engine_directory: str
    removed_controllers: tuple
    attested_by: str


@dataclass(frozen=True)
class OperatorReconciliation:
    """Explicit operator authorization bound to one journal and expected fence."""
    journal_id: str
    journal_device: int
    journal_inode: int
    expected_highest_fence: int


def _authority(value, default, methods, code):
    value = default() if value is None else value
    if any(not callable(getattr(value, name, None)) for name in methods):
        raise HandoffRefused(code)
    return value


def _verified(authenticator, operation, payload, credential):
    try:
        return credential is not None and authenticator.verify(operation, payload, credential) is True
    except Exception:
        return False


def _external_fence(anchor, generation):
    try:
        value = anchor.highest_fence(generation)
    except Exception:
        value = None
    if type(value) is not int or value < 0:
        raise HandoffRefused('handoff_external_anchor_unavailable')
    return value


def _plain(value):
    """Detached JSON copy; rejects non-JSON objects from any peer."""
    return json.loads(_canonical(value))


def _int(value, low=0, high=None):
    return type(value) is int and value >= low and (high is None or value <= high)


def _terminal(value):
    """Exact terminal shape: original container + pinned cgroup identity."""
    keys = {'container_id', 'cgroup', 'descendants_empty', 'terminal_verified', 'exit_code',
            'engine_id', 'image_id', 'timed_out', 'failed'}
    if type(value) is not dict or set(value) != keys:
        return None
    cgroup = value['cgroup']
    if (not isinstance(value['container_id'], str) or not _HEX64.fullmatch(value['container_id'])
            or type(cgroup) is not dict or set(cgroup) != {'path', 'device', 'inode'}
            or not isinstance(cgroup['path'], str) or not cgroup['path'].startswith('/')
            or not _int(cgroup['device']) or not _int(cgroup['inode'], 1)
            or value['descendants_empty'] is not True or value['terminal_verified'] is not True
            or not _int(value['exit_code'], 0, 255)
            or not isinstance(value['engine_id'], str) or not _ID.fullmatch(value['engine_id'])
            or not isinstance(value['image_id'], str) or not _DIGEST.fullmatch(value['image_id'])
            or type(value['timed_out']) is not bool or type(value['failed']) is not bool):
        return None
    return _plain(value)


_RELEASE_KEYS = {'attempt_id', 'generation', 'fence', 'receipt_digest', 'hub_ack', 'state'}


def _release(value):
    if (type(value) is not dict or set(value) != _RELEASE_KEYS or value['state'] != 'released'
            or not isinstance(value['attempt_id'], str) or not _ID.fullmatch(value['attempt_id'])
            or not isinstance(value['generation'], str) or not _GENERATION.fullmatch(value['generation'])
            or not _int(value['fence'], 1)
            or not isinstance(value['receipt_digest'], str) or not _DIGEST.fullmatch(value['receipt_digest'])
            or not isinstance(value['hub_ack'], str) or not _HEX64.fullmatch(value['hub_ack'])):
        raise HandoffRefused('handoff_request_invalid')
    return value


_CLAIM_KEYS = {'nonce', 'attempt_id', 'job_id', 'fence', 'generation', 'lease_until', 'state'}


def _start_request(value):
    keys = {'attempt_id', 'job_id', 'fence', 'generation', 'manifest_digest', 'source_commit', 'prior_release',
            'claim'}
    if (type(value) is not dict or set(value) != keys
            or not isinstance(value['attempt_id'], str) or not _ID.fullmatch(value['attempt_id'])
            or not isinstance(value['job_id'], str) or not _ID.fullmatch(value['job_id'])
            or not _int(value['fence'], 1)
            or not isinstance(value['generation'], str) or not _GENERATION.fullmatch(value['generation'])
            or not isinstance(value['manifest_digest'], str) or not _DIGEST.fullmatch(value['manifest_digest'])
            or not isinstance(value['source_commit'], str) or not _COMMIT.fullmatch(value['source_commit'])):
        raise HandoffRefused('handoff_request_invalid')
    claim = value['claim']
    if (type(claim) is not dict or set(claim) != {'statement', 'credential'}
            or type(claim['statement']) is not dict or set(claim['statement']) != _CLAIM_KEYS):
        raise HandoffRefused('handoff_request_invalid')
    prior = value['prior_release']
    if prior is not None:
        if type(prior) is not dict or set(prior) != {'release', 'credential'}:
            raise HandoffRefused('handoff_request_invalid')
        _release(prior['release'])
    return _plain(value)


def _finalize_request(value):
    keys = {'attempt_id', 'generation', 'fence', 'receipt_digest', 'hub_ack'}
    if (type(value) is not dict or set(value) != keys
            or not isinstance(value['attempt_id'], str) or not _ID.fullmatch(value['attempt_id'])
            or not isinstance(value['generation'], str) or not _GENERATION.fullmatch(value['generation'])
            or not _int(value['fence'], 1)
            or not isinstance(value['receipt_digest'], str) or not _DIGEST.fullmatch(value['receipt_digest'])
            or not isinstance(value['hub_ack'], str) or not _HEX64.fullmatch(value['hub_ack'])):
        raise HandoffRefused('handoff_request_invalid')
    return _plain(value)


def _statements(schema):
    return [statement.strip() for statement in schema.split(';\n') if statement.strip()]


def _reference_schema(schema, prelude=()):
    """Exact (type, name, table, sql) rows SQLite records for our objects."""
    db = sqlite3.connect(':memory:')
    try:
        for statement in prelude:
            db.execute(statement)
        before = {row[0] for row in db.execute('SELECT name FROM sqlite_master')}
        for statement in _statements(schema):
            db.execute(statement)
        return {tuple(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('table','trigger')")
            if row[1] not in before}
    finally:
        db.close()


def _schema_matches(db, expected):
    names = {row[1] for row in expected}
    tables = {row[1] for row in expected if row[0] == 'table'}
    actual = {tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('table','trigger')")}
    ours = {row for row in actual if row[1] in names}
    # Any extra trigger on our own tables could rewrite guarded rows.
    foreign = {row for row in actual if row[0] == 'trigger' and row[2] in tables and row[1] not in names}
    return ours == expected and not foreign


# --------------------------------------------------------------------------
# Shared validated engine admission lock
# --------------------------------------------------------------------------

@contextmanager
def engine_admission_lock(engine_directory):
    """The SAME lock file and checks as scripts/local_development_ci.private_lock
    and scripts/staging_ci_engine.admission_lock: private current-UID 0700
    engine directory, regular current-UID 0600 single-link inode whose name
    still resolves to the locked inode. Non-blocking; refusal on contention.
    """
    import fcntl  # Linux host only; there is deliberately no Windows fallback.
    engine = Path(engine_directory)
    parent = fd = None
    try:
        parent = os.open(engine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        directory = os.fstat(parent)
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) != 0o700):
            raise HandoffRefused('handoff_admission_lock_invalid')
        fd = os.open(ADMISSION_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise HandoffRefused('handoff_admission_lock_invalid')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise HandoffRefused('handoff_host_busy') from None
        named = os.stat(ADMISSION_LOCK, dir_fd=parent, follow_symlinks=False)
        current = engine.lstat()
        if ((named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
                or (current.st_dev, current.st_ino) != (directory.st_dev, directory.st_ino)):
            raise HandoffRefused('handoff_admission_lock_invalid')
        yield
    except HandoffRefused:
        raise
    except OSError:
        raise HandoffRefused('handoff_admission_lock_invalid') from None
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)


# --------------------------------------------------------------------------
# Host side
# --------------------------------------------------------------------------

_HOST_SCHEMA = '''
CREATE TABLE handoff_meta (
    id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL, floor_fence INTEGER NOT NULL);
CREATE TABLE handoff_attempts (
    attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, fence INTEGER NOT NULL UNIQUE,
    generation TEXT NOT NULL, manifest_digest TEXT NOT NULL, source_commit TEXT NOT NULL,
    state TEXT NOT NULL, receipt_id TEXT, receipt_digest TEXT, terminal TEXT, hub_ack TEXT,
    hub_release TEXT);
CREATE TRIGGER handoff_meta_guard BEFORE UPDATE ON handoff_meta
WHEN NEW.generation IS NOT OLD.generation OR NEW.floor_fence < OLD.floor_fence
BEGIN SELECT RAISE(ABORT,'handoff_meta_immutable'); END;
CREATE TRIGGER handoff_meta_delete BEFORE DELETE ON handoff_meta
BEGIN SELECT RAISE(ABORT,'handoff_meta_immutable'); END;
CREATE TRIGGER handoff_insert_guard BEFORE INSERT ON handoff_attempts
WHEN NEW.state IS NOT 'intent' OR NEW.receipt_id IS NOT NULL OR NEW.receipt_digest IS NOT NULL
  OR NEW.terminal IS NOT NULL OR NEW.hub_ack IS NOT NULL OR NEW.hub_release IS NOT NULL
  OR NEW.generation IS NOT (SELECT generation FROM handoff_meta WHERE id=1)
BEGIN SELECT RAISE(ABORT,'handoff_insert_refused'); END;
CREATE TRIGGER handoff_delete_guard BEFORE DELETE ON handoff_attempts
BEGIN SELECT RAISE(ABORT,'handoff_delete_refused'); END;
CREATE TRIGGER handoff_transition_guard BEFORE UPDATE ON handoff_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id OR NEW.job_id IS NOT OLD.job_id
  OR NEW.fence IS NOT OLD.fence OR NEW.generation IS NOT OLD.generation
  OR NEW.manifest_digest IS NOT OLD.manifest_digest OR NEW.source_commit IS NOT OLD.source_commit
  OR NOT (
    (OLD.state='intent' AND NEW.state='running' AND NEW.receipt_id IS NULL
      AND NEW.receipt_digest IS NULL AND NEW.terminal IS NULL AND NEW.hub_ack IS NULL
      AND NEW.hub_release IS NULL)
    OR (OLD.state='running' AND NEW.state='terminal_held' AND NEW.receipt_id IS NOT NULL
      AND NEW.receipt_digest IS NOT NULL AND NEW.terminal IS NOT NULL AND NEW.hub_ack IS NULL
      AND NEW.hub_release IS NULL)
    OR (OLD.state='terminal_held' AND NEW.state='finalized_reserved'
      AND NEW.receipt_id IS OLD.receipt_id AND NEW.receipt_digest IS OLD.receipt_digest
      AND NEW.terminal IS OLD.terminal AND NEW.hub_ack IS NOT NULL AND NEW.hub_release IS NULL
      AND length(NEW.hub_ack)=64 AND NEW.hub_ack NOT GLOB '*[^0-9a-f]*')
    OR (OLD.state='finalized_reserved' AND NEW.state='finalized_reserved'
      AND NEW.receipt_id IS OLD.receipt_id AND NEW.receipt_digest IS OLD.receipt_digest
      AND NEW.terminal IS OLD.terminal AND NEW.hub_ack IS OLD.hub_ack
      AND OLD.hub_release IS NULL AND NEW.hub_release IS NOT NULL))
BEGIN SELECT RAISE(ABORT,'handoff_transition_refused'); END;
'''
_HOST_EXPECTED = _reference_schema(_HOST_SCHEMA)


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path, value, *, exclusive=False):
    path = Path(path)
    raw = json.dumps(value, sort_keys=True).encode()
    target = path if exclusive else path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    if not exclusive:
        os.replace(target, path)
    _fsync_directory(path.parent)


def _read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise HandoffRefused('handoff_file_unavailable')
    value = json.loads(path.read_bytes())
    if type(value) is not dict:
        raise HandoffRefused('handoff_file_unavailable')
    return value


def enroll_host(journal_path, anchor_path, engine_directory, *, attestation=None,
                enable_durable_handoff=False):
    """Explicit operator enrollment; never called by import or a Hub message.

    Requires an operator attestation that older controllers (which do not read
    the marker) are uninstalled; takes the shared validated admission lock, so
    a currently running controller makes enrollment refuse; refuses retained
    HELD/STOPPING state.
    """
    if enable_durable_handoff is not True:
        raise HandoffRefused('handoff_disabled')
    journal, anchor, engine = Path(journal_path).absolute(), Path(anchor_path).absolute(), Path(engine_directory)
    if (type(attestation) is not LegacyControllersRemoved
            or attestation.engine_directory != str(engine.absolute())
            or type(attestation.removed_controllers) is not tuple or not attestation.removed_controllers
            or not all(isinstance(item, str) and item for item in attestation.removed_controllers)
            or not isinstance(attestation.attested_by, str) or not attestation.attested_by):
        raise HandoffRefused('handoff_legacy_attestation_required')
    if journal == anchor or os.path.lexists(journal) or os.path.lexists(anchor):
        raise HandoffRefused('handoff_already_enrolled')
    with engine_admission_lock(engine):
        if any(os.path.lexists(engine / name) for name in ('HELD.json', 'STOPPING.json', HANDOFF_MARKER)):
            raise HandoffRefused('handoff_engine_not_quiescent')
        generation = secrets.token_hex(16)
        _write_json_atomic(engine / HANDOFF_MARKER, {'schema': 'engineering-hub.ci-handoff.v1',
            'generation': generation, 'journal_id': generation, 'journal': str(journal),
            'anchor': str(anchor), 'legacy_controllers_removed': list(attestation.removed_controllers),
            'attested_by': attestation.attested_by}, exclusive=True)
        db = sqlite3.connect(journal, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            for statement in _statements(_HOST_SCHEMA):
                db.execute(statement)
            db.execute('INSERT INTO handoff_meta VALUES(1,?,0)', (generation,))
            db.execute('COMMIT')
        finally:
            db.close()
        _write_json_atomic(anchor, {'schema': 'engineering-hub.ci-handoff-anchor.v1',
                                    'generation': generation, 'highest_fence': 0}, exclusive=True)
        return generation


def reconcile_restored_journal(journal_path, anchor_path, engine_directory, confirmation, *,
                               external_anchor=None):
    """Offline operator reconciliation after a journal restore. Never automatic.

    Needs an ``OperatorReconciliation`` bound to this journal's id and inode and
    to the fence the external anchor reports; runs under the admission lock.
    It only raises the journal floor (and local hint) to that fence.
    """
    if type(confirmation) is not OperatorReconciliation or not _int(confirmation.expected_highest_fence):
        raise HandoffRefused('handoff_reconciliation_requires_operator')
    external = _authority(external_anchor, RefusingExternalAnchor, ('highest_fence', 'advance'),
                          'handoff_external_anchor_unavailable')
    journal = Path(journal_path)
    with engine_admission_lock(engine_directory):
        info = os.lstat(journal)
        if (not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != (confirmation.journal_device, confirmation.journal_inode)):
            raise HandoffRefused('handoff_reconciliation_mismatch')
        db = sqlite3.connect(journal.absolute().as_uri() + '?mode=rw', uri=True, isolation_level=None)
        try:
            if not _schema_matches(db, _HOST_EXPECTED):
                raise HandoffRefused('handoff_journal_schema_invalid')
            db.execute('BEGIN IMMEDIATE')
            try:
                generation, floor = db.execute('SELECT generation,floor_fence FROM handoff_meta WHERE id=1').fetchone()
                if (generation != confirmation.journal_id
                        or _external_fence(external, generation) != confirmation.expected_highest_fence):
                    raise HandoffRefused('handoff_reconciliation_mismatch')
                fence = max(floor, confirmation.expected_highest_fence)
                db.execute('UPDATE handoff_meta SET floor_fence=? WHERE id=1', (fence,))
                _write_json_atomic(anchor_path, {'schema': 'engineering-hub.ci-handoff-anchor.v1',
                                                 'generation': generation, 'highest_fence': fence})
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        finally:
            db.close()


class CIHostHandoff:
    """Host-side journal and contract for start / inspect / finalize.

    ``runner(start_request, guard)`` is the host executor; it must call
    ``guard()`` before each new effect. Its returned observation is NOT proof:
    the separately supplied ``terminal_verifier`` must confirm the original
    container and pinned cgroup, or the attempt stays ``running`` (held).
    """
    def __init__(self, journal_path, anchor_path, engine_directory, *, manifest_digest,
                 runner, authenticator=None, external_anchor=None, terminal_verifier=None,
                 claim_confirmer=None, clock=time.time, monotonic=time.monotonic, max_clock_skew=5.0,
                 nonce_validity=30.0, max_nonces=64, enable_durable_handoff=False):
        if enable_durable_handoff is not True:
            raise HandoffRefused('handoff_disabled')
        if not isinstance(manifest_digest, str) or not _DIGEST.fullmatch(manifest_digest) or not callable(runner):
            raise HandoffRefused('handoff_host_composition_required')
        self.journal, self.anchor = Path(journal_path).absolute(), Path(anchor_path).absolute()
        self.engine = Path(engine_directory)
        if not self.journal.is_file() or self.journal == self.anchor:
            raise HandoffRefused('handoff_not_enrolled')
        self.manifest_digest, self.runner = manifest_digest, runner
        self.authenticator = _authority(authenticator, RefusingAuthenticator, ('sign', 'verify'),
                                        'handoff_authenticator_required')
        self.external_anchor = _authority(external_anchor, RefusingExternalAnchor, ('highest_fence', 'advance'),
                                          'handoff_external_anchor_unavailable')
        self.terminal_verifier = _authority(terminal_verifier, RefusingTerminalVerifier, ('verify',),
                                            'handoff_terminal_verifier_required')
        self.claim_confirmer = _authority(claim_confirmer, RefusingClaimConfirmer, ('confirm',),
                                          'handoff_claim_confirmer_required')
        if (not callable(clock) or not callable(monotonic)
                or type(max_clock_skew) not in (int, float) or not math.isfinite(max_clock_skew)
                or not 0 <= max_clock_skew <= 60
                or type(nonce_validity) not in (int, float) or not math.isfinite(nonce_validity)
                or not 0 < nonce_validity <= 120
                or type(max_nonces) is not int or not 1 <= max_nonces <= 1024):
            raise HandoffRefused('handoff_host_composition_required')
        self.clock, self.monotonic = clock, monotonic
        self.max_clock_skew, self.nonce_validity, self.max_nonces = max_clock_skew, nonce_validity, max_nonces
        # Process-local and single-use: a host restart forgets every nonce, so
        # an in-flight answer is refused rather than trusted.
        self._nonces = {}
        self._nonce_lock = threading.Lock()
        self._local = threading.Lock()
        with self._db(write=False):
            pass  # validates exact schema on open

    @contextmanager
    def _db(self, *, write):
        db = sqlite3.connect(self.journal.as_uri() + '?mode=' + ('rw' if write else 'ro'), uri=True,
                             isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if not _schema_matches(db, _HOST_EXPECTED):
                raise HandoffRefused('handoff_journal_schema_invalid')
            if write:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                try:
                    yield db
                    db.execute('COMMIT')
                except BaseException:
                    db.execute('ROLLBACK')
                    raise
            else:
                yield db
        except sqlite3.DatabaseError:
            raise HandoffRefused('handoff_journal_unavailable') from None
        finally:
            db.close()

    def _generation(self, db):
        row = db.execute('SELECT generation,floor_fence FROM handoff_meta WHERE id=1').fetchone()
        if row is None:
            raise HandoffRefused('handoff_not_enrolled')
        return row['generation'], row['floor_fence']

    def _enrolled(self, generation):
        """Marker must name exactly this journal/anchor (advisory for new code)."""
        try:
            marker = _read_json(self.engine / HANDOFF_MARKER)
        except Exception:
            raise HandoffRefused('handoff_enrollment_marker_invalid') from None
        if (marker.get('generation') != generation or marker.get('journal_id') != generation
                or marker.get('journal') != str(self.journal) or marker.get('anchor') != str(self.anchor)):
            raise HandoffRefused('handoff_enrollment_marker_invalid')

    def _local_anchor(self, generation):
        try:
            anchor = _read_json(self.anchor)
        except Exception:
            raise HandoffRefused('handoff_anchor_unknown') from None
        if anchor.get('generation') != generation or not _int(anchor.get('highest_fence')):
            raise HandoffRefused('handoff_anchor_unknown')
        return anchor['highest_fence']

    def _verify(self, operation, payload, credential):
        if not _verified(self.authenticator, operation, payload, credential):
            raise HandoffRefused('handoff_unauthenticated')

    def _signed(self, operation, payload):
        payload = _plain(payload)
        return payload, self.authenticator.sign(operation, payload)

    def _row_record(self, row):
        return {'attempt_id': row['attempt_id'], 'job_id': row['job_id'], 'fence': row['fence'],
                'generation': row['generation'], 'manifest_digest': row['manifest_digest'],
                'source_commit': row['source_commit']}

    # -- fresh current-claim challenge -------------------------------------
    @staticmethod
    def _finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    def _now(self):
        value = self.clock()
        if not self._finite(value):
            raise HandoffRefused('handoff_clock_unavailable')
        return value

    def _tick(self):
        value = self.monotonic()
        if not self._finite(value):
            raise HandoffRefused('handoff_clock_unavailable')
        return value

    def _issue(self, binding):
        with self._db(write=False) as db:
            generation, _ = self._generation(db)
        nonce = secrets.token_hex(32)
        challenge = {**binding, 'generation': generation, 'nonce': nonce}
        with self._nonce_lock:
            tick = self._tick()
            for key in [key for key, (_, deadline) in self._nonces.items() if tick > deadline]:
                del self._nonces[key]  # prune expired only; never evict a live nonce
            if len(self._nonces) >= self.max_nonces:
                raise HandoffRefused('handoff_nonce_store_full')
            self._nonces[nonce] = (challenge, tick + self.nonce_validity)
        return self._signed('challenge_result', challenge)

    def _discard(self, nonce):
        with self._nonce_lock:
            self._nonces.pop(nonce, None)

    def challenge(self, request, credential):
        """Issue a fresh single-use nonce for one (attempt, job, fence)."""
        if (type(request) is not dict or set(request) != {'attempt_id', 'job_id', 'fence'}
                or not isinstance(request['attempt_id'], str) or not _ID.fullmatch(request['attempt_id'])
                or not isinstance(request['job_id'], str) or not _ID.fullmatch(request['job_id'])
                or not _int(request['fence'], 1)):
            raise HandoffRefused('handoff_request_invalid')
        self._verify('challenge', request, credential)
        return self._issue(_plain(request))

    def _check_claim(self, claim, binding, generation):
        """Hub's signed answer: fresh nonce, exact binding, live lease."""
        statement = claim.get('statement') if type(claim) is dict else None
        if type(statement) is not dict or not _verified(self.authenticator, 'claim', statement,
                                                        claim.get('credential')):
            raise HandoffRefused('handoff_claim_unauthenticated')
        with self._nonce_lock:
            entry = self._nonces.pop(statement.get('nonce'), None)  # single use, even on refusal
        if entry is None:
            raise HandoffRefused('handoff_claim_nonce_unknown')
        issued, deadline = entry
        if self._tick() > deadline:
            raise HandoffRefused('handoff_claim_nonce_stale')
        now = self._now()
        expected = {**binding, 'generation': generation}
        if (any(issued.get(key) != value for key, value in expected.items())
                or any(statement.get(key) != value for key, value in expected.items())
                or statement.get('state') != 'claim_current'):
            raise HandoffRefused('handoff_claim_mismatch')
        lease = statement.get('lease_until')
        if not self._finite(lease) or lease - self.max_clock_skew <= now:
            raise HandoffRefused('handoff_claim_lease_expired')

    def _confirm_current(self, binding, generation):
        challenge, credential = self._issue(binding)
        try:
            try:
                answer = self.claim_confirmer.confirm(challenge, credential)
            except Exception:
                answer = None
            if type(answer) is not tuple or len(answer) != 2:
                raise HandoffRefused('handoff_claim_unconfirmed')
            try:
                statement = _plain(answer[0])
            except Exception:
                raise HandoffRefused('handoff_claim_unauthenticated') from None
            self._check_claim({'statement': statement, 'credential': answer[1]}, binding, generation)
        finally:
            self._discard(challenge['nonce'])  # single use whether confirmed or not

    def start(self, request, credential):
        """Admit at most one new attempt; any doubt refuses before an effect."""
        request = _start_request(request)
        self._verify('start', request, credential)
        if not self._local.acquire(blocking=False):
            raise HandoffRefused('handoff_host_busy')
        try:
            with engine_admission_lock(self.engine):
                return self._start_locked(request)
        finally:
            self._local.release()

    def _prior_released(self, db, rows, floor, prior):
        """Host-verifiable evidence the Hub released the host's latest attempt."""
        latest = max(rows, key=lambda row: row['fence'], default=None)
        if latest is None and floor == 0:
            if prior is not None:
                raise HandoffRefused('handoff_prior_release_mismatch')
            return
        if prior is None or not _verified(self.authenticator, 'release', prior['release'], prior['credential']):
            raise HandoffRefused('handoff_prior_release_required')
        release = prior['release']
        if latest is not None and latest['fence'] >= floor:
            expected = {'attempt_id': latest['attempt_id'], 'generation': latest['generation'],
                        'fence': latest['fence'], 'receipt_digest': latest['receipt_digest'],
                        'hub_ack': latest['hub_ack'], 'state': 'released'}
            if release != expected or latest['hub_release'] is not None:
                raise HandoffRefused('handoff_prior_release_mismatch')
            if any(row['hub_release'] is None for row in rows if row['attempt_id'] != latest['attempt_id']):
                raise HandoffRefused('handoff_prior_release_mismatch')
            db.execute('UPDATE handoff_attempts SET hub_release=? WHERE attempt_id=? '
                       "AND state='finalized_reserved' AND hub_release IS NULL",
                       (_canonical(prior), latest['attempt_id']))
        elif release['fence'] != floor:
            # After an operator floor reconcile only a release of that fence counts.
            raise HandoffRefused('handoff_prior_release_mismatch')

    def _start_locked(self, request):
        binding = {key: request[key] for key in ('attempt_id', 'job_id', 'fence')}
        with self._db(write=False) as db:
            generation, _ = self._generation(db)
        # Fresh proof that this is the Hub's CURRENT live claim, checked first:
        # a delayed or replayed signed start is refused before anything else.
        self._check_claim(request['claim'], binding, generation)
        with self._db(write=True) as db:
            generation, floor = self._generation(db)
            self._enrolled(generation)
            local = self._local_anchor(generation)
            external = _external_fence(self.external_anchor, generation)
            rows = db.execute('SELECT * FROM handoff_attempts').fetchall()
            journal_high = max([row['fence'] for row in rows] + [floor])
            if journal_high < max(local, external):
                raise HandoffRefused('handoff_journal_restored')
            if request['generation'] != generation or request['manifest_digest'] != self.manifest_digest:
                raise HandoffRefused('handoff_binding_mismatch')
            if request['fence'] <= max(journal_high, local, external):
                raise HandoffRefused('handoff_fence_stale')
            if any(row['state'] != 'finalized_reserved' for row in rows):
                raise HandoffRefused('handoff_slot_held')
            self._prior_released(db, rows, floor, request['prior_release'])
            db.execute("INSERT INTO handoff_attempts VALUES(?,?,?,?,?,?,'intent',NULL,NULL,NULL,NULL,NULL)",
                       (request['attempt_id'], request['job_id'], request['fence'], generation,
                        request['manifest_digest'], request['source_commit']))
        # Journal first, then the external anchor and the local hint. A failed
        # advance leaves this attempt at 'intent': held, never run or replayed.
        try:
            advanced = self.external_anchor.advance(generation, request['fence']) is True
        except Exception:
            advanced = False
        if not advanced:
            raise HandoffRefused('handoff_external_anchor_unavailable')
        _write_json_atomic(self.anchor, {'schema': 'engineering-hub.ci-handoff-anchor.v1',
                                         'generation': generation, 'highest_fence': request['fence']})
        with self._db(write=True) as db:
            db.execute("UPDATE handoff_attempts SET state='running' WHERE attempt_id=? AND state='intent'",
                       (request['attempt_id'],))
        attempt_id = request['attempt_id']

        def guard():
            with self._db(write=False) as db:
                row = db.execute('SELECT state FROM handoff_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
                current, _ = self._generation(db)
            self._enrolled(current)
            if row is None or row['state'] != 'running' or current != generation:
                raise HandoffRefused('handoff_admission_lost')
            # Before every effect: a fresh Hub answer that the claim is current.
            self._confirm_current(binding, generation)

        # Any runner failure or unverified observation leaves the row 'running'.
        terminal = _terminal(_plain(self.runner(dict(request), guard)))
        try:
            proven = terminal is not None and self.terminal_verifier.verify(dict(request), dict(terminal)) is True
        except Exception:
            proven = False
        if not proven:
            raise HandoffRefused('handoff_terminal_unproven')
        record = {**{key: request[key] for key in ('attempt_id', 'job_id', 'fence', 'manifest_digest',
                                                   'source_commit')},
                  'generation': generation, 'terminal': terminal}
        digest = receipt_digest(record)
        receipt = 'receipt-' + uuid.uuid4().hex
        with self._db(write=True) as db:
            changed = db.execute("UPDATE handoff_attempts SET state='terminal_held',receipt_id=?,receipt_digest=?,"
                                 "terminal=? WHERE attempt_id=? AND state='running'",
                                 (receipt, digest, _canonical(terminal), attempt_id)).rowcount
            if changed != 1:
                raise HandoffRefused('handoff_admission_lost')
        return self._signed('start_result', {**record, 'state': 'terminal_held',
                                             'receipt_id': receipt, 'receipt_digest': digest})

    def inspect(self, request, credential):
        """Read-only journal inspection of the original attempt."""
        if type(request) is not dict or set(request) != {'attempt_id'} or not isinstance(
                request['attempt_id'], str) or not _ID.fullmatch(request['attempt_id']):
            raise HandoffRefused('handoff_request_invalid')
        self._verify('inspect', request, credential)
        with self._db(write=False) as db:
            row = db.execute('SELECT * FROM handoff_attempts WHERE attempt_id=?', (request['attempt_id'],)).fetchone()
        if row is None:
            return self._signed('inspect_result', {'attempt_id': request['attempt_id'], 'state': 'unknown'})
        result = {**self._row_record(row), 'state': row['state'], 'receipt_id': row['receipt_id'],
                  'receipt_digest': row['receipt_digest'],
                  'terminal': None if row['terminal'] is None else json.loads(row['terminal'])}
        return self._signed('inspect_result', result)

    def finalize(self, request, credential):
        """CAS terminal_held -> finalized_reserved for exactly this tuple.

        Identical repeat is idempotent; any mismatch refuses with no change.
        """
        request = _finalize_request(request)
        self._verify('finalize', request, credential)
        with self._db(write=True) as db:
            generation, _ = self._generation(db)
            self._enrolled(generation)
            row = db.execute('SELECT * FROM handoff_attempts WHERE attempt_id=?', (request['attempt_id'],)).fetchone()
            if (row is None or request['generation'] != generation or row['generation'] != generation
                    or row['fence'] != request['fence'] or row['receipt_digest'] != request['receipt_digest']):
                raise HandoffRefused('handoff_finalize_mismatch')
            if row['state'] == 'finalized_reserved':
                if row['hub_ack'] != request['hub_ack']:
                    raise HandoffRefused('handoff_finalize_mismatch')
            elif row['state'] == 'terminal_held':
                changed = db.execute("UPDATE handoff_attempts SET state='finalized_reserved',hub_ack=? "
                                     "WHERE attempt_id=? AND state='terminal_held' AND fence=? AND receipt_digest=?",
                                     (request['hub_ack'], request['attempt_id'], request['fence'],
                                      request['receipt_digest'])).rowcount
                if changed != 1:
                    raise HandoffRefused('handoff_finalize_mismatch')
            else:
                raise HandoffRefused('handoff_finalize_mismatch')
        return self._signed('finalize_result', {**request, 'state': 'finalized_reserved'})


# --------------------------------------------------------------------------
# Hub side
# --------------------------------------------------------------------------

_HUB_SCHEMA = '''
CREATE TABLE IF NOT EXISTS hub_development_ci_handoff_policy (
    id INTEGER PRIMARY KEY CHECK(id=1), action_key TEXT NOT NULL, machine_id TEXT NOT NULL,
    manifest_digest TEXT NOT NULL, host_generation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hub_development_ci_handoff (
    job_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE, attempt TEXT NOT NULL,
    fence INTEGER NOT NULL, epoch TEXT NOT NULL, host_generation TEXT NOT NULL,
    manifest_digest TEXT NOT NULL, state TEXT NOT NULL, observed TEXT, receipt_id TEXT,
    receipt_digest TEXT, terminal TEXT, outcome TEXT, hub_ack TEXT, host_ack TEXT);
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_policy_guard BEFORE UPDATE ON hub_development_ci_handoff_policy
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_policy'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_policy_delete BEFORE DELETE ON hub_development_ci_handoff_policy
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_policy'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_insert BEFORE INSERT ON hub_development_ci_handoff
WHEN NEW.state IS NOT 'intent' OR NEW.observed IS NOT NULL OR NEW.receipt_id IS NOT NULL
  OR NEW.receipt_digest IS NOT NULL OR NEW.terminal IS NOT NULL OR NEW.outcome IS NOT NULL
  OR NEW.hub_ack IS NOT NULL OR NEW.host_ack IS NOT NULL
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_insert'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_delete BEFORE DELETE ON hub_development_ci_handoff
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_delete'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_transition BEFORE UPDATE ON hub_development_ci_handoff
WHEN NEW.job_id IS NOT OLD.job_id OR NEW.attempt_id IS NOT OLD.attempt_id OR NEW.attempt IS NOT OLD.attempt
  OR NEW.fence IS NOT OLD.fence OR NEW.epoch IS NOT OLD.epoch OR NEW.host_generation IS NOT OLD.host_generation
  OR NEW.manifest_digest IS NOT OLD.manifest_digest
  OR NOT (
    (OLD.state='intent' AND NEW.state='observed' AND NEW.observed IS NOT NULL AND NEW.receipt_id IS NULL
      AND NEW.receipt_digest IS NULL AND NEW.terminal IS NULL AND NEW.outcome IS NULL
      AND NEW.hub_ack IS NULL AND NEW.host_ack IS NULL)
    OR (OLD.state IN ('intent','observed') AND NEW.state='receipt_committed' AND NEW.observed IS OLD.observed
      AND NEW.receipt_id IS NOT NULL AND NEW.receipt_digest IS NOT NULL AND NEW.terminal IS NOT NULL
      AND NEW.outcome IN ('succeeded','failed') AND length(NEW.hub_ack)=64 AND NEW.host_ack IS NULL)
    OR (OLD.state='receipt_committed' AND NEW.state='host_acked' AND NEW.observed IS OLD.observed
      AND NEW.receipt_id IS OLD.receipt_id AND NEW.receipt_digest IS OLD.receipt_digest
      AND NEW.terminal IS OLD.terminal AND NEW.outcome IS OLD.outcome AND NEW.hub_ack IS OLD.hub_ack
      AND NEW.host_ack IS NOT NULL)
    OR (OLD.state='host_acked' AND NEW.state='finalized' AND NEW.observed IS OLD.observed
      AND NEW.receipt_id IS OLD.receipt_id AND NEW.receipt_digest IS OLD.receipt_digest
      AND NEW.terminal IS OLD.terminal AND NEW.outcome IS OLD.outcome AND NEW.hub_ack IS OLD.hub_ack
      AND NEW.host_ack IS OLD.host_ack
      AND (SELECT state FROM hub_work_jobs WHERE id=OLD.job_id) IS OLD.outcome))
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_transition'); END;
CREATE TABLE IF NOT EXISTS hub_development_ci_effects (
    nonce TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL, fence INTEGER NOT NULL,
    state TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_insert BEFORE INSERT ON hub_development_ci_effects
WHEN NEW.state IS NOT 'in_flight'
  OR NOT EXISTS(SELECT 1 FROM hub_development_ci_handoff WHERE job_id=NEW.job_id
                AND attempt_id=NEW.attempt_id AND fence=NEW.fence AND state='intent')
BEGIN SELECT RAISE(ABORT,'development_ci_effect_insert'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_delete BEFORE DELETE ON hub_development_ci_effects
BEGIN SELECT RAISE(ABORT,'development_ci_effect_delete'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_transition BEFORE UPDATE ON hub_development_ci_effects
WHEN NEW.nonce IS NOT OLD.nonce OR NEW.job_id IS NOT OLD.job_id OR NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.fence IS NOT OLD.fence
  OR NOT (OLD.state='in_flight' AND NEW.state='reported'
          AND (SELECT state FROM hub_development_ci_handoff WHERE job_id=OLD.job_id) IN
              ('receipt_committed','host_acked','finalized'))
BEGIN SELECT RAISE(ABORT,'development_ci_effect_transition'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_release_guard BEFORE UPDATE OF state ON hub_work_jobs
WHEN NEW.state NOT IN ('claimed','held')
  AND EXISTS(SELECT 1 FROM hub_development_ci_effects WHERE job_id=NEW.id AND state='in_flight')
BEGIN SELECT RAISE(ABORT,'development_ci_effect_in_flight'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_job_identity BEFORE UPDATE ON hub_work_jobs
WHEN EXISTS(SELECT 1 FROM hub_development_ci_effects WHERE job_id=OLD.id AND state='in_flight') AND (NEW.sequence IS NOT OLD.sequence
  OR NEW.id IS NOT OLD.id
  OR NEW.actor IS NOT OLD.actor
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.fingerprint IS NOT OLD.fingerprint
  OR NEW.action_key IS NOT OLD.action_key
  OR NEW.environment IS NOT OLD.environment
  OR NEW.machine_id IS NOT OLD.machine_id
  OR NEW.project IS NOT OLD.project
  OR NEW.approval_id IS NOT OLD.approval_id
  OR NEW.target IS NOT OLD.target
  OR NEW.requires_execution_proof IS NOT OLD.requires_execution_proof
  OR NEW.fence IS NOT OLD.fence
  OR NEW.worker IS NOT OLD.worker
  OR NEW.created_at IS NOT OLD.created_at)
BEGIN SELECT RAISE(ABORT,'development_ci_effect_in_flight'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_job_delete BEFORE DELETE ON hub_work_jobs
WHEN EXISTS(SELECT 1 FROM hub_development_ci_effects WHERE job_id=OLD.id AND state='in_flight')
BEGIN SELECT RAISE(ABORT,'development_ci_effect_in_flight'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_machine_update BEFORE UPDATE ON hub_work_machines
WHEN OLD.job_id IS NOT NULL AND EXISTS(SELECT 1 FROM hub_development_ci_effects WHERE job_id=OLD.job_id AND state='in_flight')
  AND (NEW.job_id IS NOT OLD.job_id OR NEW.generation IS NOT OLD.generation OR NEW.machine_id IS NOT OLD.machine_id)
BEGIN SELECT RAISE(ABORT,'development_ci_effect_in_flight'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_machine_insert BEFORE INSERT ON hub_work_machines
WHEN EXISTS(SELECT 1 FROM hub_work_machines m JOIN hub_development_ci_effects e ON e.job_id=m.job_id
            WHERE m.machine_id=NEW.machine_id AND e.state='in_flight')
BEGIN SELECT RAISE(ABORT,'development_ci_effect_in_flight'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_job_insert BEFORE INSERT ON hub_work_jobs
WHEN EXISTS(SELECT 1 FROM hub_work_jobs j JOIN hub_development_ci_effects e ON e.job_id=j.id
            WHERE e.state='in_flight' AND (j.id=NEW.id OR j.sequence=NEW.sequence
                  OR (j.actor=NEW.actor AND j.request_id=NEW.request_id)))
BEGIN SELECT RAISE(ABORT,'development_ci_effect_in_flight'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_effect_machine_delete BEFORE DELETE ON hub_work_machines
WHEN OLD.job_id IS NOT NULL AND EXISTS(SELECT 1 FROM hub_development_ci_effects WHERE job_id=OLD.job_id AND state='in_flight')
BEGIN SELECT RAISE(ABORT,'development_ci_effect_in_flight'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_release_guard BEFORE UPDATE OF state ON hub_work_jobs
WHEN NEW.state IN ('succeeded','failed')
  AND EXISTS(SELECT 1 FROM hub_development_ci_handoff WHERE job_id=NEW.id)
  AND NOT EXISTS(SELECT 1 FROM hub_development_ci_handoff WHERE job_id=NEW.id
                 AND state='host_acked' AND outcome=NEW.state AND host_ack IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_unfinalized'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_release_record AFTER UPDATE OF state ON hub_work_jobs
WHEN NEW.state IN ('succeeded','failed')
BEGIN UPDATE hub_development_ci_handoff SET state='finalized'
      WHERE job_id=NEW.id AND state='host_acked' AND outcome=NEW.state; END;
'''
_HUB_EXPECTED = _reference_schema(_HUB_SCHEMA, (
    'CREATE TABLE hub_work_jobs (sequence INTEGER, id TEXT, actor TEXT, request_id TEXT, fingerprint TEXT, '
    'action_key TEXT, environment TEXT, machine_id TEXT, project TEXT, approval_id TEXT, target TEXT, '
    'requires_execution_proof INTEGER, state TEXT, revision INTEGER, worker TEXT, fence INTEGER, '
    'lease_until REAL, created_at REAL)',
    'CREATE TABLE hub_work_machines (machine_id TEXT, generation INTEGER, job_id TEXT)'))


class _UnusedProvider:
    """Satisfies the legacy composition; the handoff never invokes it."""
    async def invoke(self, attempt, admission_guard):
        raise HubError('development_ci_handoff_provider_unused', 409)

    async def verify_terminal(self, attempt, receipt_id):
        raise HubError('development_ci_handoff_provider_unused', 409)


class DurableDevelopmentCIHandoff(QueuedDevelopmentCI):
    """Hub side of the durable handoff over the REAL owned WorkQueue/PilotOwner.

    ``channel`` is an explicit transport with async ``start``, ``inspect`` and
    ``finalize`` methods taking ``(payload, credential)`` and returning
    ``(payload, credential)``. No such transport exists in this repository.
    The Hub's only terminal proof is the authenticated host inspection of the
    original attempt, which the host records only after its own terminal
    verifier re-inspected the original container/cgroup.
    """
    _handoff_mode = True

    def __init__(self, queue, owner, manifest, channel, *, action_key, host_generation,
                 authenticator=None, enable_durable_handoff=False, renew_interval=120,
                 monotonic=time.monotonic):
        if enable_durable_handoff is not True:
            raise HubError('development_ci_handoff_disabled', 409)
        if (not isinstance(host_generation, str) or not _GENERATION.fullmatch(host_generation)
                or any(not inspect.iscoroutinefunction(getattr(channel, name, None))
                       for name in ('challenge', 'start', 'inspect', 'finalize'))):
            raise HubError('development_ci_handoff_composition_required', 409)
        try:
            self.authenticator = _authority(authenticator, RefusingAuthenticator, ('sign', 'verify'),
                                            'handoff_authenticator_required')
        except HandoffRefused:
            raise HubError('development_ci_handoff_composition_required', 409) from None
        self.channel, self.host_generation = channel, host_generation
        super().__init__(queue, owner, manifest, _UnusedProvider(), action_key=action_key,
                         renew_interval=renew_interval, monotonic=monotonic)
        try:
            with self.repo._transaction() as db:
                enrolled = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                      "AND name='hub_development_ci_handoff_policy'").fetchone()
                if enrolled is None:
                    for statement in _statements(_HUB_SCHEMA):
                        db.execute(statement)
                # Never silently recreate a dropped/altered guard once enrolled.
                if not _schema_matches(db, _HUB_EXPECTED):
                    raise HubError('development_ci_handoff_schema_invalid', 409)
                expected = (action_key, manifest.machine_id, manifest.digest, host_generation)
                old = db.execute('SELECT action_key,machine_id,manifest_digest,host_generation '
                                 'FROM hub_development_ci_handoff_policy WHERE id=1').fetchone()
                if old is not None and tuple(old) != expected:
                    raise HubError('development_ci_handoff_enrollment_changed', 409)
                db.execute('INSERT OR IGNORE INTO hub_development_ci_handoff_policy VALUES(1,?,?,?,?)', expected)
        except BaseException:
            # Undo the parent's in-memory composition; nothing stays installed.
            queue.target_verifier, queue.claim_verifier = self._target, self._claim
            queue.completion_verifier = None
            self.repo._development_ci = None
            raise
        self._reconciliation = self.reconciliation_verifier
        queue.reconciliation_verifier = self._reconciliation

    def _guard(self):
        super()._guard()
        with self.repo._connection() as db:
            if not _schema_matches(db, _HUB_EXPECTED):
                raise HubError('development_ci_handoff_schema_invalid', 409)

    # -- transport --------------------------------------------------------
    def _sign(self, operation, payload):
        try:
            credential = self.authenticator.sign(operation, payload)
        except Exception:
            credential = None
        if credential is None:
            # Missing authentication: never contact the host unauthenticated.
            raise HubError('development_ci_handoff_unauthenticated', 409)
        return credential

    async def _call(self, operation, payload):
        payload = _plain(payload)
        credential = self._sign(operation, payload)
        response, response_credential = await getattr(self.channel, operation)(payload, credential)
        response = _plain(response)
        if not _verified(self.authenticator, operation + '_result', response, response_credential):
            raise HubError('development_ci_handoff_unauthenticated', 409)
        return response, response_credential

    # -- Hub state helpers --------------------------------------------------
    def _row(self, job_id):
        with self.repo._connection() as db:
            return db.execute('SELECT * FROM hub_development_ci_handoff WHERE job_id=?', (job_id,)).fetchone()

    def _pending(self):
        with self.repo._connection() as db:
            row = db.execute("SELECT h.job_id FROM hub_development_ci_handoff h JOIN hub_work_jobs j ON j.id=h.job_id "
                             "WHERE h.state!='finalized' AND j.state IN ('claimed','held') "
                             "AND j.machine_id=? ORDER BY h.fence LIMIT 1", (self.manifest.machine_id,)).fetchone()
        return None if row is None else row['job_id']

    def answer_challenge(self, challenge, credential):
        """Sign that the challenged attempt is the CURRENT live queue claim.

        Reads the actual WorkQueue (``live_claim`` via the attempt binding) at
        signing time; refuses for any lost, expired, reassigned or paused claim.
        """
        challenge = _plain(challenge)
        if (not _verified(self.authenticator, 'challenge_result', challenge, credential)
                or type(challenge) is not dict
                or set(challenge) != {'attempt_id', 'job_id', 'fence', 'generation', 'nonce'}
                or challenge['generation'] != self.host_generation):
            raise HubError('development_ci_handoff_challenge_invalid', 409)
        row = self._row(challenge['job_id'])
        if row is None or row['attempt_id'] != challenge['attempt_id'] or row['fence'] != challenge['fence']:
            raise HubError('development_ci_handoff_challenge_invalid', 409)
        attempt = DevelopmentCIAttempt(**json.loads(row['attempt']))
        if row['state'] != 'intent' or attempt.epoch != self.epoch:
            raise HubError('development_ci_handoff_claim_not_current', 409)
        self._bound(attempt)  # live_claim on the real queue, review, binding
        # Same txn: re-check the live claim and pause, then durably mark this
        # effect in flight. Until the host reports terminal (receipt_committed)
        # the ledger refuses any release/reassignment of the job; a later
        # pause/revoke only stops NEW signatures.
        with self.repo._transaction() as db:
            job = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (attempt.job_id,)).fetchone()
            machine = db.execute('SELECT generation,job_id FROM hub_work_machines WHERE machine_id=?',
                                 (self.manifest.machine_id,)).fetchone()
            now = self.queue._now()
            if (job is None or job['state'] != 'claimed' or job['worker'] != self.worker.encoded()
                    or job['fence'] != attempt.fence or not job['lease_until'] > now
                    or machine is None or tuple(machine) != (attempt.fence, attempt.job_id)
                    or db.execute('SELECT paused FROM hub_work_policy WHERE id=1').fetchone()[0]
                    or self.repo._paused(db)
                    or not self._binding_in_txn(db, attempt, job)):
                raise HubError('development_ci_handoff_claim_not_current', 409)
            db.execute("INSERT INTO hub_development_ci_effects VALUES(?,?,?,?,'in_flight')",
                       (challenge['nonce'], attempt.job_id, attempt.attempt_id, attempt.fence))
            lease_until = job['lease_until']
        statement = {'nonce': challenge['nonce'], 'attempt_id': attempt.attempt_id, 'job_id': attempt.job_id,
                     'fence': attempt.fence, 'generation': self.host_generation,
                     'lease_until': lease_until, 'state': 'claim_current'}
        return statement, self._sign('claim', statement)

    def _binding_in_txn(self, db, attempt, job):
        """The full _bound binding, re-read INSIDE the signing transaction.

        BEGIN IMMEDIATE holds the write lock, so the committed task, review,
        policy and attempt rows read here cannot change before the mark and
        signature are recorded. Every read uses THIS connection and no
        operator callback runs here (callbacks run in the _bound precheck), so
        nothing can wait on the lock this transaction holds.
        """
        try:
            if self._composition_changed() or not _schema_matches(db, _HUB_EXPECTED):
                return False
            task = self.repo._get(db, attempt.task_id, 'task')
            review = task.get('review')
            target = WorkTarget(**json.loads(job['target']))
            saved = db.execute('SELECT attempt FROM hub_development_ci_attempts WHERE job_id=?',
                               (attempt.job_id,)).fetchone()
            handoff = db.execute('SELECT attempt,state,fence FROM hub_development_ci_handoff WHERE job_id=?',
                                 (attempt.job_id,)).fetchone()
            policy = db.execute('SELECT action_key,machine_id,manifest_digest FROM hub_development_ci_policy '
                                'WHERE id=1').fetchone()
            enrolled = db.execute('SELECT action_key,machine_id,manifest_digest,host_generation '
                                  'FROM hub_development_ci_handoff_policy WHERE id=1').fetchone()
            action = self.queue.actions.get(self.action_key)
            original = _canonical(asdict(attempt))
            return (task['project'] == 'engineering-hub' and task['owner'] == attempt.actor
                    and task['status'] == 'verified' and task['candidate_commit'] == attempt.source_commit
                    and task['revision'] == attempt.task_revision
                    and type(review) is dict and review.get('actor') in AGENTS
                    and review.get('actor') != attempt.actor and review.get('verdict') == 'approve'
                    and review.get('commit') == attempt.source_commit and _canonical(review) == attempt.review
                    and target == WorkTarget(attempt.task_id, attempt.source_commit, attempt.manifest_digest)
                    and target.artifact_digest == self.manifest.digest == attempt.manifest_digest
                    and job['actor'] == attempt.actor and job['action_key'] == self.action_key
                    and job['environment'] == 'staging' and job['machine_id'] == self.manifest.machine_id
                    and bool(job['requires_execution_proof']) is True
                    and saved is not None and saved['attempt'] == original
                    and handoff is not None and handoff['attempt'] == original
                    and handoff['state'] == 'intent' and handoff['fence'] == attempt.fence
                    and policy is not None
                    and tuple(policy) == (self.action_key, self.manifest.machine_id, self.manifest.digest)
                    and enrolled is not None
                    and tuple(enrolled) == (self.action_key, self.manifest.machine_id, self.manifest.digest,
                                            self.host_generation)
                    and action is not None and action.machine_id == self.manifest.machine_id
                    and action.environments == frozenset({'staging'}) and action.requires_execution_proof
                    and not self.queue._shared_policy_present()
                    and self.repo.task_allows_environment(attempt.task_id, 'staging', db=db) is True)
        except Exception:
            return False

    def _prior_release(self):
        """Hub-signed release of the latest attempt whose queue job was released."""
        with self.repo._connection() as db:
            row = db.execute("SELECT h.* FROM hub_development_ci_handoff h JOIN hub_work_jobs j ON j.id=h.job_id "
                             "WHERE h.host_generation=? AND h.state='finalized' AND j.state=h.outcome "
                             "ORDER BY h.fence DESC LIMIT 1", (self.host_generation,)).fetchone()
        if row is None:
            return None
        release = {'attempt_id': row['attempt_id'], 'generation': row['host_generation'], 'fence': row['fence'],
                   'receipt_digest': row['receipt_digest'], 'hub_ack': row['hub_ack'], 'state': 'released'}
        return {'release': release, 'credential': self._sign('release', release)}

    def _original(self, db, row, attempt, *, states=('claimed', 'held')):
        """Bind to the ORIGINAL queue claim: job, fence, owner epoch, target."""
        job = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (attempt.job_id,)).fetchone()
        machine = db.execute('SELECT generation,job_id FROM hub_work_machines WHERE machine_id=?',
                             (self.manifest.machine_id,)).fetchone()
        policy = db.execute('SELECT action_key,machine_id,manifest_digest,host_generation '
                            'FROM hub_development_ci_handoff_policy WHERE id=1').fetchone()
        return (job is not None and job['state'] in states
                and job['worker'] == WorkWorker(SOFTWARE_VALIDATION_EXECUTOR, attempt.epoch).encoded()
                and job['fence'] == attempt.fence == row['fence'] and row['epoch'] == attempt.epoch
                and job['action_key'] == self.action_key and job['machine_id'] == self.manifest.machine_id
                and job['environment'] == 'staging' and job['actor'] == attempt.actor
                and WorkTarget(**json.loads(job['target'])) == WorkTarget(
                    attempt.task_id, attempt.source_commit, attempt.manifest_digest)
                and attempt.manifest_digest == self.manifest.digest == row['manifest_digest']
                and machine is not None and tuple(machine) == (attempt.fence, attempt.job_id)
                and policy is not None and tuple(policy) == (self.action_key, self.manifest.machine_id,
                                                              self.manifest.digest, self.host_generation)
                and row['host_generation'] == self.host_generation)

    def _review_unchanged(self, attempt):
        try:
            task = self._review(attempt.actor, WorkTarget(attempt.task_id, attempt.source_commit,
                                                          attempt.manifest_digest))
        except Exception:
            return False
        return task['revision'] == attempt.task_revision and _canonical(task['review']) == attempt.review

    def _start_payload(self, attempt):
        return {'attempt_id': attempt.attempt_id, 'job_id': attempt.job_id, 'fence': attempt.fence,
                'generation': self.host_generation, 'manifest_digest': attempt.manifest_digest,
                'source_commit': attempt.source_commit}

    def _host_record(self, attempt, value):
        """Validate a host start/inspect payload against the original attempt."""
        if type(value) is not dict:
            return None
        terminal = _terminal(value.get('terminal'))
        record = {**self._start_payload(attempt), 'terminal': terminal}
        if (terminal is None or value.get('state') != 'terminal_held'
                or any(value.get(key) != record[key] for key in record)
                or not isinstance(value.get('receipt_id'), str) or not _ID.fullmatch(value['receipt_id'])
                or value.get('receipt_digest') != receipt_digest(record)
                or terminal['engine_id'] != self.manifest.engine_id
                or terminal['image_id'] != self.manifest.image_id):
            return None
        return {**record, 'state': 'terminal_held', 'receipt_id': value['receipt_id'],
                'receipt_digest': value['receipt_digest']}

    def _commit_receipt(self, job_id, inspection):
        """receipt_committed: one Hub txn; never trusts a caller-supplied receipt."""
        row = self._row(job_id)
        attempt = DevelopmentCIAttempt(**json.loads(row['attempt']))
        trusted = self._host_record(attempt, inspection)
        if trusted is None or not self._review_unchanged(attempt):
            return False
        with self.repo._transaction() as db:
            row = db.execute('SELECT * FROM hub_development_ci_handoff WHERE job_id=?', (job_id,)).fetchone()
            if (row is None or row['state'] not in ('intent', 'observed')
                    or row['attempt'] != _canonical(asdict(attempt)) or not self._original(db, row, attempt)):
                return False
            if row['observed'] is not None and json.loads(row['observed']) != trusted:
                # Reported receipt differs from the original attempt's journal
                # (container, cgroup, digest...): never commit; stay held.
                return False
            terminal = trusted['terminal']
            outcome = ('succeeded' if terminal['exit_code'] == 0 and not terminal['timed_out']
                       and not terminal['failed'] else 'failed')
            db.execute("UPDATE hub_development_ci_handoff SET state='receipt_committed',receipt_id=?,"
                       "receipt_digest=?,terminal=?,outcome=?,hub_ack=? WHERE job_id=? AND state=?",
                       (trusted['receipt_id'], trusted['receipt_digest'], _canonical(terminal), outcome,
                        secrets.token_hex(32), job_id, row['state']))
            # The host reported the terminal outcome of every effect.
            db.execute("UPDATE hub_development_ci_effects SET state='reported' WHERE job_id=? AND state='in_flight'",
                       (job_id,))
        return True

    def _finalize_request(self, row):
        return {'attempt_id': row['attempt_id'], 'generation': row['host_generation'], 'fence': row['fence'],
                'receipt_digest': row['receipt_digest'], 'hub_ack': row['hub_ack']}

    def _host_ack_valid(self, row):
        """Re-verify the RECORDED authenticated host ack for the exact tuple."""
        try:
            recorded = json.loads(row['host_ack'])
            return (type(recorded) is dict and set(recorded) == {'ack', 'credential'}
                    and recorded['ack'] == {**self._finalize_request(row), 'state': 'finalized_reserved'}
                    and _verified(self.authenticator, 'finalize_result', recorded['ack'], recorded['credential']))
        except Exception:
            return False

    # -- queue verifiers ---------------------------------------------------
    def _handoff_verified(self, job, evidence_id, outcome):
        try:
            self._guard()
            permit = self._permit.get()
            with self.repo._connection() as db:
                row = db.execute('SELECT * FROM hub_development_ci_handoff WHERE job_id=?', (job['id'],)).fetchone()
                if row is None:
                    return False
                attempt = DevelopmentCIAttempt(**json.loads(row['attempt']))
                bound = self._original(db, row, attempt)
            return (bound and permit == (job['id'], row['fence'], evidence_id, outcome, row['hub_ack'])
                    and row['state'] == 'host_acked' and self._host_ack_valid(row)
                    and row['receipt_id'] == evidence_id
                    and row['outcome'] == outcome and job['fence'] == attempt.fence
                    and self._review_unchanged(attempt))
        except Exception:
            return False

    def completion_verifier(self, job, evidence_id, outcome):
        return self._handoff_verified(job, evidence_id, outcome)

    def reconciliation_verifier(self, job, evidence_id, outcome):
        # Only the handoff's own held jobs, only with a recorded verified host ack.
        return outcome in ('succeeded', 'failed') and self._handoff_verified(job, evidence_id, outcome)

    # -- run loop ------------------------------------------------------------
    async def _supervise(self, job, guard, awaitable):
        pending = asyncio.ensure_future(awaitable)
        while not pending.done():
            await asyncio.wait({pending}, timeout=self.renew_interval)
            if pending.done():
                break
            try:
                guard()
                self.queue.renew(job_id=job['id'], worker=self.worker, fence=job['fence'], lease_seconds=600)
            except Exception:
                break  # drain; never cancel an accepted host phase
        return await pending

    async def _finalize(self, job_id):
        row = self._row(job_id)
        if row['state'] == 'receipt_committed':
            request = self._finalize_request(row)
            ack, credential = await self._call('finalize', request)
            if ack != {**request, 'state': 'finalized_reserved'}:
                raise HubError('development_ci_handoff_ack_mismatch', 409)
            with self.repo._transaction() as db:
                current = db.execute('SELECT * FROM hub_development_ci_handoff WHERE job_id=?', (job_id,)).fetchone()
                attempt = DevelopmentCIAttempt(**json.loads(current['attempt']))
                if (current['state'] != 'receipt_committed' or current['hub_ack'] != row['hub_ack']
                        or not self._original(db, current, attempt)):
                    raise HubError('development_ci_handoff_ack_mismatch', 409)
                db.execute("UPDATE hub_development_ci_handoff SET state='host_acked',host_ack=? "
                           "WHERE job_id=? AND state='receipt_committed'",
                           (_canonical({'ack': ack, 'credential': credential}), job_id))
            row = self._row(job_id)
        if row['state'] != 'host_acked' or not self._host_ack_valid(row):
            raise HubError('development_ci_handoff_ack_mismatch', 409)
        token = self._permit.set((job_id, row['fence'], row['receipt_id'], row['outcome'], row['hub_ack']))
        try:
            job = self.queue.get_job(job_id)
            if (job['state'] == 'claimed' and job['worker'] == self.worker.encoded()
                    and not job['requires_reconciliation']):
                return self.queue.complete(job_id=job_id, worker=self.worker, fence=row['fence'],
                                           outcome=row['outcome'], evidence_id=row['receipt_id'])
            # Held (or expired) original claim: same verified tuple through the
            # queue's own reconciliation path.
            return self.queue.reconcile(job_id=job_id, evidence_id=row['receipt_id'], outcome=row['outcome'])
        finally:
            self._permit.reset(token)

    async def _advance(self, job_id):
        """Resume from durable Hub state: re-inspect read-only, commit, finalize."""
        row = self._row(job_id)
        if row['state'] in ('intent', 'observed'):
            attempt = DevelopmentCIAttempt(**json.loads(row['attempt']))
            inspection, _ = await self._call('inspect', {'attempt_id': attempt.attempt_id})
            if not self._commit_receipt(job_id, inspection):
                raise HubError('development_ci_handoff_receipt_refused', 409)
            row = self._row(job_id)
        if row['state'] in ('receipt_committed', 'host_acked'):
            return await self._finalize(job_id)
        return self.queue.get_job(job_id)

    async def _run(self):
        self._guard()
        pending = self._pending()
        if pending is not None:
            # Never claim/start anything while an earlier handoff is unreleased.
            try:
                return await self._advance(pending)
            except (Exception, asyncio.CancelledError):
                job = self.queue.get_job(pending)
                if job['state'] == 'claimed' and job['worker'] == self.worker.encoded():
                    return self._hold(job)
                return job
        job = self.queue.claim(worker=self.worker, lease_seconds=600, action_keys=(self.action_key,))
        if job is None:
            return None
        try:
            task = self._review(job['actor'], WorkTarget(**job['target']))
            attempt = DevelopmentCIAttempt(str(uuid.uuid4()), job['id'], job['fence'], self.epoch,
                task['id'], task['revision'], _canonical(task['review']), job['actor'],
                task['candidate_commit'], self.manifest.digest)
            with self.repo._transaction() as db:
                # Legacy durable attempt row (unique job id: no replay); its
                # 'handoff' state can never satisfy the legacy completion path.
                db.execute("INSERT INTO hub_development_ci_attempts VALUES(?,?,'handoff',NULL,NULL)",
                           (job['id'], _canonical(asdict(attempt))))
                db.execute("INSERT INTO hub_development_ci_handoff(job_id,attempt_id,attempt,fence,epoch,"
                           "host_generation,manifest_digest,state) VALUES(?,?,?,?,?,?,?,'intent')",
                           (job['id'], attempt.attempt_id, _canonical(asdict(attempt)), attempt.fence,
                            attempt.epoch, self.host_generation, attempt.manifest_digest))
            guard = lambda: self._bound(attempt)
            guard()
            challenge, challenge_credential = await self._call(
                'challenge', {'attempt_id': attempt.attempt_id, 'job_id': attempt.job_id, 'fence': attempt.fence})
            statement, claim_credential = self.answer_challenge(challenge, challenge_credential)
            start = {**self._start_payload(attempt), 'prior_release': self._prior_release(),
                     'claim': {'statement': statement, 'credential': claim_credential}}
            reported, _ = await self._supervise(job, guard, self._call('start', start))
            observed = self._host_record(attempt, reported)
            if observed is None:
                return self._hold(job)
            with self.repo._transaction() as db:
                db.execute("UPDATE hub_development_ci_handoff SET state='observed',observed=? "
                           "WHERE job_id=? AND state='intent'", (_canonical(observed), job['id']))
            return await self._advance(job['id'])
        except (Exception, asyncio.CancelledError):
            return self._hold(job)
