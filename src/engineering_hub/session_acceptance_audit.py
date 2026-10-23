"""Explicit owned-Hub SQLite audit sink; never restores admission authority.

Import creates nothing. Construction/write require a live exact PilotOwner and
the existing repository's WAL plus FULL/EXTRA synchronous durability settings.
No settings are repaired and no audit history can open a session or task gate.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from uuid import UUID

from .pilot_ownership import PilotOwner
from .repository import HubRepository
from .session_acceptance import SessionAcceptanceAudit
from .startup_reconciliation import ExecutorSessionIdentity

_TABLE = 'hub_session_acceptance_audit'
_SCHEMA = ('CREATE TABLE hub_session_acceptance_audit ('
           'request_id TEXT NOT NULL, event TEXT NOT NULL, record TEXT NOT NULL, '
           'PRIMARY KEY(request_id,event))')


class SessionAcceptanceAuditError(RuntimeError):
    def __init__(self):
        super().__init__('owned session acceptance audit unavailable; preserve evidence')


def _require(value):
    if not value:
        raise SessionAcceptanceAuditError()


def _digest(value):
    return type(value) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', value) is not None


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value and UUID(value).version == 4
    except (ValueError, TypeError, AttributeError):
        return False


def _record(record):
    _require(type(record) is SessionAcceptanceAudit
             and type(record.event) is str and record.event in {'intent', 'result'}
             and type(record.outcome) is str and record.outcome in
                 ({'accept', 'decline'} if record.event == 'intent' else {'accepted', 'declined', 'failed'})
             and type(record.request_id) is str and re.fullmatch(r'[0-9a-f]{64}', record.request_id)
             and type(record.revision) is int and 1 <= record.revision <= 2**31 - 1
             and type(record.actor) is str and re.fullmatch(
                 r'human:(?:[1-9][0-9]{0,19}|cf:[0-9a-f]{64})', record.actor)
             and _uuid(record.owner_epoch)
             and type(record.target_id) is str and record.target_id == 'engineering-hub:staging'
             and _digest(record.manifest_digest) and _digest(record.report_digest)
             and type(record.executor) is ExecutorSessionIdentity
             and _uuid(record.executor.generation_id) and _digest(record.executor.artifact_digest)
             and record.execution_authorization is False)
    raw = json.dumps(asdict(record), sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                     allow_nan=False)
    _require(len(raw.encode()) <= 2048)
    return raw


def _decode_record(raw):
    """Previously committed rows must still have the exact typed canonical form."""
    _require(type(raw) is str and 0 < len(raw.encode()) <= 2048)
    value = json.loads(raw)
    _require(type(value) is dict and set(value) == set(SessionAcceptanceAudit.__dataclass_fields__)
             and type(value.get('executor')) is dict
             and set(value['executor']) == set(ExecutorSessionIdentity.__dataclass_fields__))
    value['executor'] = ExecutorSessionIdentity(**value['executor'])
    record = SessionAcceptanceAudit(**value)
    _require(_record(record) == raw)
    return record


class OwnedSessionAcceptanceAudit:
    """Callable durable acknowledgement using the exact owner's transaction.

    The sink is PID/owner/epoch/repository-bound and append-only at this API.
    Every duplicate/conflict raises; it does not overwrite or retry. SQLite's
    successful WAL FULL/EXTRA commit is the durability boundary. The OS, storage,
    mount and device must honor SQLite's fsync contract; this is not NAS or power
    loss verification, anti-rollback protection, or defense against trusted root.

    Any failure seals this sink. If the commit completed but a post-commit owner
    check fails, a row may remain durable even though this call raises. Preserve
    evidence; do not claim rollback, automatically retry, or reload accepted bits.
    """
    def __init__(self, repository):
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._closed = False
        try:
            _require(type(repository) is HubRepository
                     and type(repository.pilot_owner) is PilotOwner)
            self._repository = repository
            self._owner = repository.pilot_owner
            self._path = Path(repository.path).absolute()
            self._epoch = self._owner.epoch
            _require(_uuid(self._epoch))
            self._identity()
            with repository._transaction() as db:
                self._durability(db)
                exists = db.execute('SELECT 1 FROM sqlite_master WHERE name=? COLLATE NOCASE',
                                    (_TABLE,)).fetchone()
                if exists is None:
                    db.execute(_SCHEMA)
                self._schema(db)
                self._identity()
            self._identity()
        except BaseException as error:
            self._closed = True
            if isinstance(error, Exception):
                raise SessionAcceptanceAuditError() from None
            raise

    def _identity(self):
        _require(not self._closed and os.getpid() == self._pid
                 and type(self._repository) is HubRepository
                 and self._repository.pilot_owner is self._owner
                 and Path(self._repository.path).absolute() == self._path
                 and self._owner.epoch == self._epoch)
        self._owner.assert_active(self._path)

    @staticmethod
    def _durability(db):
        # Observe only: do not silently upgrade unsafe connection/database policy.
        _require(db.in_transaction is True
                 and db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
                 and db.execute('PRAGMA synchronous').fetchone()[0] in (2, 3))

    @staticmethod
    def _schema(db):
        objects = {tuple(row) for row in db.execute(
            'SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? COLLATE NOCASE', (_TABLE,))}
        _require(objects == {
            ('table', _TABLE, _SCHEMA),
            ('index', 'sqlite_autoindex_hub_session_acceptance_audit_1', None)})

    def __call__(self, record):
        # Deny a fork before touching a possibly inherited locked RLock.
        if os.getpid() != self._pid:
            self._closed = True
            raise SessionAcceptanceAuditError()
        with self._lock:
            try:
                self._identity()
                raw = _record(record)
                _require(record.owner_epoch == self._epoch)
                with self._repository._transaction() as db:
                    self._durability(db)
                    self._schema(db)
                    existing = list(db.execute('SELECT event,record FROM ' + _TABLE + ' WHERE request_id=?',
                                               (record.request_id,)))
                    if record.event == 'intent':
                        _require(not existing)
                    else:
                        _require(len(existing) == 1 and existing[0]['event'] == 'intent')
                        intent_record = _decode_record(existing[0]['record'])
                        _require(intent_record.event == 'intent')
                        intent = asdict(intent_record)
                        expected = asdict(record)
                        expected['event'] = 'intent'
                        expected['outcome'] = ('accept' if record.outcome == 'accepted' else
                                               'decline' if record.outcome == 'declined' else intent.get('outcome'))
                        _require(intent.get('outcome') in {'accept', 'decline'} and intent == expected)
                    db.execute('INSERT INTO ' + _TABLE + '(request_id,event,record) VALUES(?,?,?)',
                               (record.request_id, record.event, raw))
                    if record.event == 'result':
                        # The native feed watches this existing durable outbox,
                        # not arbitrary WAL writes. Commit a sanitized pointer
                        # with the result so an already-queued pending session
                        # wakes after acceptance, without polling or new work.
                        # This pointer is invalidation only: no report, actor,
                        # executor pins or reloadable admission bit is emitted.
                        self._repository._emit(db, 'staging.session.' + record.outcome, {
                            'id': 'staging-session-' + record.request_id,
                            'revision': record.revision,
                            'updated_at': datetime.now(timezone.utc).isoformat()})
                    self._durability(db)
                    self._identity()
                # Commit has returned. A failure now is an uncertain retained
                # record, NOT evidence that the transaction rolled back.
                self._identity()
                return True
            except BaseException as error:
                self._closed = True
                if isinstance(error, Exception):
                    raise SessionAcceptanceAuditError() from None
                raise

    def close(self):
        if os.getpid() != self._pid:
            self._closed = True
            return
        with self._lock:
            self._closed = True  # Caller retains its repository and PilotOwner.
