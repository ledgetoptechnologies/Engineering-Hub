"""Private, single-owner SQLite command ledger for the container-pilot adapter.

The ledger is deliberately not a daemon driver. It provides durable admission,
fencing and operation intents; ambiguity is retained as held and never replayed.

The optional single-pilot protocol relies on trusted private ledger ownership.
Its no-effect denial markers provide transactional consistency, not tamper-proof
evidence against a writer that can alter the ledger. This module does not attest
external daemon facts or exclude older executables and stale dispatchers.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from engineering_hub.container_pilot_client import (
    ContainerPilotEvidence,
    ContainerPilotRequest,
    ContainerPilotResponse,
    single_pilot_outcome_digest,
)

TARGET_ID = "engineering-hub:staging"
PHASES = (
    "deploy_container",
    "smoke_container",
    "rollback_container",
    "smoke_container_rollback",
)
_ACTION_INDEX = {action: index for index, action in enumerate(PHASES)}
_OPERATION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MOUNTINFO_PATH = Path("/proc/self/mountinfo")
_LOCAL_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "tmpfs", "overlay"}


class OwnerBusy(RuntimeError):
    """A live process already owns this ledger's kernel lock."""


class LedgerClosed(RuntimeError):
    pass


class InheritedLedgerUse(RuntimeError):
    """An inherited child-process descriptor is not an authorized ledger owner."""


class Admission:
    __slots__ = ("accepted", "replayed", "response")

    def __init__(self, accepted: bool, replayed: bool, response: ContainerPilotResponse):
        self.accepted = accepted
        self.replayed = replayed
        self.response = response


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _make_response(request: ContainerPilotRequest, *, state="running", reason=None,
                   evidence=None, replayed=False) -> ContainerPilotResponse:
    values = request.model_dump(mode="json")
    values.update(
        state=state,
        reason=reason,
        evidence=(evidence or ContainerPilotEvidence(
            container_id=None, image_config_id=None, health="unknown", evidence_verified=False
        )).model_dump(mode="json"),
        replayed=replayed,
    )
    return ContainerPilotResponse.model_validate(values)


def _mount_type_for(path: Path, mountinfo: str) -> str:
    """Resolve the longest mountpoint from Linux mountinfo (including escaped spaces)."""
    target = Path(os.path.realpath(path))
    candidates = []
    try:
        for line in mountinfo.splitlines():
            left, right = line.split(" - ", 1)
            fields = left.split()
            filesystem = right.split()[0]
            mountpoint = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
            point = Path(mountpoint)
            if os.path.commonpath((str(target), str(point))) == str(point):
                candidates.append((len(str(point)), filesystem))
    except (ValueError, IndexError, OSError):
        raise ValueError("cannot identify ledger filesystem") from None
    if not candidates:
        raise ValueError("cannot identify ledger filesystem")
    return max(candidates)[1]


class SQLiteLedger:
    """A local private SQLite ledger held under a kernel flock for its lifetime.

    The configured directory must already exist, be owned by the current uid and
    have no group/other permission bits. The database and persistent lock inode
    are never moved or unlinked by this class. SMB/network-storage admission is
    an operator deployment gate and is not inferred from path syntax.
    """

    def __init__(self, database_path, *, manifest_digest: str,
                 candidate_commit: str, policy_revision: int):
        self.path = Path(database_path)
        self._owner_pid = os.getpid()
        self._lifecycle = threading.Condition()
        self._active_connections = 0
        self._closing = False
        if not self.path.is_absolute():
            raise ValueError("ledger path must be absolute")
        self.lock_path = self.path.with_name(self.path.name + ".owner.lock")
        self._expected = (manifest_digest, candidate_commit, policy_revision)
        self._lock_fd = None
        self._closed = False
        self._check_private_directory()
        self._acquire_owner()
        try:
            self._evidence_key = self._load_evidence_key()
            self._prepare_database_file()
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _check_open(self):
        # Check the PID before touching a lock inherited from a possibly
        # multithreaded parent; the child must never wait on that mutex.
        if os.getpid() != self._owner_pid:
            raise InheritedLedgerUse("ledger authority is bound to its creating process")
        if self._closed or self._lock_fd is None:
            raise LedgerClosed("ledger is closed")

    def _check_private_directory(self):
        parent = self.path.parent
        try:
            info = parent.stat(follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode) or parent.resolve(strict=True) != parent
                    or info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise ValueError
            mountinfo = MOUNTINFO_PATH.read_text(encoding="utf-8")
            if _mount_type_for(parent, mountinfo) not in _LOCAL_FILESYSTEMS:
                raise ValueError
        except (OSError, ValueError):
            raise ValueError("ledger directory must be private and on an approved local filesystem") from None
        if self.path.name in {"", ".", ".."}:
            raise ValueError("invalid ledger filename")

    def _acquire_owner(self):
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError:
            raise ValueError("cannot open private owner lock") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError("owner lock is not a private regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OwnerBusy("container-pilot ledger owner is active") from None
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _load_evidence_key(self) -> bytes:
        path = self.path.with_name(self.path.name + ".evidence.key")
        flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        created = False
        if not path.exists() and not path.is_symlink() and self.path.exists():
            raise ValueError("operation-evidence key is missing for existing ledger")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | flags, 0o600)
            created = True
        except FileExistsError:
            try:
                fd = os.open(path, os.O_RDONLY | flags)
            except OSError:
                raise ValueError("private operation-evidence key is unavailable") from None
        except OSError:
            raise ValueError("cannot create private operation-evidence key") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError("operation-evidence key is not private")
            if created:
                key = secrets.token_bytes(32)
                offset = 0
                while offset < len(key):
                    offset += os.write(fd, key[offset:])
                os.fsync(fd)
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                return key
            key = os.read(fd, 33)
            if len(key) != 32:
                raise ValueError("operation-evidence key has invalid length")
            return key
        finally:
            os.close(fd)

    def _prepare_database_file(self):
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError:
            try:
                info = self.path.stat(follow_symlinks=False)
            except OSError:
                raise ValueError("ledger database is unavailable") from None
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError("ledger database is not a private regular file")
            return
        except OSError:
            raise ValueError("cannot create private ledger database") from None
        else:
            os.close(fd)

    @contextmanager
    def _connection(self):
        self._check_open()
        with self._lifecycle:
            if self._closing or self._closed:
                raise LedgerClosed("ledger is closing or closed")
            self._active_connections += 1
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                with self._lifecycle:
                    self._active_connections -= 1
                    self._lifecycle.notify_all()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize(self):
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 2}:
                raise ValueError("unsupported container-pilot ledger version")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("BEGIN IMMEDIATE")
            try:
                statements = (
                    "CREATE TABLE IF NOT EXISTS commands (command_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, target_id TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, response TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, fence INTEGER NOT NULL, manifest_digest TEXT NOT NULL, candidate_commit TEXT NOT NULL, policy_revision INTEGER NOT NULL, state TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS targets (target_id TEXT PRIMARY KEY, fence INTEGER NOT NULL, current_run_id TEXT NOT NULL, state TEXT NOT NULL, manifest_digest TEXT NOT NULL, container_id TEXT, image_config_id TEXT, generation_id TEXT)",
                    "CREATE TABLE IF NOT EXISTS phases (run_id TEXT NOT NULL, action TEXT NOT NULL, command_id TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY (run_id, action), UNIQUE (command_id))",
                    "CREATE TABLE IF NOT EXISTS operations (operation_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, intent TEXT NOT NULL, receipt TEXT, receipt_mac TEXT, UNIQUE (command_id, kind))",
                    "CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                )
                for statement in statements:
                    db.execute(statement)
                db.execute("PRAGMA user_version=2")
                self._recover_orphans(db)
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _recover_orphans(self, db):
        rows = db.execute("SELECT command_id, response FROM commands WHERE state='running'").fetchall()
        for row in rows:
            data = json.loads(row["response"])
            data.update(state="held", reason="recovery_required", replayed=False)
            db.execute("UPDATE commands SET state='held', response=? WHERE command_id=?",
                       (_canonical(data), row["command_id"]))
            db.execute("UPDATE phases SET state='held' WHERE command_id=?", (row["command_id"],))
            run_id = db.execute("SELECT run_id FROM commands WHERE command_id=?",
                                (row["command_id"],)).fetchone()[0]
            db.execute("UPDATE runs SET state='held' WHERE run_id=?", (run_id,))
            db.execute("UPDATE targets SET state='held' WHERE current_run_id=?", (run_id,))

    def admit(self, request: ContainerPilotRequest) -> Admission:
        self._check_open()
        if type(request) is not ContainerPilotRequest:
            raise TypeError("exact shared ContainerPilotRequest required")
        payload = _canonical(request.model_dump(mode="json"))
        digest, commit, policy = self._expected
        with self._transaction() as db:
            mode = db.execute("SELECT value FROM control WHERE key='single_pilot_protocol'").fetchone()
            if mode is not None:
                if mode['value'] != 'v1':
                    raise PermissionError('single pilot protocol is invalid')
                denial = self._single_pilot_denial(db, request.command_id)
                if denial is not None:
                    original_request, original_response = denial
                    if original_request != json.loads(payload):
                        return Admission(False, False, _make_response(request, state='held', reason='identity_conflict'))
                    return Admission(False, True, original_response.model_copy(update={'replayed': True}))
                ack_row = db.execute('SELECT value FROM control WHERE key=?',
                                     ('single_pilot_ack:' + request.command_id,)).fetchone()
                if ack_row is not None:
                    ack = json.loads(ack_row['value'])
                    if ack.get('request') != json.loads(payload):
                        return Admission(False, False, _make_response(request, state='held', reason='identity_conflict'))
                    original = ContainerPilotResponse.model_validate(ack['outcome'])
                    if ack.get('outcome_digest') != single_pilot_outcome_digest(original):
                        raise PermissionError('single pilot acknowledgement is invalid')
                    return Admission(False, True, original.model_copy(update={'replayed':True}))
                gate_row = db.execute("SELECT value FROM control WHERE key='single_pilot_gate'").fetchone()
                gate = json.loads(gate_row['value']) if gate_row else None
                if gate is None or gate.get('request') != json.loads(payload):
                    return Admission(False, False, _make_response(request, state='held', reason='recovery_required'))
                if gate.get('state') == 'awaiting_ack':
                    stored = ContainerPilotResponse.model_validate(gate['outcome'])
                    return Admission(False, True, stored.model_copy(update={'replayed':True}))
                if gate.get('state') != 'effect_held':
                    raise PermissionError('single pilot gate is invalid')
            old = db.execute("SELECT payload,response FROM commands WHERE command_id=?",
                             (request.command_id,)).fetchone()
            if old:
                if old["payload"] != payload:
                    return Admission(False, False, _make_response(request, state="held", reason="identity_conflict"))
                response = ContainerPilotResponse.model_validate_json(old["response"])
                if mode is not None:
                    # No unverified in-progress or terminal result is replayed.
                    return Admission(False, False, _make_response(request, state='held', reason='recovery_required'))
                response = response.model_copy(update={"replayed": True})
                return Admission(False, True, response)

            if request.manifest_digest != digest:
                return self._store_denial(db, request, payload, "manifest_mismatch")
            if request.candidate_commit != commit:
                return self._store_denial(db, request, payload, "manifest_mismatch")
            if request.policy_revision != policy:
                return self._store_denial(db, request, payload, "policy_mismatch")

            target = db.execute("SELECT * FROM targets WHERE target_id=?", (request.target_id,)).fetchone()
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (request.run_id,)).fetchone()
            if run:
                binding = (run["target_id"], run["fence"], run["manifest_digest"],
                           run["candidate_commit"], run["policy_revision"])
                requested = (request.target_id, request.fencing_generation, request.manifest_digest,
                             request.candidate_commit, request.policy_revision)
                if binding != requested:
                    return self._store_denial(db, request, payload, "identity_conflict")
                if run["state"] == "held":
                    return self._store_denial(db, request, payload, "recovery_required")
                if target and target["current_run_id"] != request.run_id and target["state"] != "active":
                    return self._store_denial(db, request, payload, "target_busy")
                existing_phase = db.execute("SELECT state FROM phases WHERE run_id=? AND action=?",
                                            (request.run_id, request.action)).fetchone()
                if existing_phase:
                    return self._store_denial(db, request, payload, "identity_conflict")
                denial = self._phase_denial(db, request)
                if denial:
                    return self._store_denial(db, request, payload, denial)
            else:
                if request.action != "deploy_container":
                    return self._store_denial(db, request, payload, "identity_conflict")
                if target:
                    if request.fencing_generation < target["fence"]:
                        return self._store_denial(db, request, payload, "stale_fence")
                    if request.fencing_generation == target["fence"]:
                        return self._store_denial(db, request, payload, "identity_conflict")
                    if target["state"] != "active":
                        return self._store_denial(db, request, payload, "target_busy")

            if run:
                db.execute("UPDATE runs SET state='running' WHERE run_id=?", (request.run_id,))
                db.execute("UPDATE targets SET state='running' WHERE target_id=? AND current_run_id=?",
                           (request.target_id, request.run_id))
            response = _make_response(request)
            response_json = _canonical(response.model_dump(mode="json"))
            if not run:
                db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                           (request.run_id, request.target_id, request.fencing_generation,
                            request.manifest_digest, request.candidate_commit, request.policy_revision, "running"))
                db.execute("INSERT INTO targets(target_id,fence,current_run_id,state,manifest_digest) "
                           "VALUES (?,?,?,?,?) ON CONFLICT(target_id) DO UPDATE SET "
                           "fence=excluded.fence,current_run_id=excluded.current_run_id,state='running',"
                           "manifest_digest=excluded.manifest_digest",
                           (request.target_id, request.fencing_generation, request.run_id,
                            "running", request.manifest_digest))
            db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?)",
                       (request.command_id, request.run_id, request.target_id, request.action,
                        payload, "running", response_json))
            db.execute("INSERT INTO phases VALUES (?,?,?,'running')",
                       (request.run_id, request.action, request.command_id))
            return Admission(True, False, response)

    def _store_denial(self, db, request, payload, reason: str) -> Admission:
        response = _make_response(request, state="held", reason=reason)
        db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?)",
                   (request.command_id, request.run_id, request.target_id, request.action,
                    payload, "held", _canonical(response.model_dump(mode="json"))))
        # Admission and this denial are in the same write transaction. Only
        # the exact freshly rejected request may release its gate, and only
        # when no operation intent exists for it. A crash rolls both changes
        # back; a previously admitted or ambiguous command never reaches here.
        gate = _canonical({'state': 'effect_held', 'request': request.model_dump(mode='json')})
        marker = _canonical({'request': request.model_dump(mode='json'), 'response': response.model_dump(mode='json')})
        inserted = db.execute("INSERT INTO control(key,value) SELECT ?,? WHERE "
                   "EXISTS (SELECT 1 FROM control WHERE key='single_pilot_gate' AND value=?) "
                   "AND EXISTS (SELECT 1 FROM control WHERE key='single_pilot_protocol' AND value='v1') "
                   "AND NOT EXISTS (SELECT 1 FROM operations WHERE command_id=?)",
                   ('single_pilot_denial:' + request.command_id, marker, gate, request.command_id))
        if inserted.rowcount:
            db.execute("DELETE FROM control WHERE key='single_pilot_gate' AND value=?", (gate,))
        return Admission(False, False, response)

    @staticmethod
    def _single_pilot_denial(db, command_id):
        marker = db.execute('SELECT value FROM control WHERE key=?', ('single_pilot_denial:' + command_id,)).fetchone()
        if marker is None:
            return None
        row = db.execute('SELECT payload,response,state FROM commands WHERE command_id=?', (command_id,)).fetchone()
        try:
            value = json.loads(marker['value'])
            if (type(value) is not dict or set(value) != {'request', 'response'} or row is None
                    or row['state'] != 'held' or _canonical(value['request']) != row['payload']
                    or _canonical(value['response']) != row['response']
                    or db.execute('SELECT 1 FROM operations WHERE command_id=?', (command_id,)).fetchone()):
                raise ValueError()
            request = ContainerPilotRequest.model_validate(value['request'])
            response = ContainerPilotResponse.model_validate(value['response'])
            if (request.command_id != command_id or response.state != 'held' or response.replayed
                    or response.model_dump(include=set(ContainerPilotRequest.model_fields)) != request.model_dump()):
                raise ValueError()
            return value['request'], response
        except Exception:
            raise PermissionError('single pilot denial evidence is invalid') from None

    @staticmethod
    def _phase_denial(db, request) -> str | None:
        index = _ACTION_INDEX[request.action]
        prior = db.execute("SELECT action,state FROM phases WHERE run_id=?",
                           (request.run_id,)).fetchall()
        found = {row["action"]: row["state"] for row in prior}
        if index == 0:
            return "identity_conflict"
        if index == 1:
            if found.get(PHASES[0]) == "running":
                return "target_busy"
            return None if found.get(PHASES[0]) == "succeeded" else "identity_conflict"
        if index == 2:
            if found.get(PHASES[0]) == "running" or found.get(PHASES[1]) == "running":
                return "target_busy"
            return None if "failed" in (found.get(PHASES[0]), found.get(PHASES[1])) else "identity_conflict"
        if found.get(PHASES[2]) == "running":
            return "target_busy"
        return None if found.get(PHASES[2]) == "succeeded" else "identity_conflict"

    def begin_operation(self, command_id: str, kind: str) -> str:
        self._check_open()
        if not isinstance(kind, str) or not _OPERATION_NAME.fullmatch(kind):
            raise ValueError("invalid operation kind")
        with self._transaction() as db:
            row = db.execute("SELECT run_id,target_id,state,payload FROM commands WHERE command_id=?",
                             (command_id,)).fetchone()
            if row is None or row["state"] != "running":
                raise PermissionError("command is not effect-authorized")
            mode = db.execute("SELECT value FROM control WHERE key='single_pilot_protocol'").fetchone()
            if mode is not None:
                gate_row = db.execute("SELECT value FROM control WHERE key='single_pilot_gate'").fetchone()
                gate = json.loads(gate_row['value']) if gate_row else None
                if (mode['value'] != 'v1' or gate is None or gate.get('state') != 'effect_held'
                        or gate.get('request') != json.loads(row['payload'])):
                    raise PermissionError('single pilot gate does not authorize effect')
            command = json.loads(row["payload"])
            pause = db.execute("SELECT value FROM control WHERE key='paused'").fetchone()
            if pause and pause["value"] == "true":
                raise PermissionError("paused")
            target = db.execute("SELECT fence,current_run_id,state FROM targets WHERE target_id=?",
                                (row["target_id"],)).fetchone()
            if (target is None or target["fence"] != command["fencing_generation"]
                    or target["current_run_id"] != row["run_id"] or target["state"] != "running"):
                raise RuntimeError("fence or target reservation changed")
            operation_id = str(uuid.uuid5(uuid.UUID(command_id), kind))
            intent = _canonical({"operation_id": operation_id, "kind": kind,
                                 "command_id": command_id, "run_id": row["run_id"],
                                 "fencing_generation": command["fencing_generation"]})
            try:
                db.execute("INSERT INTO operations VALUES (?,?,?,'intent',?,NULL,NULL)",
                           (operation_id, command_id, kind, intent))
            except sqlite3.IntegrityError:
                raise RuntimeError("operation already reserved; never repeat") from None
            return operation_id

    def _operation_mac(self, operation_id: str, command_id: str, kind: str,
                       status: str, receipt: str) -> str:
        value = _canonical({"operation_id": operation_id, "command_id": command_id,
                            "kind": kind, "status": status, "receipt": receipt}).encode("utf-8")
        return hmac.new(self._evidence_key, value, hashlib.sha256).hexdigest()

    def complete_operation(self, operation_id: str, receipt) -> None:
        encoded = _canonical(receipt)
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ValueError("private operation receipt exceeds limit")
        with self._transaction() as db:
            row = db.execute("SELECT command_id,kind,status FROM operations WHERE operation_id=?",
                             (operation_id,)).fetchone()
            if row is None or row["status"] != "intent":
                raise RuntimeError("operation intent missing or already completed")
            mac = self._operation_mac(operation_id, row["command_id"], row["kind"],
                                      "completed", encoded)
            cursor = db.execute("UPDATE operations SET status='completed',receipt=?,receipt_mac=? "
                                "WHERE operation_id=? AND status='intent'",
                                (encoded, mac, operation_id))
            if cursor.rowcount != 1:
                raise RuntimeError("operation intent missing or already completed")

    def record_target_instance(self, command_id: str, container_id: str,
                               image_config_id: str, generation_id: str) -> None:
        """Persist independently inspected instance identity before later effects."""
        if (not re.fullmatch(r"[0-9a-f]{64}", container_id)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_config_id)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", generation_id)):
            raise ValueError("invalid verified instance identity")
        with self._transaction() as db:
            command = db.execute("SELECT run_id,target_id,state,payload FROM commands WHERE command_id=?",
                                 (command_id,)).fetchone()
            if command is None or command["state"] != "running":
                raise PermissionError("command is not running")
            payload = json.loads(command["payload"])
            if payload["action"] not in {"deploy_container", "rollback_container"}:
                raise PermissionError("phase cannot replace target instance")
            target = db.execute("SELECT fence,current_run_id,state FROM targets WHERE target_id=?",
                                (command["target_id"],)).fetchone()
            if (target is None or target["fence"] != payload["fencing_generation"]
                    or target["current_run_id"] != command["run_id"] or target["state"] != "running"):
                raise RuntimeError("fence or target reservation changed")
            db.execute("UPDATE targets SET container_id=?,image_config_id=?,generation_id=? "
                       "WHERE target_id=? AND current_run_id=?",
                       (container_id, image_config_id, generation_id,
                        command["target_id"], command["run_id"]))

    def record_terminal_failure(self, operation_id: str) -> None:
        """Persist a read-only inspected terminal result; never resubmit its effect."""
        receipt = _canonical({"terminal": True})
        with self._transaction() as db:
            row = db.execute("SELECT command_id,kind,status FROM operations WHERE operation_id=?",
                             (operation_id,)).fetchone()
            if row is None or row["status"] != "intent":
                raise RuntimeError("operation intent missing or already resolved")
            mac = self._operation_mac(operation_id, row["command_id"], row["kind"],
                                      "terminal_failed", receipt)
            cursor = db.execute(
                "UPDATE operations SET status='terminal_failed',receipt=?,receipt_mac=? "
                "WHERE operation_id=? AND status='intent'",
                (receipt, mac, operation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("operation intent missing or already resolved")

    def resolve_terminal_failure(self, command_id: str) -> ContainerPilotResponse:
        """Release a held command only after its private op record is terminal-failed."""
        with self._transaction() as db:
            row = db.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()
            if row is None or row["state"] != "held":
                raise RuntimeError("command is not held")
            failures = db.execute(
                "SELECT operation_id,command_id,kind,status,intent,receipt,receipt_mac "
                "FROM operations WHERE command_id=? AND status='terminal_failed' ORDER BY operation_id",
                (command_id,),
            ).fetchall()
            authenticated_failure = False
            for failure in failures:
                operation = self._format_operation(failure)
                if (operation["status"] == "terminal_failed"
                        and operation["receipt_authentic"] is True
                        and operation["receipt"] == {"terminal": True}):
                    authenticated_failure = True
                    break
            if not authenticated_failure:
                raise PermissionError("authenticated terminal failure evidence is required")
            old = ContainerPilotResponse.model_validate_json(row["response"])
            result = old.model_copy(update={"state": "failed", "reason": "effect_failed",
                                            "replayed": False})
            db.execute("UPDATE commands SET state='failed',response=? WHERE command_id=?",
                       (_canonical(result.model_dump(mode="json")), command_id))
            db.execute("UPDATE phases SET state='failed' WHERE command_id=?", (command_id,))
            db.execute("UPDATE runs SET state='failed' WHERE run_id=?", (row["run_id"],))
            db.execute("UPDATE targets SET state='failed' WHERE target_id=? AND current_run_id=?",
                       (row["target_id"], row["run_id"]))
            return result

    def finish(self, command_id: str, *, state: str, reason: str | None,
               evidence: ContainerPilotEvidence, generation_id: str | None = None) -> ContainerPilotResponse:
        if state not in {"succeeded", "failed", "held"}:
            raise ValueError("invalid terminal command state")
        if type(evidence) is not ContainerPilotEvidence:
            raise TypeError("exact shared ContainerPilotEvidence required")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()
            if row is None or row["state"] != "running":
                raise RuntimeError("command is not running")
            old = ContainerPilotResponse.model_validate_json(row["response"])
            result = old.model_copy(update={"state": state, "reason": reason,
                                            "evidence": evidence, "replayed": False})
            response_json = _canonical(result.model_dump(mode="json"))
            db.execute("UPDATE commands SET state=?,response=? WHERE command_id=?",
                       (state, response_json, command_id))
            db.execute("UPDATE phases SET state=? WHERE command_id=?", (state, command_id))
            run_state = "running"
            target_state = "running"
            if state == "held":
                run_state = target_state = "held"
            elif state == "failed":
                run_state = target_state = "failed"
            elif old.action == "smoke_container" or old.action == "smoke_container_rollback":
                run_state = target_state = "active"
            db.execute("UPDATE runs SET state=? WHERE run_id=?", (run_state, row["run_id"]))
            db.execute("UPDATE targets SET state=? WHERE target_id=? AND current_run_id=?",
                       (target_state, row["target_id"], row["run_id"]))
            if (state == "succeeded" and old.action in {"deploy_container", "rollback_container"}
                    and evidence.container_id and evidence.image_config_id):
                db.execute("UPDATE targets SET container_id=?,image_config_id=?,generation_id=COALESCE(?,generation_id) "
                           "WHERE target_id=? AND current_run_id=?",
                           (evidence.container_id, evidence.image_config_id, generation_id,
                            row["target_id"], row["run_id"]))
            return result

    def get(self, command_id: str) -> ContainerPilotResponse | None:
        self._check_open()
        try:
            if str(uuid.UUID(command_id)) != command_id:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        with self._connection() as db:
            db.execute('BEGIN')
            row = db.execute("SELECT response FROM commands WHERE command_id=?", (command_id,)).fetchone()
            if row is None:
                return None
            raw = ContainerPilotResponse.model_validate_json(row['response'])
            mode = db.execute("SELECT value FROM control WHERE key='single_pilot_protocol'").fetchone()
            if mode is None:
                return raw
            if mode['value'] != 'v1':
                raise PermissionError('single pilot protocol is invalid')
            denial = self._single_pilot_denial(db, command_id)
            if denial is not None:
                return denial[1]
            ack = db.execute('SELECT value FROM control WHERE key=?', ('single_pilot_ack:' + command_id,)).fetchone()
            if ack:
                record = json.loads(ack['value'])
                outcome = ContainerPilotResponse.model_validate(record['outcome'])
                if record.get('outcome_digest') != single_pilot_outcome_digest(outcome):
                    raise PermissionError('single pilot acknowledgement is invalid')
                return outcome
            gate_row = db.execute("SELECT value FROM control WHERE key='single_pilot_gate'").fetchone()
            gate = json.loads(gate_row['value']) if gate_row else None
            if (gate and gate.get('state') == 'awaiting_ack'
                    and gate.get('request', {}).get('command_id') == command_id):
                return ContainerPilotResponse.model_validate(gate['outcome'])
            request = ContainerPilotRequest.model_validate(raw.model_dump(include=set(ContainerPilotRequest.model_fields)))
            return _make_response(request, state='held', reason='recovery_required')

    def single_pilot_enabled(self) -> bool:
        with self._connection() as db:
            row = db.execute("SELECT value FROM control WHERE key='single_pilot_protocol'").fetchone()
        if row is None:
            return False
        if row['value'] != 'v1':
            raise PermissionError('single pilot protocol is invalid')
        return True

    def _format_operation(self, row) -> dict:
        status = row["status"]
        receipt = None
        authentic = None
        if status in {"completed", "terminal_failed"}:
            raw = row["receipt"]
            supplied = row["receipt_mac"]
            try:
                expected = self._operation_mac(row["operation_id"], row["command_id"],
                                               row["kind"], status, raw)
                parsed = json.loads(raw)
                valid = (_canonical(parsed) == raw and isinstance(supplied, str)
                         and hmac.compare_digest(supplied, expected))
            except (TypeError, ValueError, RecursionError):
                valid = False
                parsed = None
            if valid:
                receipt = parsed
                authentic = True
            else:
                status = "invalid"
                authentic = False
        elif status != "intent":
            status = "invalid"
            authentic = False
        return {"operation_id": row["operation_id"], "command_id": row["command_id"],
                "kind": row["kind"], "status": status,
                "intent": json.loads(row["intent"]), "receipt": receipt,
                "receipt_authentic": authentic}

    def reconcile_snapshot(self, command_id: str) -> dict | None:
        """Return durable authenticated facts only; never mutates rows."""
        self._check_open()
        try:
            if str(uuid.UUID(command_id)) != command_id:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        with self._connection() as db:
            row = db.execute("SELECT response FROM commands WHERE command_id=?", (command_id,)).fetchone()
            if not row:
                return None
            operations = db.execute("SELECT operation_id,command_id,kind,status,intent,receipt,receipt_mac "
                                    "FROM operations WHERE command_id=? ORDER BY kind",
                                    (command_id,)).fetchall()
        return {"response": json.loads(row["response"]),
                "operations": tuple(self._format_operation(op) for op in operations)}

    def operation_rows(self, command_id: str) -> tuple[dict, ...]:
        self._check_open()
        with self._connection() as db:
            rows = db.execute("SELECT operation_id,command_id,kind,status,intent,receipt,receipt_mac "
                              "FROM operations WHERE command_id=? ORDER BY kind", (command_id,)).fetchall()
        return tuple(self._format_operation(row) for row in rows)

    def enable_single_pilot_protocol(self) -> None:
        """Explicitly enroll a fresh ledger; this does not activate a controller.

        An older executable ignoring this control state can bypass it; an
        externally enforced exclusive owner and version enrollment are required.
        """
        with self._transaction() as db:
            if (db.execute("SELECT 1 FROM control WHERE key='single_pilot_protocol'").fetchone()
                    or db.execute('SELECT 1 FROM commands LIMIT 1').fetchone()
                    or any(db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone()
                           for table in ('operations','runs','targets','phases','control'))):
                raise PermissionError('fresh single pilot ledger required')
            db.execute("INSERT INTO control(key,value) VALUES('single_pilot_protocol','v1')")

    def arm_single_pilot(self, request: ContainerPilotRequest) -> None:
        """Durably hold one exact request before admission; never idempotently re-arm."""
        if type(request) is not ContainerPilotRequest:
            raise TypeError('exact pilot request required')
        if (request.manifest_digest, request.candidate_commit, request.policy_revision) != self._expected:
            raise PermissionError('pilot enrollment mismatch')
        gate = _canonical({'state':'effect_held', 'request':request.model_dump(mode='json')})
        with self._transaction() as db:
            mode = db.execute("SELECT value FROM control WHERE key='single_pilot_protocol'").fetchone()
            if mode is None or mode['value'] != 'v1':
                raise PermissionError('single pilot protocol not enabled')
            if (db.execute("SELECT 1 FROM control WHERE key='single_pilot_gate'").fetchone()
                    or db.execute('SELECT 1 FROM commands WHERE command_id=?', (request.command_id,)).fetchone()
                    or db.execute('SELECT 1 FROM control WHERE key=?', ('single_pilot_ack:'+request.command_id,)).fetchone()):
                raise PermissionError('single pilot already held or identity used')
            db.execute("INSERT INTO control(key,value) VALUES('single_pilot_gate',?)", (gate,))

    def record_single_pilot_verified_outcome(self, request: ContainerPilotRequest,
                                              outcome: ContainerPilotResponse, *, candidate_stop: bool = False) -> None:
        """Store caller-verified outcome; caller must verify all external evidence.

        This only binds the exact stored terminal response and authenticated
        ledger receipts. It does not verify daemon generation, journal, state
        reservation, profile, probes or exclusive ownership. The opted-in adapter
        calls it after its own evidence checks; those checks remain necessary.
        """
        if type(request) is not ContainerPilotRequest or type(outcome) is not ContainerPilotResponse:
            raise TypeError('exact pilot identities required')
        if outcome.model_dump(include=set(ContainerPilotRequest.model_fields)) != request.model_dump():
            raise PermissionError('pilot outcome identity mismatch')
        if type(candidate_stop) is not bool or (candidate_stop and request.action != 'rollback_container'):
            raise ValueError('invalid candidate stop policy')
        deploy = request.action in {'deploy_container','rollback_container'}
        eligible = ((deploy and outcome.state == 'succeeded' and outcome.reason is None
                     and outcome.evidence.health == 'unknown' and outcome.evidence.evidence_verified is True)
                    or (not deploy and outcome.evidence.evidence_verified is True
                        and ((outcome.state == 'succeeded' and outcome.reason is None and outcome.evidence.health == 'healthy')
                             or (outcome.state == 'failed' and outcome.reason == 'health_failed' and outcome.evidence.health == 'unhealthy'))))
        if not eligible or outcome.replayed:
            raise PermissionError('pilot outcome not eligible')
        role = 'rollback' if request.action in {'rollback_container','smoke_container_rollback'} else 'candidate'
        expected = ({'state_copy' if role == 'candidate' else 'rollback_state', 'create_'+role, 'start_'+role}
                    if request.action in {'deploy_container','rollback_container'} else {'probe'})
        if candidate_stop:
            expected.add('stop')
        gate_text = _canonical({'state':'effect_held', 'request':request.model_dump(mode='json')})
        next_text = _canonical({'state':'awaiting_ack', 'request':request.model_dump(mode='json'),
                                'outcome':outcome.model_dump(mode='json')})
        with self._transaction() as db:
            gate = db.execute("SELECT value FROM control WHERE key='single_pilot_gate'").fetchone()
            row = db.execute('SELECT payload,response FROM commands WHERE command_id=?', (request.command_id,)).fetchone()
            if gate is not None and gate['value'] == next_text:
                return  # Exact retry after an ambiguous commit is not an effect.
            if gate is None or gate['value'] != gate_text or row is None or row['payload'] != _canonical(request.model_dump(mode='json')) or row['response'] != _canonical(outcome.model_dump(mode='json')):
                raise PermissionError('pilot outcome does not match held command')
            records = db.execute("SELECT operation_id,command_id,kind,status,intent,receipt,receipt_mac FROM operations WHERE command_id=?", (request.command_id,)).fetchall()
            parsed = [self._format_operation(record) for record in records]
            if (len(parsed) != len(expected) or {item['kind'] for item in parsed} != expected
                    or any(item['status'] != 'completed' or item['receipt_authentic'] is not True
                           or item['operation_id'] != str(uuid.uuid5(uuid.UUID(request.command_id), item['kind'])) for item in parsed)):
                raise PermissionError('pilot ledger evidence incomplete')
            if candidate_stop:
                receipt = next(item['receipt'] for item in parsed if item['kind'] == 'stop')
                if (type(receipt) is not dict or set(receipt) != {'container_id', 'stopped'}
                        or type(receipt['container_id']) is not str
                        or not re.fullmatch(r'[a-f0-9]{64}', receipt['container_id'])
                        or receipt['container_id'] == outcome.evidence.container_id
                        or receipt['stopped'] is not True):
                    raise PermissionError('candidate stop receipt mismatch')
            db.execute("UPDATE control SET value=? WHERE key='single_pilot_gate'", (next_text,))

    def acknowledge_single_pilot(self, request: ContainerPilotRequest, outcome_digest: str) -> None:
        """Ack an exact outcome digest; exact repeats do not clear a later gate.

        No caller authentication is supplied here. The controller must durably
        save the exact request and outcome before sending authenticated ACK.
        """
        if type(request) is not ContainerPilotRequest or type(outcome_digest) is not str:
            raise TypeError('exact pilot request and outcome digest required')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', outcome_digest):
            raise ValueError('invalid pilot outcome digest')
        key = 'single_pilot_ack:' + request.command_id
        with self._transaction() as db:
            previous = db.execute('SELECT value FROM control WHERE key=?', (key,)).fetchone()
            if previous is not None:
                record = json.loads(previous['value'])
                if record.get('request') != request.model_dump(mode='json') or record.get('outcome_digest') != outcome_digest:
                    raise PermissionError('pilot acknowledgment identity conflict')
                return
            gate_row = db.execute("SELECT value FROM control WHERE key='single_pilot_gate'").fetchone()
            gate = json.loads(gate_row['value']) if gate_row else None
            if (type(gate) is not dict or gate.get('state') != 'awaiting_ack'
                    or gate.get('request') != request.model_dump(mode='json')):
                raise PermissionError('pilot acknowledgment identity conflict')
            outcome = ContainerPilotResponse.model_validate(gate.get('outcome'))
            if single_pilot_outcome_digest(outcome) != outcome_digest:
                raise PermissionError('pilot acknowledgment identity conflict')
            record = _canonical({'request':request.model_dump(mode='json'),
                                 'outcome':outcome.model_dump(mode='json'), 'outcome_digest':outcome_digest})
            db.execute('INSERT INTO control(key,value) VALUES(?,?)', (key, record))
            db.execute("DELETE FROM control WHERE key='single_pilot_gate'")

    def inspect_single_pilot(self):
        """Inspect persisted gate only; absence is not proof of no effect."""
        with self._connection() as db:
            row = db.execute("SELECT value FROM control WHERE key='single_pilot_gate'").fetchone()
        return None if row is None else json.loads(row['value'])

    def set_paused(self, paused: bool) -> None:
        if type(paused) is not bool:
            raise ValueError("pause state requires a JSON boolean")
        with self._transaction() as db:
            db.execute("INSERT INTO control(key,value) VALUES('paused',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       ("true" if paused else "false",))

    def is_paused(self) -> bool:
        self._check_open()
        with self._connection() as db:
            row = db.execute("SELECT value FROM control WHERE key='paused'").fetchone()
        return bool(row and row["value"] == "true")

    def target_snapshot(self, target_id: str = TARGET_ID) -> dict | None:
        self._check_open()
        with self._connection() as db:
            row = db.execute("SELECT * FROM targets WHERE target_id=?", (target_id,)).fetchone()
        return dict(row) if row else None

    def close(self):
        # flock is associated with the shared open-file description. A forked
        # child closes only its descriptor copy; LOCK_UN would release the
        # parent's still-active owner lock.
        if os.getpid() != self._owner_pid:
            fd, self._lock_fd = self._lock_fd, None
            self._closed = True
            if fd is not None:
                os.close(fd)
            return
        with self._lifecycle:
            if self._closed:
                return
            self._closing = True
            while self._active_connections:
                self._lifecycle.wait()
            self._closed = True
            fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *_exc):
        self.close()
