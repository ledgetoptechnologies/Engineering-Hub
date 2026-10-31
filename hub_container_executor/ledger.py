"""Private, single-owner SQLite command ledger for the container-pilot adapter.

The ledger is deliberately not a daemon driver. It provides durable admission,
fencing and operation intents; ambiguity is retained as held and never replayed.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from engineering_hub.container_pilot_client import (
    ContainerPilotEvidence,
    ContainerPilotRequest,
    ContainerPilotResponse,
)

from .backend import EffectiveProfile

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
                    "CREATE TABLE IF NOT EXISTS create_contexts (operation_id TEXT PRIMARY KEY, context TEXT NOT NULL, context_mac TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS mount_gate_challenges (operation_id TEXT PRIMARY KEY, launch_id TEXT NOT NULL UNIQUE, nonce TEXT NOT NULL UNIQUE, identity TEXT NOT NULL, identity_mac TEXT NOT NULL, state TEXT NOT NULL, decision TEXT, decision_mac TEXT)",
                    "CREATE TABLE IF NOT EXISTS gate_required (command_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, state_mac TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS gate_acceptances (operation_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, record TEXT NOT NULL, record_mac TEXT NOT NULL)",
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
            old = db.execute("SELECT payload,response FROM commands WHERE command_id=?",
                             (request.command_id,)).fetchone()
            if old:
                if old["payload"] != payload:
                    return Admission(False, False, _make_response(request, state="held", reason="identity_conflict"))
                response = ContainerPilotResponse.model_validate_json(old["response"])
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
        return Admission(False, False, response)

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

    def begin_operation(self, command_id: str, kind: str, *, create_context=None) -> str:
        self._check_open()
        if not isinstance(kind, str) or not _OPERATION_NAME.fullmatch(kind):
            raise ValueError("invalid operation kind")
        if create_context is not None:
            if kind not in {"create_candidate", "create_rollback"} or type(create_context) is not dict:
                raise ValueError("invalid create context")
            encoded_context = _canonical(create_context)
            if len(encoded_context.encode("utf-8")) > 16384:
                raise ValueError("create context exceeds limit")
        else:
            encoded_context = None
        with self._transaction() as db:
            row = db.execute("SELECT run_id,target_id,state,payload FROM commands WHERE command_id=?",
                             (command_id,)).fetchone()
            if row is None or row["state"] != "running":
                raise PermissionError("command is not effect-authorized")
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
                if encoded_context is not None:
                    mac = self._operation_mac(operation_id, command_id, kind,
                                              "create_context", encoded_context)
                    db.execute("INSERT INTO create_contexts VALUES (?,?,?)",
                               (operation_id, encoded_context, mac))
            except sqlite3.IntegrityError:
                raise RuntimeError("operation already reserved; never repeat") from None
            return operation_id

    def _operation_mac(self, operation_id: str, command_id: str, kind: str,
                       status: str, receipt: str) -> str:
        value = _canonical({"operation_id": operation_id, "command_id": command_id,
                            "kind": kind, "status": status, "receipt": receipt}).encode("utf-8")
        return hmac.new(self._evidence_key, value, hashlib.sha256).hexdigest()

    def profile_commitment(self, profile: EffectiveProfile) -> str:
        """Bind an exact private profile without persisting its secret environment."""
        self._check_open()
        if type(profile) is not EffectiveProfile:
            raise ValueError("exact effective profile required")
        raw = _canonical(asdict(profile)).encode("utf-8")
        return hmac.new(self._evidence_key,
                        b"engineering-hub.create-profile-commitment.v1\x00" + raw,
                        hashlib.sha256).hexdigest()

    def matches_profile_commitment(self, profile: EffectiveProfile, commitment) -> bool:
        if type(commitment) is not str or not re.fullmatch(r"[0-9a-f]{64}", commitment):
            return False
        return hmac.compare_digest(self.profile_commitment(profile), commitment)

    def create_context(self, operation_id: str) -> dict | None:
        """Return authenticated original create context, or no authority.

        Legacy intents have no context. Never infer it from daemon metadata.
        """
        self._check_open()
        with self._connection() as db:
            row = db.execute(
                "SELECT o.command_id,o.kind,c.context,c.context_mac FROM operations o "
                "JOIN create_contexts c ON c.operation_id=o.operation_id WHERE o.operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None or row["kind"] not in {"create_candidate", "create_rollback"}:
            return None
        try:
            raw, supplied = row["context"], row["context_mac"]
            if (type(raw) is not str or len(raw) > 16384
                    or len(raw.encode("utf-8")) > 16384
                    or type(supplied) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", supplied)):
                return None
            mac = self._operation_mac(operation_id, row["command_id"], row["kind"],
                                      "create_context", raw)
            if not hmac.compare_digest(mac, supplied):
                return None
            parsed = json.loads(raw)
            if type(parsed) is not dict or _canonical(parsed) != raw:
                return None
            return parsed
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None

    def _gate_binding(self, db, pins, *, allow_held=False, allow_succeeded=False):
        """Recheck ledger facts; other pins remain independently enrolled inputs."""
        operation = db.execute("SELECT * FROM operations WHERE operation_id=?", (pins['operation_id'],)).fetchone()
        if operation is None or operation['kind'] not in {'create_candidate', 'create_rollback'}:
            raise PermissionError('gate binding unavailable')
        verified = self._format_operation(operation)
        if (verified['status'] != 'completed' or verified['receipt_authentic'] is not True
                or verified['receipt'] != {'container_id': pins['container_id']}):
            raise PermissionError('gate binding unavailable')
        row = db.execute("SELECT context,context_mac FROM create_contexts WHERE operation_id=?", (pins['operation_id'],)).fetchone()
        if row is None or type(row['context']) is not str or len(row['context'].encode('utf-8')) > 16384:
            raise PermissionError('gate binding unavailable')
        mac = self._operation_mac(pins['operation_id'], operation['command_id'], operation['kind'],
                                  'create_context', row['context'])
        if type(row['context_mac']) is not str or not hmac.compare_digest(mac, row['context_mac']):
            raise PermissionError('gate binding unavailable')
        context = json.loads(row['context'])
        if type(context) is not dict or _canonical(context) != row['context']:
            raise PermissionError('gate binding unavailable')
        command = db.execute("SELECT * FROM commands WHERE command_id=?", (operation['command_id'],)).fetchone()
        allowed_states = {'running'} | ({'held'} if allow_held else set()) | ({'succeeded'} if allow_succeeded else set())
        if command is None or command['state'] not in allowed_states:
            raise PermissionError('gate binding unavailable')
        expected_state = 'running' if command['state'] == 'succeeded' else command['state']
        expected_phase_state = command['state']
        request = ContainerPilotRequest.model_validate_json(command['payload'])
        run = db.execute("SELECT * FROM runs WHERE run_id=?", (command['run_id'],)).fetchone()
        target = db.execute("SELECT * FROM targets WHERE target_id=?", (command['target_id'],)).fetchone()
        phase = db.execute("SELECT * FROM phases WHERE command_id=?", (operation['command_id'],)).fetchone()
        paused = db.execute("SELECT value FROM control WHERE key='paused'").fetchone()
        if (paused is not None and paused['value'] != 'false'
                or run is None or run['state'] != expected_state
                or target is None or target['state'] != expected_state
                or phase is None or phase['state'] != expected_phase_state
                or request.command_id != command['command_id'] or request.run_id != command['run_id']
                or request.target_id != command['target_id'] or request.action != command['action']
                or phase['run_id'] != request.run_id or phase['action'] != request.action
                or request.run_id != pins['run_id'] or request.fencing_generation != pins['fence']
                or request.manifest_digest != pins['manifest_digest']
                or request.candidate_commit != run['candidate_commit'] or request.policy_revision != run['policy_revision']
                or run['fence'] != pins['fence'] or run['target_id'] != request.target_id
                or run['manifest_digest'] != pins['manifest_digest']
                or (run['manifest_digest'], run['candidate_commit'], run['policy_revision']) != self._expected
                or target['fence'] != pins['fence'] or target['current_run_id'] != pins['run_id']
                or target['manifest_digest'] != pins['manifest_digest']
                or target['container_id'] != pins['container_id']
                or target['image_config_id'] != pins['image_config_id']
                or target['generation_id'] != pins['generation_id']
                or context.get('schema') != 2 or context.get('run_id') != pins['run_id']
                or (operation['kind'], request.action, context.get('role')) not in {
                    ('create_candidate', 'deploy_container', 'candidate'),
                    ('create_rollback', 'rollback_container', 'rollback')}
                or context.get('fencing_generation') != pins['fence']
                or context.get('manifest_digest') != pins['manifest_digest']
                or context.get('image_config_id') != pins['image_config_id']
                or context.get('generation_id') != pins['generation_id']
                or context.get('runnable_reference') != pins['runnable_digest']
                or context.get('platform_manifest_digest') != pins['platform_manifest_digest']):
            raise PermissionError('gate binding unavailable')
        return operation

    def pending_gate_context(self, *, role):
        """Read the current fenced pre-start context; this establishes no host custody."""
        if type(role) is not str or role not in {'candidate', 'rollback'}:
            raise ValueError('invalid gate role')
        with self._transaction() as db:
            rows = db.execute("SELECT g.* FROM gate_required g JOIN commands c ON c.command_id=g.command_id "
                              "WHERE c.state='running'").fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise PermissionError('ambiguous gate context')
            required = rows[0]
            operation_id, command_id = required['operation_id'], required['command_id']
            mac = self._operation_mac(operation_id, command_id, 'gate_required', 'pending', '')
            if (required['state'] != 'pending' or type(required['state_mac']) is not str
                    or not hmac.compare_digest(required['state_mac'], mac)
                    or db.execute('SELECT 1 FROM gate_acceptances WHERE operation_id=?', (operation_id,)).fetchone()):
                raise PermissionError('gate context unavailable')
            operation = db.execute('SELECT * FROM operations WHERE operation_id=? AND command_id=?',
                                   (operation_id, command_id)).fetchone()
            row = db.execute('SELECT context,context_mac FROM create_contexts WHERE operation_id=?',
                             (operation_id,)).fetchone()
            if (operation is None or operation['kind'] != 'create_' + role or row is None
                    or type(row['context']) is not str or len(row['context'].encode()) > 16384
                    or type(row['context_mac']) is not str):
                raise PermissionError('gate context unavailable')
            raw = row['context']
            mac = self._operation_mac(operation_id, command_id, operation['kind'], 'create_context', raw)
            if not hmac.compare_digest(mac, row['context_mac']):
                raise PermissionError('gate context unavailable')
            try:
                context = json.loads(raw)
                receipt = self._format_operation(operation)
                if (type(context) is not dict or _canonical(context) != raw or context.get('schema') != 2
                        or receipt['status'] != 'completed' or receipt['receipt_authentic'] is not True
                        or type(receipt['receipt']) is not dict or set(receipt['receipt']) != {'container_id'}):
                    raise ValueError
                result = {'operation_id': operation_id, 'run_id': context['run_id'],
                          'fence': context['fencing_generation'], 'role': role,
                          'generation_id': context['generation_id'], 'container_id': receipt['receipt']['container_id'],
                          'image_config_id': context['image_config_id'], 'runnable_digest': context['runnable_reference'],
                          'platform_manifest_digest': context['platform_manifest_digest']}
                pins = dict(result, manifest_digest=context['manifest_digest'])
                self._gate_binding(db, pins)
            except (ValueError, TypeError, KeyError, RecursionError):
                raise PermissionError('gate context unavailable') from None
            return result

    def require_gate_outcome(self, command_id, operation_id):
        """Durably require a gate outcome before starting; no completion proof exists yet.

        This record relies on independently protected ledger storage; it does
        not prevent restoration of an older complete database or old binaries.
        """
        with self._transaction() as db:
            operation = db.execute('SELECT * FROM operations WHERE operation_id=? AND command_id=?',
                                   (operation_id, command_id)).fetchone()
            if operation is None or operation['kind'] not in {'create_candidate', 'create_rollback'}:
                raise PermissionError('gate requirement unavailable')
            row = db.execute('SELECT context,context_mac FROM create_contexts WHERE operation_id=?',
                             (operation_id,)).fetchone()
            if row is None or type(row['context']) is not str or len(row['context'].encode()) > 16384:
                raise PermissionError('gate requirement unavailable')
            mac = self._operation_mac(operation_id, command_id, operation['kind'], 'create_context', row['context'])
            if type(row['context_mac']) is not str or not hmac.compare_digest(mac, row['context_mac']):
                raise PermissionError('gate requirement unavailable')
            context = json.loads(row['context'])
            if type(context) is not dict or _canonical(context) != row['context']:
                raise PermissionError('gate requirement unavailable')
            receipt = self._format_operation(operation)
            if (receipt['status'] != 'completed' or receipt['receipt_authentic'] is not True
                    or type(receipt['receipt']) is not dict or set(receipt['receipt']) != {'container_id'}):
                raise PermissionError('gate requirement unavailable')
            try:
                pins = {'operation_id': operation_id, 'container_id': receipt['receipt']['container_id'],
                        'run_id': context['run_id'], 'fence': context['fencing_generation'],
                        'manifest_digest': context['manifest_digest'], 'image_config_id': context['image_config_id'],
                        'generation_id': context['generation_id'], 'runnable_digest': context['runnable_reference'],
                        'platform_manifest_digest': context['platform_manifest_digest']}
                self._gate_binding(db, pins)
            except (KeyError, TypeError, ValueError):
                raise PermissionError('gate requirement unavailable') from None
            state = 'pending'
            signature = self._operation_mac(operation_id, command_id, 'gate_required', state, '')
            try:
                db.execute('INSERT INTO gate_required VALUES(?,?,?,?)', (command_id, operation_id, state, signature))
            except sqlite3.IntegrityError:
                raise PermissionError('gate requirement already exists') from None

    def hold_gate_uncertain(self, value):
        """Mark an authenticated current gate operation uncertain without claiming completion."""
        if type(value) is not dict:
            raise ValueError('invalid gate identity')
        from .mount_gate_protocol import IDENTITY_FIELDS
        if not IDENTITY_FIELDS.issubset(value):
            raise ValueError('invalid gate identity')
        pins = {name: value[name] for name in IDENTITY_FIELDS}
        with self._transaction() as db:
            operation = self._gate_binding(db, pins)
            # A fenced acceptance commit is the final eligibility decision.
            # A later lost reply is unknown to the caller, not a revocation.
            accepted = db.execute('SELECT * FROM gate_acceptances WHERE operation_id=?',
                                  (pins['operation_id'],)).fetchone()
            if accepted is not None:
                raw = accepted['record']
                if (accepted['command_id'] != operation['command_id'] or type(raw) is not str
                        or len(raw.encode()) > 16384 or type(accepted['record_mac']) is not str):
                    raise PermissionError('gate acceptance unavailable')
                mac = self._operation_mac(pins['operation_id'], operation['command_id'], 'gate_acceptance', 'accepted', raw)
                if not hmac.compare_digest(mac, accepted['record_mac']):
                    raise PermissionError('gate acceptance unavailable')
                try:
                    record = json.loads(raw)
                    if type(record) is not dict or _canonical(record) != raw or record.get('identity') != pins:
                        raise ValueError
                except (ValueError, TypeError, RecursionError):
                    raise PermissionError('gate acceptance unavailable') from None
                return
            row = db.execute('SELECT * FROM gate_required WHERE command_id=? AND operation_id=?',
                             (operation['command_id'], pins['operation_id'])).fetchone()
            if row is None or row['state'] not in {'pending', 'held'}:
                raise PermissionError('gate requirement unavailable')
            expected = self._operation_mac(pins['operation_id'], operation['command_id'], 'gate_required', row['state'], '')
            if type(row['state_mac']) is not str or not hmac.compare_digest(row['state_mac'], expected):
                raise PermissionError('gate requirement unavailable')
            signature = self._operation_mac(pins['operation_id'], operation['command_id'], 'gate_required', 'held', '')
            db.execute("UPDATE gate_required SET state='held',state_mac=? WHERE command_id=? AND operation_id=?",
                       (signature, operation['command_id'], pins['operation_id']))

    def gate_completion_snapshot(self, identity, expected_probe_digest):
        """Read current authenticated gate/health facts; never authorize success.

        Ledger MACs do not prevent whole-database rollback or establish host
        identity or custody. Callers must freshly recheck those independently.
        """
        from .mount_gate_protocol import IDENTITY_FIELDS, PROTOCOL, AUDIENCE, _payload
        if (type(identity) is not dict or set(identity) != IDENTITY_FIELDS
                or type(expected_probe_digest) is not str
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', expected_probe_digest)):
            raise ValueError('invalid gate completion request')
        pins = json.loads(_canonical(identity))
        _payload(dict(pins, protocol=PROTOCOL, audience=AUDIENCE, decision='allow',
                      observed_device=pins['expected_device'], observed_inode=pins['expected_inode'],
                      issued_at_ms=1, expires_at_ms=2))
        with self._transaction() as db:
            return self._gate_completion_snapshot(db, pins, expected_probe_digest)

    def _gate_completion_snapshot(self, db, pins, expected_probe_digest, *, allow_pending_health=False):
        from .mount_gate_protocol import IDENTITY_FIELDS, _payload
        raw = _canonical(pins)
        operation = self._gate_binding(db, pins)
        command_id, operation_id = operation['command_id'], pins['operation_id']
        required = db.execute('SELECT * FROM gate_required WHERE command_id=? AND operation_id=?',
                              (command_id, operation_id)).fetchone()
        expected = self._operation_mac(operation_id, command_id, 'gate_required', 'pending', '')
        if (required is None or required['state'] != 'pending' or type(required['state_mac']) is not str
                or not hmac.compare_digest(required['state_mac'], expected)):
            raise PermissionError('gate completion unavailable')
        challenge = db.execute('SELECT * FROM mount_gate_challenges WHERE operation_id=?', (operation_id,)).fetchone()
        if (challenge is None or challenge['state'] != 'consumed' or challenge['identity'] != raw
                or challenge['launch_id'] != pins['launch_id'] or challenge['nonce'] != pins['nonce']
                or type(challenge['identity_mac']) is not str or type(challenge['decision']) is not str
                or type(challenge['decision_mac']) is not str):
            raise PermissionError('gate completion unavailable')
        identity_mac = self._operation_mac(operation_id, command_id, operation['kind'], 'mount_gate_consumed', raw)
        decision_mac = self._operation_mac(operation_id, command_id, operation['kind'],
                                           'mount_gate_decision', challenge['decision'])
        if (not hmac.compare_digest(challenge['identity_mac'], identity_mac)
                or not hmac.compare_digest(challenge['decision_mac'], decision_mac)):
            raise PermissionError('gate completion unavailable')
        decision = json.loads(challenge['decision']); _payload(decision)
        if (_canonical(decision) != challenge['decision']
                or any(type(decision[k]) is not type(pins[k]) or decision[k] != pins[k] for k in IDENTITY_FIELDS)):
            raise PermissionError('gate completion unavailable')
        health = db.execute('SELECT * FROM operations WHERE command_id=? AND kind=?',
                            (command_id, 'gate_health')).fetchone()
        if allow_pending_health and (health is None or (health['status'] == 'intent'
                and health['receipt'] is None and health['receipt_mac'] is None)):
            return None
        if health is None: raise PermissionError('gate completion unavailable')
        verified = self._format_operation(health); receipt = verified['receipt']
        if (verified['status'] != 'completed' or verified['receipt_authentic'] is not True
                or type(receipt) is not dict
                or set(receipt) != {'container_id', 'image_config_id', 'profile_digest', 'health', 'evidence_verified',
                                    'daemon_pid', 'started_at_ms', 'finished_at_ms', 'elapsed_ms'}
                or receipt['container_id'] != pins['container_id'] or receipt['image_config_id'] != pins['image_config_id']
                or receipt['profile_digest'] != expected_probe_digest or receipt['health'] != 'healthy'
                or receipt['evidence_verified'] is not True
                or type(receipt['daemon_pid']) is not int or receipt['daemon_pid'] != pins['host_pid']
                or any(type(receipt[k]) is not int for k in ('started_at_ms', 'finished_at_ms', 'elapsed_ms'))
                or not 0 < receipt['started_at_ms'] <= receipt['finished_at_ms']
                or not 0 <= receipt['elapsed_ms'] <= 120000
                or abs((receipt['finished_at_ms'] - receipt['started_at_ms']) - receipt['elapsed_ms']) > 1000
                or not receipt['finished_at_ms'] <= int(time.time() * 1000) < receipt['finished_at_ms'] + 5000):
            raise PermissionError('gate completion unavailable')
        return {'operation_id': operation_id, 'health_operation_id': verified['operation_id'],
                'role': 'candidate' if operation['kind'] == 'create_candidate' else 'rollback',
                'health_daemon_pid': receipt['daemon_pid'], 'health_started_at_ms': receipt['started_at_ms'],
                'health_finished_at_ms': receipt['finished_at_ms'], 'health_elapsed_ms': receipt['elapsed_ms'],
                'health_receipt_digest': 'sha256:' + hashlib.sha256(_canonical(receipt).encode()).hexdigest(),
                'decision_payload_digest': 'sha256:' + hashlib.sha256(challenge['decision'].encode()).hexdigest(),
                'probe_digest': expected_probe_digest}

    def gate_health_ready(self, identity, expected_probe_digest):
        """Read identity-bound readiness; pending is not authorization or custody proof."""
        from .mount_gate_protocol import IDENTITY_FIELDS, PROTOCOL, AUDIENCE, _payload
        if (type(identity) is not dict or set(identity) != IDENTITY_FIELDS
                or type(expected_probe_digest) is not str
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', expected_probe_digest)):
            raise ValueError('invalid gate readiness request')
        pins = json.loads(_canonical(identity))
        _payload(dict(pins, protocol=PROTOCOL, audience=AUDIENCE, decision='allow',
                      observed_device=pins['expected_device'], observed_inode=pins['expected_inode'],
                      issued_at_ms=1, expires_at_ms=2))
        with self._transaction() as db:
            snapshot = self._gate_completion_snapshot(db, pins, expected_probe_digest, allow_pending_health=True)
            return {'ready': snapshot is not None}

    def accept_gate_completion(self, identity, *, role, executable_digest, health_contract,
                               probe_digest, health_receipt_digest, decision_payload_digest):
        """Commit externally authenticated completion facts under the current fence.

        The caller must verify the signed supervisor policy and direct custody.
        The commit is authoritative for eligibility; old database restoration remains an external trust boundary.
        """
        from .application_gate_enrollment import COMPLETION_HEALTH_CONTRACT
        from .mount_gate_protocol import IDENTITY_FIELDS, PROTOCOL, AUDIENCE, _payload
        if (type(identity) is not dict or set(identity) != IDENTITY_FIELDS
                or role not in {'candidate', 'rollback'} or type(role) is not str
                or health_contract != COMPLETION_HEALTH_CONTRACT or type(health_contract) is not str
                or any(type(v) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', v)
                       for v in (executable_digest, probe_digest, health_receipt_digest, decision_payload_digest))):
            raise ValueError('invalid gate acceptance')
        pins = json.loads(_canonical(identity))
        _payload(dict(pins, protocol=PROTOCOL, audience=AUDIENCE, decision='allow',
                      observed_device=pins['expected_device'], observed_inode=pins['expected_inode'],
                      issued_at_ms=1, expires_at_ms=2))
        with self._transaction() as db:
            snapshot = self._gate_completion_snapshot(db, pins, probe_digest)
            if (snapshot['role'] != role or snapshot['health_receipt_digest'] != health_receipt_digest
                    or snapshot['decision_payload_digest'] != decision_payload_digest):
                raise PermissionError('gate acceptance mismatch')
            operation = self._gate_binding(db, pins)
            record = dict(identity=pins, role=role, executable_digest=executable_digest,
                          health_contract=health_contract, probe_digest=probe_digest,
                          health_receipt_digest=health_receipt_digest,
                          decision_payload_digest=decision_payload_digest)
            raw = _canonical(record)
            mac = self._operation_mac(pins['operation_id'], operation['command_id'], 'gate_acceptance', 'accepted', raw)
            try:
                db.execute('INSERT INTO gate_acceptances VALUES(?,?,?,?)',
                           (pins['operation_id'], operation['command_id'], raw, mac))
            except sqlite3.IntegrityError:
                raise PermissionError('gate acceptance already recorded') from None
            return record

    def lookup_gate_completion(self, identity):
        """Read committed acceptance under the current running or held fence; never replay an effect."""
        from .mount_gate_protocol import IDENTITY_FIELDS, PROTOCOL, AUDIENCE, _payload
        if type(identity) is not dict or set(identity) != IDENTITY_FIELDS:
            raise ValueError('invalid gate identity')
        pins = json.loads(_canonical(identity))
        _payload(dict(pins, protocol=PROTOCOL, audience=AUDIENCE, decision='allow',
                      observed_device=pins['expected_device'], observed_inode=pins['expected_inode'],
                      issued_at_ms=1, expires_at_ms=2))
        with self._transaction() as db:
            operation = self._gate_binding(db, pins, allow_held=True, allow_succeeded=True)
            row = db.execute('SELECT * FROM gate_acceptances WHERE operation_id=?', (pins['operation_id'],)).fetchone()
            if row is None:
                return None
            if (row['command_id'] != operation['command_id'] or type(row['record']) is not str
                    or len(row['record'].encode()) > 16384 or type(row['record_mac']) is not str):
                raise PermissionError('gate acceptance unavailable')
            mac = self._operation_mac(pins['operation_id'], operation['command_id'], 'gate_acceptance', 'accepted', row['record'])
            if not hmac.compare_digest(mac, row['record_mac']):
                raise PermissionError('gate acceptance unavailable')
            try:
                record = json.loads(row['record'])
                if (type(record) is not dict or _canonical(record) != row['record']
                        or record.get('identity') != pins):
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                raise PermissionError('gate acceptance unavailable') from None
            return record

    def wait_for_gate_acceptance(self, command_id, deadline_monotonic):
        """Wait up to five seconds; SQLite busy timeout is cooperative, not cancellation."""
        if type(deadline_monotonic) not in (int, float) or not math.isfinite(deadline_monotonic):
            raise ValueError('invalid gate deadline')
        deadline_monotonic = min(deadline_monotonic, time.monotonic() + 5.0)
        while time.monotonic() < deadline_monotonic:
            with self._connection() as db:
                row = db.execute('SELECT 1 FROM gate_acceptances WHERE command_id=?', (command_id,)).fetchone()
                if row is not None:
                    return True
            time.sleep(min(0.05, max(0, deadline_monotonic - time.monotonic())))
        return False

    def _accepted_gate_allows_finish(self, db, command_id, evidence, generation_id):
        row = db.execute('SELECT * FROM gate_acceptances WHERE command_id=?', (command_id,)).fetchone()
        if (row is None or type(row['record']) is not str or len(row['record'].encode()) > 16384
                or type(row['record_mac']) is not str):
            return False
        mac = self._operation_mac(row['operation_id'], command_id, 'gate_acceptance', 'accepted', row['record'])
        if not hmac.compare_digest(mac, row['record_mac']):
            return False
        try:
            record = json.loads(row['record'])
            if type(record) is not dict or _canonical(record) != row['record']:
                return False
            pins = record['identity']
            operation = self._gate_binding(db, pins)
            snapshot = self._gate_completion_snapshot(db, pins, record['probe_digest'])
            return (operation['command_id'] == command_id and row['operation_id'] == pins['operation_id']
                    and snapshot['role'] == record['role']
                    and snapshot['health_receipt_digest'] == record['health_receipt_digest']
                    and snapshot['decision_payload_digest'] == record['decision_payload_digest']
                    and evidence.container_id == pins['container_id']
                    and evidence.image_config_id == pins['image_config_id']
                    and evidence.evidence_verified is True
                    and generation_id == pins['generation_id'])
        except (PermissionError, ValueError, TypeError, KeyError, RecursionError):
            return False

    def reserve_mount_gate_challenge(self, identity):
        """Reserve one independently enrolled identity; this verifies no host claim.

        MACs detect changed rows, not restoration of a complete earlier valid row
        or database. An independently enforced owner/storage anti-rollback
        boundary is required; this reservation is not a machine-wide fence.
        """
        from .mount_gate_protocol import IDENTITY_FIELDS, MAX_BYTES, PROTOCOL, AUDIENCE, _payload
        if type(identity) is not dict or set(identity) != IDENTITY_FIELDS:
            raise ValueError('invalid gate identity')
        encoded = _canonical(identity)
        if len(encoded.encode('utf-8')) > MAX_BYTES:
            raise ValueError('invalid gate identity')
        pins = json.loads(encoded)
        _payload(dict(pins, protocol=PROTOCOL, audience=AUDIENCE, decision='allow',
            observed_device=pins['expected_device'], observed_inode=pins['expected_inode'],
            issued_at_ms=1, expires_at_ms=2))
        raw = _canonical(pins)
        with self._transaction() as db:
            operation = self._gate_binding(db, pins)
            mac = self._operation_mac(pins['operation_id'], operation['command_id'], operation['kind'], 'mount_gate_reserved', raw)
            try:
                db.execute("INSERT INTO mount_gate_challenges VALUES(?,?,?,?,?,'reserved',NULL,NULL)",
                           (pins['operation_id'], pins['launch_id'], pins['nonce'], raw, mac))
            except sqlite3.IntegrityError:
                raise PermissionError('gate challenge already reserved') from None

    def consume_mount_gate_challenge(self, decision):
        """Consume once within this ledger history; caller verifies signature/freshness.

        Authenticated old rows or a restored old database can be replayed unless
        independently protected storage and owner authority prevent rollback.
        This method neither establishes that boundary nor proves host custody.
        """
        from .mount_gate_protocol import IDENTITY_FIELDS, MAX_BYTES, _payload
        if type(decision) is not dict:
            raise ValueError('invalid gate decision')
        encoded = _canonical(decision)
        if len(encoded.encode('utf-8')) > MAX_BYTES:
            raise ValueError('invalid gate decision')
        payload = json.loads(encoded)
        _payload(payload)
        pins = {name: payload[name] for name in IDENTITY_FIELDS}
        raw = _canonical(pins)
        with self._transaction() as db:
            operation = self._gate_binding(db, pins)
            row = db.execute("SELECT * FROM mount_gate_challenges WHERE operation_id=?", (pins['operation_id'],)).fetchone()
            mac = self._operation_mac(pins['operation_id'], operation['command_id'], operation['kind'], 'mount_gate_reserved', raw)
            if (row is None or row['state'] != 'reserved' or row['launch_id'] != pins['launch_id']
                    or row['nonce'] != pins['nonce'] or row['identity'] != raw
                    or row['decision'] is not None or row['decision_mac'] is not None
                    or type(row['identity_mac']) is not str or not hmac.compare_digest(row['identity_mac'], mac)):
                raise PermissionError('gate challenge unavailable')
            consumed_mac = self._operation_mac(pins['operation_id'], operation['command_id'], operation['kind'], 'mount_gate_consumed', raw)
            decision_raw = _canonical(payload)
            decision_mac = self._operation_mac(pins['operation_id'], operation['command_id'], operation['kind'], 'mount_gate_decision', decision_raw)
            changed = db.execute("UPDATE mount_gate_challenges SET state='consumed',identity_mac=?,decision=?,decision_mac=? WHERE operation_id=? AND state='reserved'",
                                 (consumed_mac, decision_raw, decision_mac, pins['operation_id'])).rowcount
            if changed != 1:
                raise PermissionError('gate challenge unavailable')
        return True

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
            # A terminal engine result does not prove the gate's outcome or
            # resolve an uncertain authenticated handoff.
            if db.execute('SELECT 1 FROM gate_required WHERE command_id=?', (command_id,)).fetchone():
                raise PermissionError('gate outcome remains unresolved')
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
            # The committed authenticated acceptance is authoritative; all other
            # gate outcomes, including failed effects, remain held.
            if db.execute('SELECT 1 FROM gate_required WHERE command_id=?', (command_id,)).fetchone():
                if state != 'succeeded' or not self._accepted_gate_allows_finish(db, command_id, evidence, generation_id):
                    state, reason = 'held', 'inspect_required'
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
            row = db.execute("SELECT response FROM commands WHERE command_id=?", (command_id,)).fetchone()
        return ContainerPilotResponse.model_validate_json(row["response"]) if row else None

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
