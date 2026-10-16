import os
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from agent_message_bus.database import (
    MessageError, MessageRepository, NotFound, SendConflict, SendReplayUnavailable, iso, now_utc,
)


def test_same_request_returns_original_message_without_new_delivery(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    original = repo.insert(sender="hermes", recipient="codex", message_type="task", body={"x": 1},
                           client_request_id="request-1")
    replay = repo.insert(sender="hermes", recipient="codex", message_type="task", body={"x": 1},
                         client_request_id="request-1")

    assert replay == original
    assert repo.delivery_head() == 1
    with repo.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_fingerprint_uses_canonical_json_and_resolved_defaults(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    original = repo.insert(sender="hermes", recipient="codex", message_type="task",
                           body={"nested": {"b": 2, "a": 1}, "x": "café"},
                           client_request_id="canonical")
    replay = repo.insert(sender="hermes", recipient="codex", message_type="task",
                         body={"x": "café", "nested": {"a": 1, "b": 2}}, priority="normal",
                         correlation_id=None, in_reply_to=None, requires_user_approval=False,
                         client_request_id="canonical")
    assert replay == original
    assert repo.delivery_head() == 1


@pytest.mark.parametrize("field,value", [
    ("recipient", "claude"),
    ("message_type", "request"),
    ("body", {"secret": "different-private-body"}),
    ("priority", "high"),
    ("correlation_id", "different-correlation"),
    ("in_reply_to", "missing-parent"),
    ("requires_user_approval", True),
])
def test_reusing_request_id_with_any_changed_semantic_field_conflicts(tmp_path, field, value):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    original = {
        "recipient": "codex", "message_type": "task", "body": {"secret": "private-body"},
        "priority": "normal", "correlation_id": None, "in_reply_to": None,
        "requires_user_approval": False,
    }
    repo.insert(sender="hermes", client_request_id="conflict-key", **original)
    changed = {**original, field: value}

    with pytest.raises(SendConflict) as exc:
        repo.insert(sender="hermes", client_request_id="conflict-key", **changed)

    assert str(exc.value) == "client request id conflicts with an earlier send"
    assert "private-body" not in str(exc.value) and "different-private-body" not in str(exc.value)
    assert repo.delivery_head() == 1


@pytest.mark.parametrize("client_request_id", [
    "", " leading", "trailing ", "_starts-with-punctuation", "-starts-with-punctuation",
    "bad/slash", "line\nbreak", "é", "a" * 129, 0, True,
])
def test_invalid_request_ids_are_rejected_without_coercion(tmp_path, client_request_id):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    with pytest.raises(MessageError, match="invalid client request id"):
        repo.insert(sender="hermes", recipient="codex", message_type="task", body={},
                    client_request_id=client_request_id)
    assert repo.delivery_head() == 0


def test_request_id_ascii_length_boundaries_and_case_are_preserved(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    maximum = "A" + "b" * 127
    first = repo.insert(sender="hermes", recipient="codex", message_type="task", body={},
                        client_request_id=maximum)
    upper = repo.insert(sender="hermes", recipient="codex", message_type="task", body={},
                        client_request_id="Case")
    lower = repo.insert(sender="hermes", recipient="codex", message_type="task", body={},
                        client_request_id="case")
    assert first and upper.id != lower.id
    assert repo.delivery_head() == 3


def test_legacy_sends_without_a_request_id_remain_distinct(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    first = repo.insert(sender="hermes", recipient="codex", message_type="task", body={})
    second = repo.insert(sender="hermes", recipient="codex", message_type="task", body={},
                         client_request_id=None)
    assert first.id != second.id
    assert repo.delivery_head() == 2


def test_receipt_is_sender_scoped_private_and_tracks_current_status(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    message = repo.insert(sender="hermes", recipient="codex", message_type="task",
                          body={"subject": "private-subject", "body": "private-body"},
                          correlation_id="private-correlation", client_request_id="private-key")
    receipt = repo.get_send_receipt("hermes", "private-key")
    assert receipt == {
        "message_id": message.id, "sender": "hermes", "recipient": "codex",
        "created_at": message.created_at, "seq": 1, "status": "pending", "retained": True,
    }
    assert set(receipt) == {"message_id", "sender", "recipient", "created_at", "seq", "status", "retained"}
    assert not ({"body", "subject", "correlation_id", "client_request_id", "fingerprint"} & receipt.keys())
    assert repo.claim(message.id, "codex").status == "claimed"
    assert repo.get_send_receipt("hermes", "private-key")["status"] == "claimed"
    repo.acknowledge(message.id, "codex")
    assert repo.get_send_receipt("hermes", "private-key")["status"] == "acknowledged"
    with repo.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(send_idempotency)")}
        assert "body_json" not in columns and "body" not in columns
        receipt_values = tuple(conn.execute("SELECT * FROM send_idempotency").fetchone())
        assert all("private-body" not in str(value) for value in receipt_values)
        assert all("private-subject" not in str(value) for value in receipt_values)
        assert all("private-correlation" not in str(value) for value in receipt_values)


def test_unknown_and_other_senders_get_the_same_not_found_receipt_error(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    repo.insert(sender="hermes", recipient="codex", message_type="task", body={},
                client_request_id="sender-private-key")
    errors = []
    for actor, key in (("codex", "sender-private-key"), ("hermes", "unknown-key")):
        with pytest.raises(NotFound) as exc:
            repo.get_send_receipt(actor, key)
        errors.append(str(exc.value))
    assert errors == ["send receipt not found", "send receipt not found"]


def test_pruned_message_keeps_tombstone_across_restart_and_blocks_reuse(tmp_path):
    path = tmp_path / "messages.sqlite3"
    repo = MessageRepository(path)
    message = repo.insert(sender="hermes", recipient="codex", message_type="task", body={"x": 1},
                          client_request_id="durable-key")
    repo.claim(message.id, "codex")
    repo.acknowledge(message.id, "codex")
    with repo.connect() as conn:
        conn.execute("UPDATE messages SET acknowledged_at=? WHERE id=?",
                     (iso(now_utc() - timedelta(days=60)), message.id))
    assert repo.cleanup(acknowledged_days=30) == {"acknowledged_deleted": 1, "rejected_deleted": 0}

    restarted = MessageRepository(path)
    receipt = restarted.get_send_receipt("hermes", "durable-key")
    assert receipt == {
        "message_id": message.id, "sender": "hermes", "recipient": "codex",
        "created_at": message.created_at, "seq": 1, "status": "pruned", "retained": False,
    }
    with pytest.raises(SendReplayUnavailable):
        restarted.insert(sender="hermes", recipient="codex", message_type="task", body={"x": 1},
                         client_request_id="durable-key")
    with pytest.raises(SendConflict):
        restarted.insert(sender="hermes", recipient="codex", message_type="task", body={"x": 2},
                         client_request_id="durable-key")
    with restarted.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM send_idempotency").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert restarted.delivery_head() == 1


def test_matching_retry_does_not_revalidate_a_pruned_parent(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    parent = repo.insert(sender="hermes", recipient="codex", message_type="task", body={})
    reply = repo.insert(sender="codex", recipient="hermes", message_type="reply", body={"ok": True},
                        in_reply_to=parent.id, client_request_id="reply-key")
    repo.claim(parent.id, "codex")
    repo.acknowledge(parent.id, "codex")
    with repo.connect() as conn:
        conn.execute("UPDATE messages SET acknowledged_at=? WHERE id=?",
                     (iso(now_utc() - timedelta(days=60)), parent.id))
    repo.cleanup(acknowledged_days=30)

    replay = repo.insert(sender="codex", recipient="hermes", message_type="reply", body={"ok": True},
                         in_reply_to=parent.id, client_request_id="reply-key")
    assert replay == reply
    assert repo.delivery_head() == 2


def test_failed_receipt_insert_rolls_back_message_and_does_not_reserve_key(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    with repo.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_send_receipt BEFORE INSERT ON send_idempotency
            BEGIN SELECT RAISE(ABORT, 'receipt write blocked for test'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="receipt write blocked"):
        repo.insert(sender="hermes", recipient="codex", message_type="task", body={"x": 1},
                    client_request_id="retry-after-rollback")
    with repo.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM message_delivery_sequence").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM send_idempotency").fetchone()[0] == 0
        conn.execute("DROP TRIGGER reject_send_receipt")

    message = repo.insert(sender="hermes", recipient="codex", message_type="task", body={"x": 1},
                          client_request_id="retry-after-rollback")
    assert repo.sequence_for(message.id) == 1
    assert repo.get_send_receipt("hermes", "retry-after-rollback")["message_id"] == message.id


def test_old_database_initialization_preserves_rows_and_sequence_and_adds_receipt_table(tmp_path):
    path = tmp_path / "old.sqlite3"
    repo = MessageRepository(path)
    with repo.connect() as conn:
        conn.execute("DROP TABLE send_idempotency")
    legacy = repo.insert(sender="hermes", recipient="codex", message_type="legacy", body={})
    before_head = repo.delivery_head()

    restarted = MessageRepository(path)
    assert restarted.get(legacy.id) == legacy
    assert restarted.delivery_head() == before_head == 1
    with restarted.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM send_idempotency").fetchone()[0] == 0
    duplicate_a = restarted.insert(sender="hermes", recipient="codex", message_type="legacy", body={})
    duplicate_b = restarted.insert(sender="hermes", recipient="codex", message_type="legacy", body={})
    assert duplicate_a.id != duplicate_b.id
    assert restarted.delivery_head() == 3


def test_two_independent_process_writers_converge_on_one_durable_send(tmp_path):
    path = tmp_path / "writers.sqlite3"
    MessageRepository(path)
    gate = tmp_path / "start-writers"
    ready_paths = [tmp_path / "writer-1.ready", tmp_path / "writer-2.ready"]
    script = """
import sys, time
from pathlib import Path
from agent_message_bus.database import MessageRepository
path, gate, ready = sys.argv[1:]
repo = MessageRepository(path)
Path(ready).touch()
while not Path(gate).exists():
    time.sleep(0.005)
message = repo.insert(sender='hermes', recipient='codex', message_type='task', body={'race': True},
                     client_request_id='cross-process-key')
print(message.id)
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(path), str(gate), str(ready)],
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        for ready in ready_paths
    ]
    try:
        deadline = time.monotonic() + 10
        while not all(ready.exists() for ready in ready_paths) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert all(ready.exists() for ready in ready_paths), "writers did not initialize"
        gate.touch()
        outputs = [process.communicate(timeout=15) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert [process.returncode for process in processes] == [0, 0], outputs
    message_ids = [stdout.strip() for stdout, _ in outputs]
    assert len(set(message_ids)) == 1
    reopened = MessageRepository(path)
    assert reopened.delivery_head() == 1
    assert reopened.get_send_receipt("hermes", "cross-process-key")["message_id"] == message_ids[0]
    with reopened.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM send_idempotency").fetchone()[0] == 1


def test_request_ids_are_scoped_by_sender(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    hermes = repo.insert(sender="hermes", recipient="codex", message_type="task", body={"from": "h"},
                         client_request_id="shared-sender-key")
    codex = repo.insert(sender="codex", recipient="hermes", message_type="task", body={"from": "c"},
                        client_request_id="shared-sender-key")
    assert hermes.id != codex.id
    assert repo.get_send_receipt("hermes", "shared-sender-key")["message_id"] == hermes.id
    assert repo.get_send_receipt("codex", "shared-sender-key")["message_id"] == codex.id
    assert repo.delivery_head() == 2


def test_non_finite_json_is_rejected_for_new_sends_and_conflicts_for_reused_keys(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    non_finite = {"value": float("nan")}
    with pytest.raises(MessageError, match="send payload is not valid JSON data") as exc:
        repo.insert(sender="hermes", recipient="codex", message_type="task", body=non_finite,
                    client_request_id="nan-key")
    assert "nan" not in str(exc.value).lower()
    repo.insert(sender="hermes", recipient="codex", message_type="task", body={"value": 1},
                client_request_id="nan-conflict-key")
    with pytest.raises(SendConflict):
        repo.insert(sender="hermes", recipient="codex", message_type="task", body=non_finite,
                    client_request_id="nan-conflict-key")
    assert repo.delivery_head() == 1


def test_failed_parent_validation_does_not_reserve_request_id(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    parent = repo.insert(sender="hermes", recipient="codex", message_type="task", body={})
    with pytest.raises(MessageError, match="parent message not found"):
        repo.insert(sender="codex", recipient="hermes", message_type="reply", body={},
                    in_reply_to="missing-parent", client_request_id="parent-failure-key")

    reply = repo.insert(sender="codex", recipient="hermes", message_type="reply", body={},
                        in_reply_to=parent.id, client_request_id="parent-failure-key")
    assert reply.in_reply_to == parent.id
    assert repo.delivery_head() == 2


def test_receipts_track_rejected_status(tmp_path):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    message = repo.insert(sender="hermes", recipient="codex", message_type="task", body={},
                          client_request_id="rejected-key")
    repo.claim(message.id, "codex")
    repo.reject(message.id, "codex", "declined")
    assert repo.get_send_receipt("hermes", "rejected-key")["status"] == "rejected"
