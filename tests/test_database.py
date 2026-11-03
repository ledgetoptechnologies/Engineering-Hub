import sqlite3
from datetime import timedelta

import pytest

from agent_message_bus.database import InvalidTransition, MessageRepository, MessageError, iso, now_utc


def test_database_initializes_wal_and_inserts(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3", lease_seconds=10)
    with repo.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    message = repo.insert(sender="hermes", recipient="codex", message_type="task", body={"x": 1})
    assert message.status == "pending" and message.body == {"x": 1}


def test_sender_recipient_and_priority_validation(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    with pytest.raises(MessageError): repo.insert(sender="hermes", recipient="hermes", message_type="x", body={})
    with pytest.raises(MessageError): repo.insert(sender="other", recipient="codex", message_type="x", body={})
    with pytest.raises(MessageError): repo.insert(sender="hermes", recipient="codex", message_type="x", body={}, priority="bad")


def test_claude_is_a_valid_agent_for_recipient_validation(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    assert repo.list_for_recipient("claude") == []
    with pytest.raises(MessageError):
        repo.list_for_recipient("unknown")


def test_legacy_schema_migration_preserves_messages_indexes_and_other_tables(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            sender TEXT NOT NULL CHECK (sender IN ('hermes', 'codex')),
            recipient TEXT NOT NULL CHECK (recipient IN ('hermes', 'codex')),
            message_type TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
            body_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'claimed', 'acknowledged', 'rejected')),
            correlation_id TEXT,
            requires_user_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_user_approval IN (0, 1)),
            created_at TEXT NOT NULL,
            claimed_by TEXT,
            claim_expires_at TEXT,
            acknowledged_at TEXT,
            rejected_at TEXT,
            rejection_reason TEXT
        );
        CREATE INDEX idx_messages_recipient_status_created ON messages (recipient, status, created_at);
        CREATE INDEX idx_messages_claim_expiry ON messages (status, claim_expires_at);
        CREATE INDEX idx_messages_retention ON messages (status, acknowledged_at, rejected_at);
        CREATE INDEX idx_messages_custom ON messages (message_type, created_at DESC);
        CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
            content_type TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE knowledge (project TEXT NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL,
            content TEXT NOT NULL, version INTEGER NOT NULL, sha256 TEXT NOT NULL,
            created_by TEXT NOT NULL, updated_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (project, path));
    """)
    legacy_rows = [
        ("pending", "hermes", "codex", "pending", "trace-pending", "2024-01-01T00:00:00Z", None, None, None, None, None),
        ("claimed", "hermes", "codex", "claimed", "trace-claimed", "2024-01-02T00:00:00Z", "codex", "2024-01-02T00:10:00Z", None, None, None),
        ("acked", "codex", "hermes", "acknowledged", "trace-acked", "2024-01-03T00:00:00Z", "hermes", None, "2024-01-03T00:01:00Z", None, None),
        ("rejected", "codex", "hermes", "rejected", "trace-rejected", "2024-01-04T00:00:00Z", "hermes", None, None, "2024-01-04T00:02:00Z", "kept reason"),
    ]
    for message_id, sender, recipient, status, correlation_id, created_at, claimed_by, expires, acked, rejected, reason in legacy_rows:
        conn.execute("""INSERT INTO messages
            (id, sender, recipient, message_type, body_json, status, correlation_id, created_at,
             claimed_by, claim_expires_at, acknowledged_at, rejected_at, rejection_reason)
            VALUES (?, ?, ?, 'legacy', '{}', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, sender, recipient, status, correlation_id, created_at,
             claimed_by, expires, acked, rejected, reason))
    conn.execute("INSERT INTO files VALUES ('kept.txt', 4, 'digest', 'text/plain', 'created', 'updated')")
    conn.execute("""INSERT INTO knowledge VALUES ('project', 'notes.md', 'Notes', 'content', 3,
        'knowledge-digest', 'hermes', 'codex', 'k-created', 'k-updated')""")
    before_indexes = {row[0]: row[1] for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='messages' AND sql IS NOT NULL")}
    conn.commit()
    conn.close()

    repo = MessageRepository(path)
    with repo.connect() as migrated:
        after_rows = migrated.execute("""SELECT id, sender, recipient, message_type, priority, body_json,
            status, correlation_id, requires_user_approval, created_at, claimed_by, claim_expires_at,
            acknowledged_at, rejected_at, rejection_reason FROM messages ORDER BY id""").fetchall()
        assert [tuple(row) for row in after_rows] == sorted([
            (row[0], row[1], row[2], "legacy", "normal", "{}", row[3], row[4], 0,
             row[5], row[6], row[7], row[8], row[9], row[10]) for row in legacy_rows
        ])
        after_indexes = {row[0]: row[1] for row in migrated.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='messages' AND sql IS NOT NULL")}
        assert all(after_indexes[name] == sql for name, sql in before_indexes.items())
        assert tuple(migrated.execute("SELECT path, size, sha256, content_type, created_at, updated_at FROM files").fetchone()) == (
            "kept.txt", 4, "digest", "text/plain", "created", "updated")
        assert tuple(migrated.execute("""SELECT project, path, title, content, version, sha256, created_by,
            updated_by, created_at, updated_at FROM knowledge""").fetchone()) == (
                "project", "notes.md", "Notes", "content", 3, "knowledge-digest", "hermes", "codex", "k-created", "k-updated")
        table_sql = migrated.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()[0].lower()
        assert "'claude'" in table_sql
        assert {row[1] for row in migrated.execute("PRAGMA table_info(messages)")} >= {"in_reply_to", "hop_count"}

    repo.initialize()
    claude = repo.insert(sender="claude", recipient="hermes", message_type="task", body={}, correlation_id="legacy-trace")
    assert claude.sender == "claude" and claude.correlation_id == "legacy-trace"
    assert claude.hop_count == 0 and claude.in_reply_to is None
    with repo.connect() as migrated:
        repeated_indexes = {row[0]: row[1] for row in migrated.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='messages' AND sql IS NOT NULL")}
        assert all(repeated_indexes[name] == sql for name, sql in before_indexes.items())
        assert migrated.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert migrated.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1


def test_correlated_hops_are_persisted_and_capped_at_six(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    parent = repo.insert(sender="hermes", recipient="codex", message_type="task", body={}, correlation_id="thread")
    assert parent.hop_count == 0
    for hop in range(1, 7):
        parent = repo.insert(sender=parent.recipient, recipient=parent.sender, message_type="reply",
                             body={}, in_reply_to=parent.id, correlation_id="thread")
        assert parent.hop_count == hop
        assert repo.get(parent.id).hop_count == hop
    with pytest.raises(MessageError, match="hop limit"):
        repo.insert(sender=parent.recipient, recipient=parent.sender, message_type="reply", body={},
                    in_reply_to=parent.id, correlation_id="unrelated-correlation")


def test_correlated_message_requires_valid_direct_parent_and_preserves_legacy_correlation(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    parent = repo.insert(sender="hermes", recipient="codex", message_type="task", body={}, correlation_id="not-a-message-id")
    with pytest.raises(MessageError, match="parent"):
        repo.insert(sender="codex", recipient="hermes", message_type="reply", body={}, in_reply_to="missing")
    with pytest.raises(MessageError, match="sender"):
        repo.insert(sender="hermes", recipient="codex", message_type="reply", body={}, in_reply_to=parent.id)
    with pytest.raises(MessageError, match="peer"):
        repo.insert(sender="codex", recipient="claude", message_type="reply", body={}, in_reply_to=parent.id)
    legacy = repo.insert(sender="codex", recipient="hermes", message_type="note", body={},
                         correlation_id="not-a-message-id")
    assert legacy.correlation_id == "not-a-message-id" and legacy.in_reply_to is None
    assert parent.correlation_id == "not-a-message-id"


def test_actionable_filter_is_applied_before_limit(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    for index in range(55):
        repo.insert(sender="hermes", recipient="codex", message_type="event", body={"n": index})
    task = repo.insert(sender="hermes", recipient="codex", message_type="TaSk", body={})
    assert repo.list_for_recipient("codex", limit=50)[0].message_type == "event"
    assert [message.id for message in repo.list_for_recipient(
        "codex", limit=1, actionable_only=True)] == [task.id]


def test_claim_ack_and_duplicate_claim_are_atomic(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    msg = repo.insert(sender="hermes", recipient="codex", message_type="x", body={})
    claimed = repo.claim(msg.id, "codex")
    assert claimed.status == "claimed" and claimed.claimed_by == "codex"
    with pytest.raises(InvalidTransition): repo.claim(msg.id, "codex")
    with pytest.raises(InvalidTransition): repo.acknowledge(msg.id, "hermes")
    done = repo.acknowledge(msg.id, "codex")
    assert done.status == "acknowledged" and done.acknowledged_at
    # A retried ack (lost response, restart) returns the committed result, unchanged, marked as a replay.
    assert repo.acknowledge_with_outcome(msg.id, "codex") == (done, True)
    with pytest.raises(InvalidTransition): repo.acknowledge(msg.id, "hermes")
    with pytest.raises(InvalidTransition): repo.reject(msg.id, "codex", "late")


def test_retried_reject_is_idempotent_only_for_the_same_reason(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    msg = repo.insert(sender="hermes", recipient="codex", message_type="x", body={})
    repo.claim(msg.id, "codex")
    done = repo.reject(msg.id, "codex", "out of scope")
    assert repo.reject(msg.id, "codex", "out of scope") == done
    with pytest.raises(InvalidTransition): repo.reject(msg.id, "codex", "another reason")
    with pytest.raises(InvalidTransition): repo.acknowledge(msg.id, "codex")


def test_unclaimed_message_still_cannot_be_finished(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    msg = repo.insert(sender="hermes", recipient="codex", message_type="x", body={})
    with pytest.raises(InvalidTransition): repo.acknowledge(msg.id, "codex")
    with pytest.raises(InvalidTransition): repo.reject(msg.id, "codex", "x")
    assert repo.get(msg.id).status == "pending"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_json_numbers_are_refused_at_send_and_never_stored(tmp_path, value):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    with pytest.raises(MessageError, match="not valid JSON"):
        repo.insert(sender="hermes", recipient="codex", message_type="x", body={"n": value})
    with repo.connect() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_expired_claim_can_be_retried(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3", lease_seconds=10)
    msg = repo.insert(sender="hermes", recipient="codex", message_type="x", body={})
    repo.claim(msg.id, "codex")
    with repo.connect() as conn:
        conn.execute("UPDATE messages SET claim_expires_at=? WHERE id=?", (iso(now_utc() - timedelta(seconds=1)), msg.id))
    retried = repo.claim(msg.id, "codex")
    assert retried.status == "claimed" and retried.claim_expires_at != msg.claim_expires_at


def test_retention_preserves_pending_and_claimed(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    old_a = repo.insert(sender="hermes", recipient="codex", message_type="a", body={})
    repo.claim(old_a.id, "codex"); repo.acknowledge(old_a.id, "codex")
    old_r = repo.insert(sender="hermes", recipient="codex", message_type="r", body={})
    repo.claim(old_r.id, "codex"); repo.reject(old_r.id, "codex", "no")
    pending = repo.insert(sender="hermes", recipient="codex", message_type="p", body={})
    claimed = repo.insert(sender="hermes", recipient="codex", message_type="c", body={}); repo.claim(claimed.id, "codex")
    old = iso(now_utc() - timedelta(days=100))
    with repo.connect() as conn:
        conn.execute("UPDATE messages SET acknowledged_at=? WHERE id=?", (old, old_a.id))
        conn.execute("UPDATE messages SET rejected_at=? WHERE id=?", (old, old_r.id))
    result = repo.cleanup(acknowledged_days=30, rejected_days=90)
    assert result == {"acknowledged_deleted": 1, "rejected_deleted": 1}
    assert repo.get(pending.id).status == "pending"
    assert repo.get(claimed.id).status == "claimed"


def test_first_finish_reports_new_transition(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    msg = repo.insert(sender="hermes", recipient="codex", message_type="x", body={})
    repo.claim(msg.id, "codex")
    message, replayed = repo.acknowledge_with_outcome(msg.id, "codex")
    assert message.status == "acknowledged" and replayed is False


def test_concurrent_ack_retries_make_exactly_one_transition(tmp_path):
    import threading
    repo = MessageRepository(tmp_path / "m.sqlite3")
    msg = repo.insert(sender="hermes", recipient="codex", message_type="x", body={})
    repo.claim(msg.id, "codex")
    barrier, results = threading.Barrier(8), []
    def ack():
        barrier.wait(); results.append(repo.acknowledge_with_outcome(msg.id, "codex"))
    threads = [threading.Thread(target=ack) for _ in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(replayed for _, replayed in results) == [False] + [True] * 7
    assert len({message.acknowledged_at for message, _ in results}) == 1


def test_concurrent_rejects_with_different_reasons_one_wins(tmp_path):
    import threading
    repo = MessageRepository(tmp_path / "m.sqlite3")
    msg = repo.insert(sender="hermes", recipient="codex", message_type="x", body={})
    repo.claim(msg.id, "codex")
    barrier, outcomes = threading.Barrier(2), {}
    def reject(reason):
        barrier.wait()
        try: outcomes[reason] = repo.reject_with_outcome(msg.id, "codex", reason)[1]
        except InvalidTransition: outcomes[reason] = "refused"
    threads = [threading.Thread(target=reject, args=(reason,)) for reason in ("one", "two")]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(map(str, outcomes.values())) == ["False", "refused"]
    winner = next(reason for reason, value in outcomes.items() if value is False)
    assert repo.get(msg.id).rejection_reason == winner


@pytest.mark.parametrize("body", [{"a": [{"b": float("nan")}]}, [1, {"x": float("inf")}], float("-inf")])
@pytest.mark.parametrize("key", [None, "retry-key-1"])
def test_nested_and_keyed_non_json_numbers_are_refused(tmp_path, body, key):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    with pytest.raises(MessageError):
        repo.insert(sender="hermes", recipient="codex", message_type="x", body=body, client_request_id=key)
    with repo.connect() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
