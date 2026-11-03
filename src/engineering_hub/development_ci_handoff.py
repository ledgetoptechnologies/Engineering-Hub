"""Disabled-by-default durable CI handoff between the Hub queue and a CI host.

Nothing here is activated by import, environment, MCP or REST. Both sides must
be constructed with ``enable_durable_handoff=True`` by trusted operator code.

Protocol (agreed design):

* Host per-attempt states: ``intent -> running -> terminal_held ->
  finalized_reserved``. ``finalized_reserved`` is NOT free capacity; the host
  never self-frees. The host admits a new attempt only on an authenticated Hub
  start whose fence is strictly greater than every fence it durably recorded
  (journal, reconciled floor and the separate non-rollback anchor) while every
  earlier attempt is ``finalized_reserved``.
* Hub: ``receipt_committed`` is one Hub transaction that checks the trusted,
  authenticated host inspection against the ORIGINAL queue claim and attempt,
  stores the receipt digest and a fresh random ``hub_ack``; the queue job stays
  claimed/held and the machine reserved. After an authenticated host
  ``finalized_reserved`` ack, ONE queue transaction completes the job (machine
  release) and, through a ledger trigger, marks the handoff ``finalized``.

No transport, key, endpoint or credential exists in this repository. Both sides
take an explicit authenticator whose default refuses every message; until a real
authenticated channel is enrolled every attempt stays held.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

from .development_ci import DevelopmentCIAttempt, QueuedDevelopmentCI, _canonical
from .repository import HubError
from .work_queue import SOFTWARE_VALIDATION_EXECUTOR, WorkTarget, WorkWorker

HANDOFF_MARKER = 'HANDOFF_ENROLLED.json'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_HEX64 = re.compile(r'[a-f0-9]{64}')
_GENERATION = re.compile(r'[a-f0-9]{32}')
_DIGEST = re.compile(r'sha256:[a-f0-9]{64}')
_COMMIT = re.compile(r'[a-f0-9]{40}')
_HOST_STATES = ('intent', 'running', 'terminal_held', 'finalized_reserved')


def receipt_digest(record):
    return 'sha256:' + hashlib.sha256(_canonical(record).encode()).hexdigest()


class HandoffRefused(RuntimeError):
    """Refusal with a fixed code; never carries caller or host text."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class RefusingAuthenticator:
    """Default authenticator: no authenticated channel exists, so refuse.

    A real implementation must bind the Hub and the enrolled host to keys that
    are provisioned outside this repository. ``sign`` returning None means no
    credential is available, and callers must not contact the peer.
    """
    def sign(self, operation, payload):
        return None

    def verify(self, operation, payload, credential):
        return False


def _authenticator(value):
    value = RefusingAuthenticator() if value is None else value
    if not callable(getattr(value, 'sign', None)) or not callable(getattr(value, 'verify', None)):
        raise HandoffRefused('handoff_authenticator_required')
    return value


def _verified(authenticator, operation, payload, credential):
    try:
        return credential is not None and authenticator.verify(operation, payload, credential) is True
    except Exception:
        return False


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


def _start_request(value):
    keys = {'attempt_id', 'job_id', 'fence', 'generation', 'manifest_digest', 'source_commit'}
    if (type(value) is not dict or set(value) != keys
            or not isinstance(value['attempt_id'], str) or not _ID.fullmatch(value['attempt_id'])
            or not isinstance(value['job_id'], str) or not _ID.fullmatch(value['job_id'])
            or not _int(value['fence'], 1)
            or not isinstance(value['generation'], str) or not _GENERATION.fullmatch(value['generation'])
            or not isinstance(value['manifest_digest'], str) or not _DIGEST.fullmatch(value['manifest_digest'])
            or not isinstance(value['source_commit'], str) or not _COMMIT.fullmatch(value['source_commit'])):
        raise HandoffRefused('handoff_request_invalid')
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


# --------------------------------------------------------------------------
# Host side
# --------------------------------------------------------------------------

_HOST_SCHEMA = '''
CREATE TABLE handoff_meta (
    id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL, floor_fence INTEGER NOT NULL);
CREATE TABLE handoff_attempts (
    attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, fence INTEGER NOT NULL UNIQUE,
    generation TEXT NOT NULL, manifest_digest TEXT NOT NULL, source_commit TEXT NOT NULL,
    state TEXT NOT NULL, receipt_id TEXT, receipt_digest TEXT, terminal TEXT, hub_ack TEXT);
CREATE TRIGGER handoff_meta_guard BEFORE UPDATE ON handoff_meta
WHEN NEW.generation IS NOT OLD.generation OR NEW.floor_fence < OLD.floor_fence
BEGIN SELECT RAISE(ABORT,'handoff_meta_immutable'); END;
CREATE TRIGGER handoff_meta_delete BEFORE DELETE ON handoff_meta
BEGIN SELECT RAISE(ABORT,'handoff_meta_immutable'); END;
CREATE TRIGGER handoff_insert_guard BEFORE INSERT ON handoff_attempts
WHEN NEW.state IS NOT 'intent' OR NEW.receipt_id IS NOT NULL OR NEW.receipt_digest IS NOT NULL
  OR NEW.terminal IS NOT NULL OR NEW.hub_ack IS NOT NULL
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
      AND NEW.receipt_digest IS NULL AND NEW.terminal IS NULL AND NEW.hub_ack IS NULL)
    OR (OLD.state='running' AND NEW.state='terminal_held' AND NEW.receipt_id IS NOT NULL
      AND NEW.receipt_digest IS NOT NULL AND NEW.terminal IS NOT NULL AND NEW.hub_ack IS NULL)
    OR (OLD.state='terminal_held' AND NEW.state='finalized_reserved'
      AND NEW.receipt_id IS OLD.receipt_id AND NEW.receipt_digest IS OLD.receipt_digest
      AND NEW.terminal IS OLD.terminal AND NEW.hub_ack IS NOT NULL
      AND length(NEW.hub_ack)=64 AND NEW.hub_ack NOT GLOB '*[^0-9a-f]*'))
BEGIN SELECT RAISE(ABORT,'handoff_transition_refused'); END;
'''


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_atomic(path, value, *, exclusive=False):
    path = Path(path)
    raw = json.dumps(value, sort_keys=True).encode()
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    _fsync_directory(path.parent)


def _read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise HandoffRefused('handoff_file_unavailable')
    return json.loads(path.read_bytes())


def _flock(path):
    import fcntl  # Linux host only; there is deliberately no Windows fallback.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise HandoffRefused('handoff_host_busy') from None
    return fd


def enroll_host(journal_path, anchor_path, engine_directory, *, enable_durable_handoff=False):
    """Explicit operator enrollment; never called by import or a Hub message.

    Takes the same engine admission flock as the standalone controller, refuses
    if that controller retained uncertain state, then writes the writer-exclusion
    marker BEFORE creating the journal and its separate non-rollback anchor.
    """
    if enable_durable_handoff is not True:
        raise HandoffRefused('handoff_disabled')
    journal, anchor, engine = Path(journal_path), Path(anchor_path), Path(engine_directory)
    if journal.resolve() == anchor.resolve() or os.path.lexists(journal) or os.path.lexists(anchor):
        raise HandoffRefused('handoff_already_enrolled')
    lock = _flock(engine / 'admission.lock')
    try:
        if any(os.path.lexists(engine / name) for name in ('HELD.json', 'STOPPING.json', HANDOFF_MARKER)):
            raise HandoffRefused('handoff_engine_not_quiescent')
        generation = secrets.token_hex(16)
        _write_json_atomic(engine / HANDOFF_MARKER, {'schema': 'engineering-hub.ci-handoff.v1',
            'generation': generation, 'journal': str(journal)}, exclusive=True)
        db = sqlite3.connect(journal, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            for statement in _HOST_SCHEMA.split(';\n'):
                if statement.strip():
                    db.execute(statement)
            db.execute('INSERT INTO handoff_meta VALUES(1,?,0)', (generation,))
            db.execute('COMMIT')
        finally:
            db.close()
        _write_json_atomic(anchor, {'schema': 'engineering-hub.ci-handoff-anchor.v1',
                                    'generation': generation, 'highest_fence': 0}, exclusive=True)
        return generation
    finally:
        os.close(lock)


def reconcile_restored_journal(journal_path, anchor_path, *, operator_confirmed=False):
    """Offline operator reconciliation after a journal restore. Never automatic.

    The operator must already have inspected every host effect between the
    restored journal and the anchor. This only raises the journal's floor to the
    anchor fence so older/equal fences can never be admitted again.
    """
    if operator_confirmed is not True:
        raise HandoffRefused('handoff_reconciliation_requires_operator')
    anchor = _read_json(anchor_path)
    db = sqlite3.connect(Path(journal_path).absolute().as_uri() + '?mode=rw', uri=True, isolation_level=None)
    try:
        db.execute('BEGIN IMMEDIATE')
        generation, floor = db.execute('SELECT generation,floor_fence FROM handoff_meta WHERE id=1').fetchone()
        if anchor.get('generation') != generation or not _int(anchor.get('highest_fence')):
            db.execute('ROLLBACK')
            raise HandoffRefused('handoff_anchor_mismatch')
        db.execute('UPDATE handoff_meta SET floor_fence=? WHERE id=1', (max(floor, anchor['highest_fence']),))
        db.execute('COMMIT')
    finally:
        db.close()


class CIHostHandoff:
    """Host-side journal and contract for start / inspect / finalize.

    ``runner(start_request, guard)`` is the trusted host executor; it must call
    ``guard()`` before each new effect and return the private terminal
    observation for the ORIGINAL container and pinned cgroup. It is injected,
    because the standalone controller has no handoff mode (see module report).
    """
    def __init__(self, journal_path, anchor_path, engine_directory, *, manifest_digest,
                 runner, authenticator=None, enable_durable_handoff=False):
        if enable_durable_handoff is not True:
            raise HandoffRefused('handoff_disabled')
        if not isinstance(manifest_digest, str) or not _DIGEST.fullmatch(manifest_digest) or not callable(runner):
            raise HandoffRefused('handoff_host_composition_required')
        self.journal, self.anchor = Path(journal_path), Path(anchor_path)
        self.engine = Path(engine_directory)
        if not self.journal.is_file() or self.journal.resolve() == self.anchor.resolve():
            raise HandoffRefused('handoff_not_enrolled')
        self.manifest_digest, self.runner = manifest_digest, runner
        self.authenticator = _authenticator(authenticator)
        self._local = threading.Lock()

    @contextmanager
    def _db(self, *, write):
        mode = 'rw' if write else 'ro'
        db = sqlite3.connect(self.journal.absolute().as_uri() + '?mode=' + mode, uri=True,
                             isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        try:
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
        finally:
            db.close()

    def _generation(self, db):
        row = db.execute('SELECT generation,floor_fence FROM handoff_meta WHERE id=1').fetchone()
        if row is None:
            raise HandoffRefused('handoff_not_enrolled')
        return row['generation'], row['floor_fence']

    def _writer_excluded(self, generation):
        try:
            marker = _read_json(self.engine / HANDOFF_MARKER)
        except Exception:
            raise HandoffRefused('handoff_writer_exclusion_missing') from None
        if marker.get('generation') != generation:
            raise HandoffRefused('handoff_writer_exclusion_missing')

    def _anchor_fence(self, generation):
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

    def start(self, request, credential):
        """Admit at most one new attempt; any doubt refuses before an effect."""
        request = _start_request(request)
        self._verify('start', request, credential)
        if not self._local.acquire(blocking=False):
            raise HandoffRefused('handoff_host_busy')
        try:
            lock = _flock(str(self.journal) + '.lock')
            try:
                return self._start_locked(request)
            finally:
                os.close(lock)
        finally:
            self._local.release()

    def _start_locked(self, request):
        with self._db(write=True) as db:
            generation, floor = self._generation(db)
            self._writer_excluded(generation)
            anchored = self._anchor_fence(generation)
            rows = db.execute('SELECT attempt_id,fence,state FROM handoff_attempts').fetchall()
            journal_high = max([row['fence'] for row in rows] + [floor])
            if journal_high < anchored:
                raise HandoffRefused('handoff_journal_restored')
            if request['generation'] != generation or request['manifest_digest'] != self.manifest_digest:
                raise HandoffRefused('handoff_binding_mismatch')
            if request['fence'] <= max(journal_high, anchored):
                raise HandoffRefused('handoff_fence_stale')
            if any(row['state'] != 'finalized_reserved' for row in rows):
                raise HandoffRefused('handoff_slot_held')
            db.execute("INSERT INTO handoff_attempts VALUES(?,?,?,?,?,?,'intent',NULL,NULL,NULL,NULL)",
                       (request['attempt_id'], request['job_id'], request['fence'], generation,
                        request['manifest_digest'], request['source_commit']))
        # Journal first, then anchor: a crash between leaves journal >= anchor
        # (safe); a failed anchor write leaves this attempt held at intent.
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
            self._writer_excluded(current)
            if row is None or row['state'] != 'running' or current != generation:
                raise HandoffRefused('handoff_admission_lost')

        # Any runner failure or invalid observation leaves the row 'running':
        # held, never replayed, never self-freed.
        terminal = _terminal(_plain(self.runner(dict(request), guard)))
        if terminal is None:
            raise HandoffRefused('handoff_terminal_unavailable')
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
        finalized_reserved keeps the slot reserved: only a Hub start carrying a
        strictly greater fence can admit the next attempt.
        """
        request = _finalize_request(request)
        self._verify('finalize', request, credential)
        with self._db(write=True) as db:
            generation, _ = self._generation(db)
            self._writer_excluded(generation)
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
    receipt_digest TEXT, terminal TEXT, outcome TEXT, hub_ack TEXT);
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_insert BEFORE INSERT ON hub_development_ci_handoff
WHEN NEW.state IS NOT 'intent' OR NEW.observed IS NOT NULL OR NEW.receipt_id IS NOT NULL
  OR NEW.receipt_digest IS NOT NULL OR NEW.terminal IS NOT NULL OR NEW.outcome IS NOT NULL OR NEW.hub_ack IS NOT NULL
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_insert'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_delete BEFORE DELETE ON hub_development_ci_handoff
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_delete'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_transition BEFORE UPDATE ON hub_development_ci_handoff
WHEN NEW.job_id IS NOT OLD.job_id OR NEW.attempt_id IS NOT OLD.attempt_id OR NEW.attempt IS NOT OLD.attempt
  OR NEW.fence IS NOT OLD.fence OR NEW.epoch IS NOT OLD.epoch OR NEW.host_generation IS NOT OLD.host_generation
  OR NEW.manifest_digest IS NOT OLD.manifest_digest
  OR NOT (
    (OLD.state='intent' AND NEW.state='observed' AND NEW.observed IS NOT NULL AND NEW.receipt_id IS NULL
      AND NEW.receipt_digest IS NULL AND NEW.terminal IS NULL AND NEW.outcome IS NULL AND NEW.hub_ack IS NULL)
    OR (OLD.state IN ('intent','observed') AND NEW.state='receipt_committed' AND NEW.observed IS OLD.observed
      AND NEW.receipt_id IS NOT NULL AND NEW.receipt_digest IS NOT NULL AND NEW.terminal IS NOT NULL
      AND NEW.outcome IN ('succeeded','failed') AND length(NEW.hub_ack)=64)
    OR (OLD.state='receipt_committed' AND NEW.state='finalized' AND NEW.observed IS OLD.observed
      AND NEW.receipt_id IS OLD.receipt_id AND NEW.receipt_digest IS OLD.receipt_digest
      AND NEW.terminal IS OLD.terminal AND NEW.outcome IS OLD.outcome AND NEW.hub_ack IS OLD.hub_ack
      AND (SELECT state FROM hub_work_jobs WHERE id=OLD.job_id) IS OLD.outcome))
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_transition'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_release_guard BEFORE UPDATE OF state ON hub_work_jobs
WHEN NEW.state IN ('succeeded','failed')
  AND EXISTS(SELECT 1 FROM hub_development_ci_handoff WHERE job_id=NEW.id)
  AND NOT EXISTS(SELECT 1 FROM hub_development_ci_handoff WHERE job_id=NEW.id
                 AND state='receipt_committed' AND outcome=NEW.state AND hub_ack IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'development_ci_handoff_unfinalized'); END;
CREATE TRIGGER IF NOT EXISTS hub_ci_handoff_release_record AFTER UPDATE OF state ON hub_work_jobs
WHEN NEW.state IN ('succeeded','failed')
BEGIN UPDATE hub_development_ci_handoff SET state='finalized'
      WHERE job_id=NEW.id AND state='receipt_committed' AND outcome=NEW.state; END;
'''


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
    """
    _handoff_mode = True

    def __init__(self, queue, owner, manifest, channel, *, action_key, host_generation,
                 authenticator=None, enable_durable_handoff=False, renew_interval=120,
                 monotonic=time.monotonic):
        if enable_durable_handoff is not True:
            raise HubError('development_ci_handoff_disabled', 409)
        if (not isinstance(host_generation, str) or not _GENERATION.fullmatch(host_generation)
                or any(not inspect.iscoroutinefunction(getattr(channel, name, None))
                       for name in ('start', 'inspect', 'finalize'))):
            raise HubError('development_ci_handoff_composition_required', 409)
        try:
            self.authenticator = _authenticator(authenticator)
        except HandoffRefused:
            raise HubError('development_ci_handoff_composition_required', 409) from None
        self.channel, self.host_generation = channel, host_generation
        super().__init__(queue, owner, manifest, _UnusedProvider(), action_key=action_key,
                         renew_interval=renew_interval, monotonic=monotonic)
        with self.repo._transaction() as db:
            for statement in _HUB_SCHEMA.split(';\n'):
                if statement.strip():
                    db.execute(statement)
            expected = (action_key, manifest.machine_id, manifest.digest, host_generation)
            old = db.execute('SELECT action_key,machine_id,manifest_digest,host_generation '
                             'FROM hub_development_ci_handoff_policy WHERE id=1').fetchone()
            if old is not None and tuple(old) != expected:
                raise HubError('development_ci_handoff_enrollment_changed', 409)
            db.execute('INSERT OR IGNORE INTO hub_development_ci_handoff_policy VALUES(1,?,?,?,?)', expected)
        self._reconciliation = self.reconciliation_verifier
        queue.reconciliation_verifier = self._reconciliation

    # -- transport --------------------------------------------------------
    async def _call(self, operation, payload):
        payload = _plain(payload)
        try:
            credential = self.authenticator.sign(operation, payload)
        except Exception:
            credential = None
        if credential is None:
            # Missing authentication: never contact the host unauthenticated.
            raise HubError('development_ci_handoff_unauthenticated', 409)
        response, response_credential = await getattr(self.channel, operation)(payload, credential)
        response = _plain(response)
        if not _verified(self.authenticator, operation + '_result', response, response_credential):
            raise HubError('development_ci_handoff_unauthenticated', 409)
        return response

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
        return True

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
                    and row['state'] == 'receipt_committed' and row['receipt_id'] == evidence_id
                    and row['outcome'] == outcome and job['fence'] == attempt.fence
                    and self._review_unchanged(attempt))
        except Exception:
            return False

    def completion_verifier(self, job, evidence_id, outcome):
        return self._handoff_verified(job, evidence_id, outcome)

    def reconciliation_verifier(self, job, evidence_id, outcome):
        # Only the handoff's own held jobs, only with an acked host finalize.
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
        request = {'attempt_id': row['attempt_id'], 'generation': row['host_generation'], 'fence': row['fence'],
                   'receipt_digest': row['receipt_digest'], 'hub_ack': row['hub_ack']}
        ack = await self._call('finalize', request)
        if ack != {**request, 'state': 'finalized_reserved'}:
            raise HubError('development_ci_handoff_ack_mismatch', 409)
        token = self._permit.set((job_id, row['fence'], row['receipt_id'], row['outcome'], row['hub_ack']))
        try:
            job = self.queue.get_job(job_id)
            if (job['state'] == 'claimed' and job['worker'] == self.worker.encoded()
                    and not job['requires_reconciliation']):
                return self.queue.complete(job_id=job_id, worker=self.worker, fence=row['fence'],
                                           outcome=row['outcome'], evidence_id=row['receipt_id'])
            # Held (or expired) original claim: same verified tuple, same txn
            # semantics, via the queue's own reconciliation path.
            return self.queue.reconcile(job_id=job_id, evidence_id=row['receipt_id'], outcome=row['outcome'])
        finally:
            self._permit.reset(token)

    async def _advance(self, job_id):
        """Resume from durable Hub state: re-inspect read-only, commit, finalize."""
        row = self._row(job_id)
        if row['state'] in ('intent', 'observed'):
            attempt = DevelopmentCIAttempt(**json.loads(row['attempt']))
            inspection = await self._call('inspect', {'attempt_id': attempt.attempt_id})
            if not self._commit_receipt(job_id, inspection):
                raise HubError('development_ci_handoff_receipt_refused', 409)
            row = self._row(job_id)
        if row['state'] == 'receipt_committed':
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
            reported = await self._supervise(job, guard, self._call('start', self._start_payload(attempt)))
            observed = self._host_record(attempt, reported)
            if observed is None:
                return self._hold(job)
            with self.repo._transaction() as db:
                db.execute("UPDATE hub_development_ci_handoff SET state='observed',observed=? "
                           "WHERE job_id=? AND state='intent'", (_canonical(observed), job['id']))
            return await self._advance(job['id'])
        except (Exception, asyncio.CancelledError):
            return self._hold(job)
