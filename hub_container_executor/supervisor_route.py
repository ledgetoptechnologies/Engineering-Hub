"""DISABLED-BY-DEFAULT supervisor-owned Hub command/observation route foundation.

Nothing runs at import and there is no default instance, factory or listener.
``SupervisorRoute`` refuses construction unless handed a
``SupervisorRouteConfig`` whose ``enabled`` is exactly ``True``. It is not wired
into the launch supervisor, the executor ASGI app, the Hub, any composition or
any service; constructing it is an explicit operator/test act.

What it does: each forwarded command is bound to its HTTP method, path,
SHA-256 of the exact body bytes, command_id, queue fence, the enrolled launch
epoch (launch_id + persisted monotonic launch counter + boot_id) and the pinned
backend identity (fields of the existing signed current-observation pins). A
durable pre-effect intent row is committed (SQLite, synchronous=FULL, rollback
journal, parent-directory fsync, ``PilotOwner`` single-owner lock, as in
``operation_journal``) in the same transaction that checks the epoch high-water
mark and command_id uniqueness BEFORE anything is forwarded. Identical replay
returns the recorded outcome, ``in_progress`` or ``held``; a different binding
under a known command_id is a conflict and is never forwarded. Ambiguity
(transport loss, timeout, malformed backend reply, backend 5xx, epoch change
mid-command, observer/command backend mismatch, failure to record the outcome)
is durably ``unknown`` where possible and always reported ``held``; it is never
redispatched automatically. ``inspect`` is read-only and never forwards. A
definite outcome is returned with an Ed25519 record signed by the SAME enrolled
issuer key the caller already holds (verified against the enrolled public key),
under a distinct signature domain, including an observation nonce derived from
the binding digest. No key material is generated or discovered here.

Explicit limits (this foundation grants no authority by itself):

* SO_PEERCRED identifies the process that created/listened on the backend
  socket at connect time, not later holders of that socket or its FDs.
* FD delegation inside the container is unresolved: a pinned init process can
  pass its listening socket or connections to another process undetected.
* Independent Docker writers on the engine are not fenced; the queue fence and
  epoch bind only commands that pass through this route.
* The persisted monotonic launch counter is NOT produced by the launch
  supervisor today (its launch journal holds exactly one launch row and no
  counter). This route only persists and enforces the highest counter it has
  SEEN; the counter's authoritative source is a caller-supplied epoch source.
* The executor does not sign its command responses (it holds no key). Backend
  reply authenticity rests only on the SO_PEERCRED/pidfd peer check of the
  pinned init process; the signed record attests what the supervisor forwarded
  and received, not independent proof of Docker effects or health.
* The actual supervisor/engine wiring (who constructs this, which socket the
  executor serves commands on, handing the supervisor's retained signer to it)
  and the Hub-side client integration are NOT implemented.
* Same-UID/root administrators remain trusted; the private-path guards do not
  sandbox hostile same-UID processes racing filesystem replacement.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import sqlite3
import struct
import sys
import threading
import time
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins


PROTOCOL = 'engineering-hub.supervisor-route-record.v1'
AUDIENCE = 'engineering-hub.hub-supervisor-route'
# Distinct from the launch-receipt and current-observation domains, so a route
# record signed by the same enrolled key can never verify as either of those.
SIGNATURE_DOMAIN = b'engineering-hub-supervisor-route-v1\x00'
_NONCE_DOMAIN = b'engineering-hub-supervisor-route-observation-nonce-v1\x00'
MAX_BODY_BYTES = 8192
MAX_RESPONSE_BYTES = 16 * 1024
MAX_RECORD_BYTES = 64 * 1024
_BUSY_MS = 250
_METHODS = frozenset({'GET', 'POST'})
_PATH = re.compile(r'/v1/container-pilot/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){0,3}\Z')
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_SIGNATURE = re.compile(r'[A-Za-z0-9_-]{86}\Z')
_INTENT = ('CREATE TABLE supervisor_route_intent (command_id TEXT PRIMARY KEY, '
           'binding_digest TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL, '
           'owner_epoch TEXT NOT NULL, error TEXT, record TEXT, response BLOB)')
_EPOCH = ('CREATE TABLE supervisor_route_epoch (id INTEGER PRIMARY KEY CHECK(id=1), '
          'launch_counter INTEGER NOT NULL, launch_id TEXT NOT NULL, boot_id TEXT NOT NULL)')
_TABLES = {'hub_pilot_owner', 'hub_pilot_owner_epochs', 'supervisor_route_intent', 'supervisor_route_epoch'}
_STATES = frozenset({'intent', 'completed', 'unknown'})
_ERRORS = frozenset({'transport_lost', 'malformed_response', 'backend_ambiguous', 'epoch_changed',
                     'backend_identity_mismatch', 'outcome_unrecorded', 'interrupted', 'signing_failed'})


class SupervisorRouteError(RuntimeError):
    """Sanitized denial; never reflects paths, bodies, keys or backend text."""


class SupervisorRouteDisabled(SupervisorRouteError):
    """No explicit enabled configuration: the route does not exist."""


class SupervisorRouteRejected(SupervisorRouteError):
    """Definitely NOT forwarded (invalid, stale epoch, or conflicting replay)."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__('supervisor route rejected command: ' + reason)


class SupervisorRouteRefused(SupervisorRouteError):
    """Fail-closed: the durable intent could not be committed; NOT forwarded."""

    def __init__(self):
        super().__init__('supervisor route journal unavailable; command not forwarded')


class RouteJournalError(SupervisorRouteError):
    pass


def _require(condition, error=None):
    if not condition:
        raise error if error is not None else SupervisorRouteRejected('invalid')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _strict_json(raw, maximum):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ValueError

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject(_):
        raise ValueError
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


def _uuid(value, *, v4=False):
    try:
        return (type(value) is str and str(UUID(value)) == value
                and (not v4 or UUID(value).version == 4))
    except (ValueError, TypeError, AttributeError):
        return False


def _positive(value, upper=2**63 - 1):
    return type(value) is int and 0 < value <= upper


def _now_ms():
    wall = time.time()
    _require(type(wall) in (int, float) and math.isfinite(wall) and wall > 0, SupervisorRouteError('clock'))
    return int(wall * 1000)


@dataclass(frozen=True)
class RouteEpoch:
    """Enrolled launch epoch: launch_id + monotonic launch counter + boot_id."""
    launch_id: str
    launch_counter: int
    boot_id: str

    def __post_init__(self):
        _require(_uuid(self.launch_id, v4=True) and _uuid(self.boot_id) and _positive(self.launch_counter))

    def as_dict(self):
        return {'launch_id': self.launch_id, 'launch_counter': self.launch_counter, 'boot_id': self.boot_id}


@dataclass(frozen=True)
class BackendPin:
    """Backend identity pinned for an epoch, from the signed observation pins."""
    container_id: str
    host_pid: int
    process_start_ticks: int
    uid: int
    gid: int
    engine_id: str
    engine_pid: int
    engine_start_ticks: int

    def __post_init__(self):
        _require(type(self.container_id) is str and _HEX.fullmatch(self.container_id)
                 and type(self.engine_id) is str and _ID.fullmatch(self.engine_id)
                 and all(_positive(v) for v in (self.host_pid, self.process_start_ticks,
                                                self.engine_pid, self.engine_start_ticks))
                 and all(type(v) is int and 0 <= v < 2**32 - 1 for v in (self.uid, self.gid)))

    @classmethod
    def from_launch_pins(cls, pins, *, uid, gid):
        """Reuse the exact fields the current-observation protocol signs."""
        _require(type(pins) is CurrentLaunchPins)
        return cls(pins.container_id, pins.authenticated_host_pid, pins.process_start_ticks,
                   uid, gid, pins.engine_id, pins.engine_pid, pins.engine_start_ticks)

    def as_dict(self):
        return {'container_id': self.container_id, 'host_pid': self.host_pid,
                'process_start_ticks': self.process_start_ticks, 'uid': self.uid, 'gid': self.gid,
                'engine_id': self.engine_id, 'engine_pid': self.engine_pid,
                'engine_start_ticks': self.engine_start_ticks}


@dataclass(frozen=True)
class ObservedPeer:
    """Kernel-observed backend peer for ONE forwarded exchange."""
    pid: int
    uid: int
    gid: int
    start_ticks: int

    def matches(self, pin):
        return (type(pin) is BackendPin and (self.pid, self.uid, self.gid, self.start_ticks)
                == (pin.host_pid, pin.uid, pin.gid, pin.process_start_ticks))


@dataclass(frozen=True)
class BackendReply:
    status: int
    body: bytes
    peer: ObservedPeer


@dataclass(frozen=True)
class RouteCommand:
    """One Hub command plus the epoch the Hub believes is current."""
    method: str
    path: str
    body: bytes
    command_id: str
    queue_fence: int
    epoch: RouteEpoch

    def __post_init__(self):
        _require(type(self.method) is str and self.method in _METHODS
                 and type(self.path) is str and _PATH.fullmatch(self.path)
                 and type(self.body) is bytes and len(self.body) <= MAX_BODY_BYTES
                 and (self.method == 'POST' or self.body == b'')
                 and _uuid(self.command_id) and _positive(self.queue_fence)
                 and type(self.epoch) is RouteEpoch)


def binding_for(command, pin):
    """Canonical binding over every forwarded/attested field."""
    _require(type(command) is RouteCommand and type(pin) is BackendPin)
    return {'method': command.method, 'path': command.path,
            'body_sha256': hashlib.sha256(command.body).hexdigest(),
            'command_id': command.command_id, 'queue_fence': command.queue_fence,
            'epoch': command.epoch.as_dict(), 'backend': pin.as_dict()}


def binding_digest(binding):
    return hashlib.sha256(_canonical(binding)).hexdigest()


def observation_nonce(digest):
    _require(type(digest) is str and _HEX.fullmatch(digest))
    return hashlib.sha256(_NONCE_DOMAIN + bytes.fromhex(digest)).hexdigest()


@dataclass(frozen=True)
class RouteResult:
    """state: completed | in_progress | held. Only completed carries a record."""
    state: str
    command_id: str
    binding_digest: str
    replayed: bool
    error: str | None = None
    record: bytes | None = None
    response: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SupervisorRouteConfig:
    """Explicit configuration. ``enabled`` defaults to False: no route exists."""
    enabled: bool = False
    journal_path: str | None = None
    create_journal: bool = False
    forward_timeout_ms: int = 5000
    allow_ephemeral_test_storage: bool = False


def verify_route_record(raw, *, public_key, expected_binding, issuer_id):
    """Hub-side verification interface (not yet wired into any Hub client).

    Returns the verified payload or raises SupervisorRouteError. The caller
    supplies the binding it intended plus the independently enrolled key.
    """
    error = SupervisorRouteError('supervisor route record unverified')
    try:
        envelope = _strict_json(raw, MAX_RECORD_BYTES)
        _require(_canonical(envelope) == raw and type(envelope) is dict
                 and set(envelope) == {'payload', 'signature'}, error)
        payload, signature = envelope['payload'], envelope['signature']
        _require(type(payload) is dict and type(signature) is str and _SIGNATURE.fullmatch(signature), error)
        decoded = base64.urlsafe_b64decode(signature + '==')
        _require(len(decoded) == 64, error)
        Ed25519PublicKey.from_public_bytes(public_key).verify(decoded, SIGNATURE_DOMAIN + _canonical(payload))
        digest = binding_digest(expected_binding)
        _require(payload.get('protocol') == PROTOCOL and payload.get('audience') == AUDIENCE
                 and payload.get('issuer_id') == issuer_id
                 and payload.get('binding') == expected_binding
                 and payload.get('binding_digest') == digest
                 and payload.get('observation_nonce') == observation_nonce(digest), error)
        return payload
    except SupervisorRouteError:
        raise
    except Exception:
        raise error from None


class RouteJournal:
    """Dedicated durable route intent/outcome journal (Linux, single owner).

    Mirrors ``operation_journal.SQLiteOperationJournal`` durability: PilotOwner
    flock/enrollment, BEGIN IMMEDIATE, synchronous=FULL, rollback journal,
    parent fsync after commit, 250ms busy bound per wait. Any failure raises
    RouteJournalError and the transaction is rolled back.
    """

    def __init__(self, database_path, *, create, allow_ephemeral_test_storage=False):
        from engineering_hub.pilot_ownership import PilotOwner
        from .state import _private_dir, _private_file
        self._owner = None
        self._closed = True
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        try:
            _require(sys.platform == 'linux' and type(create) is bool, RouteJournalError('linux'))
            self.path = Path(database_path)
            _require(self.path.is_absolute() and '..' not in self.path.parts, RouteJournalError('path'))
            _private_dir(self.path.parent)
            self._sidecars(_private_file)
            if create:
                _require(not self.path.exists() and not self.path.is_symlink(), RouteJournalError('fresh'))
            else:
                _private_file(self.path)
                with self._connection(readonly=True) as db:
                    self._schema(db)
            self._owner = PilotOwner(self.path, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
            _require(not create or self._owner.previous_epoch is None, RouteJournalError('fresh'))
            self._closed = False
            if create:
                with self._transaction(initializing=True) as db:
                    db.execute(_INTENT)
                    db.execute(_EPOCH)
                    db.execute('PRAGMA user_version=1')
            with self._transaction() as db:
                pass
        except BaseException as error:
            if self._owner is not None:
                self._owner.close()
            self._closed = True
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise RouteJournalError('route journal unavailable') from None

    @property
    def owner_epoch(self):
        return self._owner.epoch

    @staticmethod
    def _schema(db):
        rows = db.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
        _require({name for name, kind, _ in rows if kind == 'table'} == _TABLES
                 and all(kind == 'table' for _, kind, _ in rows), RouteJournalError('schema'))
        sql = {name: text for name, _, text in rows}
        _require(sql['supervisor_route_intent'] == _INTENT and sql['supervisor_route_epoch'] == _EPOCH
                 and db.execute('PRAGMA user_version').fetchone() == (1,)
                 and db.execute('PRAGMA quick_check').fetchall() == [('ok',)], RouteJournalError('schema'))

    def _sidecars(self, private_file):
        for suffix in ('-journal', '-wal', '-shm'):
            path = self.path.with_name(self.path.name + suffix)
            if path.exists() or path.is_symlink():
                private_file(path)
                _require(suffix == '-journal', RouteJournalError('sidecar'))

    @contextmanager
    def _connection(self, *, readonly=False):
        db = sqlite3.connect(self.path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                             uri=True, timeout=_BUSY_MS / 1000, isolation_level=None)
        try:
            db.execute(f'PRAGMA busy_timeout={_BUSY_MS}')
            db.execute('PRAGMA trusted_schema=OFF')
            if not readonly:
                db.execute('PRAGMA synchronous=FULL')
                _require(db.execute('PRAGMA journal_mode=DELETE').fetchone() == ('delete',),
                         RouteJournalError('mode'))
            yield db
        finally:
            db.close()

    def _active(self):
        from .state import _private_file
        _require(not self._closed and self._owner is not None and os.getpid() == self._pid,
                 RouteJournalError('inactive'))
        self._owner.assert_active(self.path)
        self._owner._check_paths()
        self._sidecars(_private_file)

    @contextmanager
    def _transaction(self, *, initializing=False):
        _require(os.getpid() == self._pid, RouteJournalError('process'))
        with self._mutex:
            try:
                self._active()
                with self._connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        _require(self._owner._read(db) == self._owner._row, RouteJournalError('owner'))
                        if not initializing:
                            self._schema(db)
                        yield db
                        _require(self._owner._read(db) == self._owner._row, RouteJournalError('owner'))
                        db.commit()
                        os.fsync(self._owner._parent_fd)
                    except BaseException:
                        db.rollback()
                        raise
                self._active()
            except RouteJournalError:
                raise
            except SupervisorRouteRejected:
                raise
            except Exception:
                raise RouteJournalError('route journal unavailable') from None

    @staticmethod
    def _row(db, command_id):
        row = db.execute('SELECT command_id,binding_digest,binding,state,owner_epoch,error,record,response '
                         'FROM supervisor_route_intent WHERE command_id=?', (command_id,)).fetchone()
        if row is not None:
            _require(row[3] in _STATES and _HEX.fullmatch(row[1]) and (row[5] is None or row[5] in _ERRORS)
                     and (row[3] == 'completed') == (row[6] is not None), RouteJournalError('row'))
        return row

    def admit(self, command_id, digest, binding, epoch):
        """ONE transaction: replay/conflict check, epoch high-water, intent row.

        Returns ('new', None) when the caller may forward exactly once, or
        ('replay', row) for an identical binding. Raises Rejected otherwise.
        """
        with self._transaction() as db:
            row = self._row(db, command_id)
            if row is not None:
                if row[1] != digest or row[2] != _canonical(binding).decode():
                    raise SupervisorRouteRejected('binding_conflict')
                return 'replay', row
            high = db.execute('SELECT launch_counter,launch_id,boot_id FROM supervisor_route_epoch').fetchone()
            if high is not None:
                if epoch.launch_counter < high[0]:
                    raise SupervisorRouteRejected('stale_epoch')
                if epoch.launch_counter == high[0] and (epoch.launch_id, epoch.boot_id) != high[1:]:
                    raise SupervisorRouteRejected('epoch_conflict')
            if high is None or epoch.launch_counter > high[0]:
                db.execute('INSERT OR REPLACE INTO supervisor_route_epoch VALUES (1,?,?,?)',
                           (epoch.launch_counter, epoch.launch_id, epoch.boot_id))
            db.execute("INSERT INTO supervisor_route_intent VALUES (?,?,?,'intent',?,NULL,NULL,NULL)",
                       (command_id, digest, _canonical(binding).decode(), self._owner.epoch))
            return 'new', None

    def record_completed(self, command_id, digest, record, response):
        with self._transaction() as db:
            row = self._row(db, command_id)
            _require(row is not None and row[1] == digest and row[3] == 'intent', RouteJournalError('state'))
            db.execute("UPDATE supervisor_route_intent SET state='completed', record=?, response=? "
                       "WHERE command_id=? AND state='intent'", (record.decode(), response, command_id))

    def mark_unknown(self, command_id, digest, error):
        _require(error in _ERRORS, RouteJournalError('error'))
        with self._transaction() as db:
            row = self._row(db, command_id)
            _require(row is not None and row[1] == digest and row[3] in {'intent', 'unknown'},
                     RouteJournalError('state'))
            if row[3] == 'intent':
                db.execute("UPDATE supervisor_route_intent SET state='unknown', error=? WHERE command_id=?",
                           (error, command_id))

    def read(self, command_id):
        """Read-only inspection: a read transaction, no writes, no forwarding."""
        _require(os.getpid() == self._pid, RouteJournalError('process'))
        with self._mutex:
            try:
                self._active()
                with self._connection(readonly=True) as db:
                    self._schema(db)
                    return self._row(db, command_id)
            except RouteJournalError:
                raise
            except Exception:
                raise RouteJournalError('route journal unavailable') from None

    def close(self):
        self._closed = True
        if self._owner is not None:
            self._owner.close()


def _proc_start(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    suffix = raw[raw.rfind(')') + 2:].split()
    _require(len(suffix) >= 20 and suffix[0] not in {'Z', 'X'}, SupervisorRouteError('peer'))
    ticks = int(suffix[19])
    _require(ticks > 0, SupervisorRouteError('peer'))
    return ticks


class UnixSocketBackend:
    """One-shot HTTP/1.1 over an explicit Unix socket; observes SO_PEERCRED.

    The peer PID is held by pidfd across the exchange and its start time is
    rechecked, so PID reuse during the exchange is detected. No retries, no
    reconnects, no redirects. Raises on any transport uncertainty.
    """

    def __init__(self, socket_path):
        _require(sys.platform == 'linux' and hasattr(socket, 'SO_PEERCRED') and hasattr(os, 'pidfd_open'),
                 SupervisorRouteError('linux'))
        _require(type(socket_path) is str and socket_path.startswith('/')
                 and os.path.normpath(socket_path) == socket_path, SupervisorRouteError('path'))
        self._path = socket_path

    def forward(self, *, method, path, body, timeout_ms):
        deadline = time.monotonic() + timeout_ms / 1000

        def remaining():
            value = deadline - time.monotonic()
            if not value > 0:
                raise TimeoutError
            return value
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        pidfd = None
        try:
            peer.settimeout(remaining())
            peer.connect(self._path)
            pid, uid, gid = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            _require(pid > 0, SupervisorRouteError('peer'))
            before = _proc_start(pid)
            pidfd = os.pidfd_open(pid, 0)
            start = _proc_start(pid)
            _require(before == start, SupervisorRouteError('peer'))
            connection = http.client.HTTPConnection('supervisor-route', timeout=remaining())
            connection.sock = peer
            headers = {'Host': 'supervisor-route', 'Accept-Encoding': 'identity', 'Connection': 'close'}
            if method == 'POST':
                headers['Content-Type'] = 'application/json'
            connection.request(method, path, body=body if method == 'POST' else None, headers=headers)
            peer.settimeout(remaining())
            response = connection.getresponse()
            _require(response.getheader('Transfer-Encoding') is None
                     and len([k for k, _ in response.getheaders() if k.lower() == 'content-length']) == 1,
                     SupervisorRouteError('framing'))
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            _require(len(raw) <= MAX_RESPONSE_BYTES, SupervisorRouteError('size'))
            remaining()
            _require(not select.select([pidfd], [], [], 0)[0] and _proc_start(pid) == start,
                     SupervisorRouteError('peer'))
            return BackendReply(response.status, raw, ObservedPeer(pid, uid, gid, start))
        finally:
            if pidfd is not None:
                os.close(pidfd)
            peer.close()


class SupervisorRoute:
    """Explicitly enabled, supervisor-owned Hub-facing command route.

    ``epoch_source()`` must return the supervisor's CURRENT (RouteEpoch,
    BackendPin); it is consulted before admission and again after forwarding.
    ``backend.forward(method=, path=, body=, timeout_ms=)`` returns a
    BackendReply or raises. ``signing_key`` is the already-enrolled issuer
    Ed25519 key and must match ``release.public_key``; none is created here.
    Commands are serialized by one mutex; this is a foundation, not a server.
    """

    def __init__(self, *, config, release, signing_key, epoch_source, backend):
        if type(config) is not SupervisorRouteConfig or config.enabled is not True:
            raise SupervisorRouteDisabled('supervisor route is disabled')
        _require(type(release) is CurrentExecutorReleasePins and isinstance(signing_key, Ed25519PrivateKey)
                 and signing_key.public_key().public_bytes(serialization.Encoding.Raw,
                     serialization.PublicFormat.Raw) == release.public_key
                 and callable(epoch_source) and callable(getattr(backend, 'forward', None))
                 and type(config.forward_timeout_ms) is int and 100 <= config.forward_timeout_ms <= 120000
                 and type(config.journal_path) is str, SupervisorRouteError('invalid supervisor route'))
        self._pid = os.getpid()
        self._mutex = threading.Lock()
        self._release, self._signer = release, signing_key
        self._epoch_source, self._backend = epoch_source, backend
        self._timeout = config.forward_timeout_ms
        self._inflight = set()
        self._journal = RouteJournal(config.journal_path, create=config.create_journal,
                                     allow_ephemeral_test_storage=config.allow_ephemeral_test_storage)

    def _current(self):
        epoch, pin = self._epoch_source()
        _require(type(epoch) is RouteEpoch and type(pin) is BackendPin, SupervisorRouteError('epoch'))
        return epoch, pin

    def _held_from_row(self, row, *, replayed):
        command_id, digest, _, state, owner_epoch, error, record, response = row
        if state == 'completed':
            return RouteResult('completed', command_id, digest, replayed, None, record.encode(), response)
        if state == 'intent' and owner_epoch == self._journal.owner_epoch and command_id in self._inflight:
            return RouteResult('in_progress', command_id, digest, replayed)
        # An intent left by an earlier owner (restart) or whose outcome this
        # process could not record is uncertain: held, never redispatched.
        return RouteResult('held', command_id, digest, replayed, error or 'interrupted')

    def submit(self, command):
        _require(os.getpid() == self._pid, SupervisorRouteError('process'))
        _require(type(command) is RouteCommand)
        with self._mutex:
            try:
                epoch, pin = self._current()
            except Exception:
                raise SupervisorRouteRefused() from None
            if command.epoch != epoch:
                # Never journaled, never forwarded. Older counters are also
                # rejected by the durable high-water check inside admit().
                raise SupervisorRouteRejected('stale_epoch' if command.epoch.launch_counter < epoch.launch_counter
                                              else 'epoch_mismatch')
            binding = binding_for(command, pin)
            digest = binding_digest(binding)
            try:
                outcome, row = self._journal.admit(command.command_id, digest, binding, epoch)
            except SupervisorRouteRejected:
                raise
            except Exception:
                raise SupervisorRouteRefused() from None
            if outcome == 'replay':
                return self._held_from_row(row, replayed=True)
            self._inflight.add(command.command_id)
            try:
                return self._forward(command, binding, digest, epoch, pin)
            finally:
                self._inflight.discard(command.command_id)

    def _hold(self, command, digest, error):
        try:
            self._journal.mark_unknown(command.command_id, digest, error)
        except Exception:
            # The committed 'intent' row stays authoritative and reads as held.
            pass
        return RouteResult('held', command.command_id, digest, False, error)

    def _forward(self, command, binding, digest, epoch, pin):
        try:
            reply = self._backend.forward(method=command.method, path=command.path, body=command.body,
                                          timeout_ms=self._timeout)
        except Exception:
            return self._hold(command, digest, 'transport_lost')
        try:
            after = self._current()
        except Exception:
            return self._hold(command, digest, 'epoch_changed')
        if after != (epoch, pin):
            return self._hold(command, digest, 'epoch_changed')
        if type(reply) is not BackendReply or type(reply.peer) is not ObservedPeer:
            return self._hold(command, digest, 'malformed_response')
        if not reply.peer.matches(pin):
            return self._hold(command, digest, 'backend_identity_mismatch')
        try:
            _require(type(reply.status) is int and 200 <= reply.status < 500
                     and type(reply.body) is bytes)
            parsed = _strict_json(reply.body, MAX_RESPONSE_BYTES)
            _require(type(parsed) is dict)
        except Exception:
            server_error = type(reply.status) is int and 500 <= reply.status < 600
            return self._hold(command, digest, 'backend_ambiguous' if server_error else 'malformed_response')
        try:
            payload = {'protocol': PROTOCOL, 'audience': AUDIENCE, 'issuer_id': self._release.issuer_id,
                       'binding': binding, 'binding_digest': digest,
                       'observation_nonce': observation_nonce(digest),
                       'backend_status': reply.status,
                       'response_sha256': hashlib.sha256(reply.body).hexdigest(),
                       'observed_peer': {'pid': reply.peer.pid, 'uid': reply.peer.uid,
                                         'gid': reply.peer.gid, 'start_ticks': reply.peer.start_ticks},
                       'recorded_at_ms': _now_ms()}
            signature = base64.urlsafe_b64encode(self._signer.sign(SIGNATURE_DOMAIN + _canonical(payload))
                                                 ).rstrip(b'=').decode()
            record = _canonical({'payload': payload, 'signature': signature})
            _require(len(record) <= MAX_RECORD_BYTES)
        except Exception:
            return self._hold(command, digest, 'signing_failed')
        try:
            self._journal.record_completed(command.command_id, digest, record, reply.body)
        except Exception:
            # Forwarded, but not durably recorded: NEVER report success.
            return self._hold(command, digest, 'outcome_unrecorded')
        return RouteResult('completed', command.command_id, digest, False, None, record, reply.body)

    def inspect(self, command_id):
        """Read-only recorded state; never forwards, never writes."""
        _require(os.getpid() == self._pid, SupervisorRouteError('process'))
        _require(_uuid(command_id))
        try:
            row = self._journal.read(command_id)
        except Exception:
            raise SupervisorRouteRefused() from None
        return None if row is None else self._held_from_row(row, replayed=True)

    def close(self):
        self._signer = None
        self._journal.close()
