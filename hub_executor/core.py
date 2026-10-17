import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


COMMAND_FIELDS = {
    "command_id",
    "target_id",
    "action",
    "artifact_digest",
    "candidate_commit",
    "policy_revision",
    "fencing_generation",
}
ACTIONS = {"deploy_fixture", "smoke_fixture", "rollback_fixture"}


class ExecutorError(Exception):
    def __init__(self, code, status):
        self.code = code
        self.status = status
        super().__init__(code)


class HubExecutor:
    def __init__(
        self,
        database_path,
        target_roots,
        artifacts,
        *,
        candidate_commit,
        artifact_commits=None,
        artifact_roles=None,
        policy_revision,
    ):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.target_roots = {key: Path(value) for key, value in target_roots.items()}
        self.artifacts = {key: bytes(value) for key, value in artifacts.items()}
        self.candidate_commit = candidate_commit
        self.artifact_commits = dict(
            artifact_commits
            if artifact_commits is not None
            else {digest: candidate_commit for digest in self.artifacts}
        )
        self.artifact_roles = dict(
            artifact_roles
            if artifact_roles is not None
            else {digest: "candidate" for digest in self.artifacts}
        )
        if set(self.artifact_commits) != set(self.artifacts) or set(
            self.artifact_roles
        ) != set(self.artifacts):
            raise ValueError("each fixture artifact needs configured provenance and role")
        if any(role not in {"candidate", "rollback"} for role in self.artifact_roles.values()):
            raise ValueError("fixture artifact role is invalid")
        for digest, content in self.artifacts.items():
            expected = f"sha256:{hashlib.sha256(content).hexdigest()}"
            if digest != expected:
                raise ValueError("fixture artifact digest does not match its bytes")
        self.policy_revision = policy_revision
        with self._db() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS commands ("
                "command_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                "status TEXT NOT NULL, result TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS targets ("
                "target_id TEXT PRIMARY KEY, fencing_generation INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS control ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            rows = connection.execute(
                "SELECT command_id, result FROM commands WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                result = json.loads(row["result"])
                result.update(state="held", reason="outcome_unresolved")
                connection.execute(
                    "UPDATE commands SET status = 'held', result = ? WHERE command_id = ?",
                    (json.dumps(result), row["command_id"]),
                )

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_shape(command):
        if not isinstance(command, dict) or set(command) != COMMAND_FIELDS:
            raise ExecutorError("invalid_payload", 400)
        command_id = command["command_id"]
        if not isinstance(command_id, str):
            raise ExecutorError("invalid_payload", 400)
        try:
            if str(uuid.UUID(command_id)) != command_id:
                raise ValueError
        except ValueError as error:
            raise ExecutorError("invalid_payload", 400) from error
        target = command["target_id"]
        if not isinstance(target, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,47}:staging", target
        ):
            raise ExecutorError("invalid_payload", 400)
        if not isinstance(command["action"], str) or command["action"] not in ACTIONS:
            raise ExecutorError("invalid_payload", 400)
        digest = command["artifact_digest"]
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ExecutorError("invalid_payload", 400)
        candidate = command["candidate_commit"]
        if not isinstance(candidate, str) or not re.fullmatch(r"[0-9a-f]{40}", candidate):
            raise ExecutorError("invalid_payload", 400)
        for field in ("policy_revision", "fencing_generation"):
            value = command[field]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExecutorError("invalid_payload", 400)

    def execute_command(self, command):
        self._validate_shape(command)
        payload = json.dumps(command, sort_keys=True, separators=(",", ":"))
        result = {
            **command,
            "state": "running",
            "reason": None,
            "observed": {},
        }
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload, result FROM commands WHERE command_id = ?",
                (command["command_id"],),
            ).fetchone()
            if existing:
                if existing["payload"] != payload:
                    raise ExecutorError("command_conflict", 409)
                replay = json.loads(existing["result"])
                replay["replayed"] = True
                return replay
            if command["target_id"] not in self.target_roots or command[
                "artifact_digest"
            ] not in self.artifacts:
                raise ExecutorError("target_or_artifact_not_allowed", 403)
            action = command["action"]
            role = self.artifact_roles[command["artifact_digest"]]
            if (
                command["candidate_commit"] != self.artifact_commits[command["artifact_digest"]]
                or command["policy_revision"] != self.policy_revision
                or (action == "deploy_fixture" and role != "candidate")
                or (action == "rollback_fixture" and role != "rollback")
            ):
                raise ExecutorError("policy_conflict", 409)
            paused = connection.execute(
                "SELECT value FROM control WHERE key = 'paused'"
            ).fetchone()
            if paused and paused["value"] == "true":
                raise ExecutorError("paused", 423)
            current = connection.execute(
                "SELECT fencing_generation FROM targets WHERE target_id = ?",
                (command["target_id"],),
            ).fetchone()
            if current and command["fencing_generation"] <= current["fencing_generation"]:
                raise ExecutorError("stale_fence", 409)
            connection.execute(
                "INSERT INTO targets(target_id, fencing_generation) VALUES (?, ?) "
                "ON CONFLICT(target_id) DO UPDATE SET fencing_generation = excluded.fencing_generation",
                (command["target_id"], command["fencing_generation"]),
            )
            connection.execute(
                "INSERT INTO commands VALUES (?, ?, 'running', ?)",
                (command["command_id"], payload, json.dumps(result)),
            )

        stale_before_effect = False
        try:
            with self._db() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT fencing_generation FROM targets WHERE target_id = ?",
                    (command["target_id"],),
                ).fetchone()
                if current["fencing_generation"] != command["fencing_generation"]:
                    result.update(state="failed", reason="stale_fence")
                    connection.execute(
                        "UPDATE commands SET status = 'failed', result = ? WHERE command_id = ?",
                        (json.dumps(result), command["command_id"]),
                    )
                    stale_before_effect = True
                elif command["action"] == "smoke_fixture":
                    observed = self._read_observed(command["target_id"])
                    if observed.get("active_artifact_digest") != command["artifact_digest"]:
                        result.update(
                            state="failed",
                            reason="fixture_state_mismatch",
                            observed=observed,
                        )
                        connection.execute(
                            "UPDATE commands SET status = 'failed', result = ? WHERE command_id = ?",
                            (json.dumps(result), command["command_id"]),
                        )
                    else:
                        observed = self._apply_fixture(command)
                        result.update(state="succeeded", reason=None, observed=observed)
                        connection.execute(
                            "UPDATE commands SET status = 'succeeded', result = ? WHERE command_id = ?",
                            (json.dumps(result), command["command_id"]),
                        )
                else:
                    observed = self._apply_fixture(command)
                    result.update(state="succeeded", reason=None, observed=observed)
                    connection.execute(
                        "UPDATE commands SET status = 'succeeded', result = ? WHERE command_id = ?",
                        (json.dumps(result), command["command_id"]),
                    )
        except Exception:
            self._mark_held(command["command_id"])
            raise
        if stale_before_effect:
            raise ExecutorError("stale_fence", 409)
        return result

    def _mark_held(self, command_id):
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, result, status FROM commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                return
            command = json.loads(row["payload"])
            result = json.loads(row["result"])
            result.update(
                state="held",
                reason="outcome_unresolved",
                observed=self._read_observed(command["target_id"]),
            )
            connection.execute(
                "UPDATE commands SET status = 'held', result = ? WHERE command_id = ?",
                (json.dumps(result), command_id),
            )


    def _apply_fixture(self, command):
        target = self.target_roots[command["target_id"]]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(exist_ok=True)
        os.chmod(target.parent, 0o777)
        os.chmod(target, 0o777)
        state_path = target / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            state = {"effect_count": 0}
        state.update(
            {
                "active_artifact_digest": command["artifact_digest"],
                "artifact_bytes": self.artifacts[command["artifact_digest"]].decode("ascii"),
                "effect_count": state["effect_count"] + 1,
                "marker": {
                    "command_id": command["command_id"],
                    "action": command["action"],
                    "artifact_digest": command["artifact_digest"],
                    "fencing_generation": command["fencing_generation"],
                },
            }
        )
        descriptor, temporary_name = tempfile.mkstemp(prefix=".state-", dir=target)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o644)
                json.dump(state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, state_path)
            directory_fd = os.open(target, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return {
            "active_artifact_digest": state["active_artifact_digest"],
            "effect_count": state["effect_count"],
            "marker": state["marker"],
        }

    def set_paused(self, paused):
        if not isinstance(paused, bool):
            raise ExecutorError("invalid_payload", 400)
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO control(key, value) VALUES ('paused', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("true" if paused else "false",),
            )
        return {"paused": paused}

    def is_paused(self):
        with self._db() as connection:
            row = connection.execute(
                "SELECT value FROM control WHERE key = 'paused'"
            ).fetchone()
        return bool(row and row["value"] == "true")

    def get_command(self, command_id):
        try:
            if str(uuid.UUID(command_id)) != command_id:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        with self._db() as connection:
            row = connection.execute(
                "SELECT result FROM commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        return json.loads(row["result"]) if row else None

    def reconcile_command(self, command_id):
        try:
            if str(uuid.UUID(command_id)) != command_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as error:
            raise ExecutorError("invalid_payload", 400) from error
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, status, result FROM commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            if row is None:
                raise ExecutorError("command_not_found", 404)
            result = json.loads(row["result"])
            if row["status"] != "held":
                return result
            command = json.loads(row["payload"])
            observed = self._read_observed(command["target_id"])
            marker = observed.get("marker") if observed else None
            expected_marker = {
                "command_id": command["command_id"],
                "action": command["action"],
                "artifact_digest": command["artifact_digest"],
                "fencing_generation": command["fencing_generation"],
            }
            if (
                marker == expected_marker
                and observed.get("active_artifact_digest") == command["artifact_digest"]
            ):
                result.update(state="succeeded", reason=None, observed=observed)
                status = "succeeded"
            else:
                result.update(state="held", reason="outcome_unresolved", observed=observed or {})
                status = "held"
            connection.execute(
                "UPDATE commands SET status = ?, result = ? WHERE command_id = ?",
                (status, json.dumps(result), command_id),
            )
            return result

    def _read_observed(self, target_id):
        state_path = self.target_roots[target_id] / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return {
            "active_artifact_digest": state.get("active_artifact_digest"),
            "effect_count": state.get("effect_count"),
            "marker": state.get("marker"),
        }
