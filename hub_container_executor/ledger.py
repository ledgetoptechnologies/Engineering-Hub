"""Private, single-owner SQLite command ledger for the container-pilot adapter.

The ledger is deliberately not a daemon driver. It provides durable admission,
fencing and operation intents; ambiguity is retained as held and never replayed.
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
import uuid
from contextlib import contextmanager
from pathlib import Path

from engineering_hub.container_pilot_client import (
    ContainerPilotEvidence,
    ContainerPilotRequest,
    ContainerPilotResponse,
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
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            connection.close()

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

    def begin_operation(self, command_id: str, kind: str) -> str:
        self._check_open()
        if not isinstance(kind, str) or not _OPERATION_NAME.fullmatch(kind):
            raise ValueError("invalid operation kind")
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
            failure = db.execute(
                "SELECT 1 FROM operations WHERE command_id=? AND status='terminal_failed'",
                (command_id,),
            ).fetchone()
            if failure is None:
                raise PermissionError("terminal failure evidence is required")
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
        if self._closed:
            return
        self._closed = True
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, *_exc):
        self.close()
