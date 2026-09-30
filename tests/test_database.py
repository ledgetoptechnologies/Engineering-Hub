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


def test_claim_ack_and_duplicate_claim_are_atomic(tmp_path):
    repo = MessageRepository(tmp_path / "m.sqlite3")
    msg = repo.insert(sender="hermes", recipient="codex", message_type="x", body={})
    claimed = repo.claim(msg.id, "codex")
    assert claimed.status == "claimed" and claimed.claimed_by == "codex"
    with pytest.raises(InvalidTransition): repo.claim(msg.id, "codex")
    with pytest.raises(InvalidTransition): repo.acknowledge(msg.id, "hermes")
    done = repo.acknowledge(msg.id, "codex")
    assert done.status == "acknowledged" and done.acknowledged_at
    with pytest.raises(InvalidTransition): repo.acknowledge(msg.id, "codex")


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
