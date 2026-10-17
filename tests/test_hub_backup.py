import hashlib
import json
import os
import sqlite3
import threading
import time

import pytest

from engineering_hub.auth import SessionStore
from engineering_hub.backup import backup_hub, main
from engineering_hub.repository import HubRepository


def test_coherent_wal_backup_and_restored_repository_auth(tmp_path):
    source, target = tmp_path / "hub.db", tmp_path / "backup.db"
    repo = HubRepository(source)
    sessions = SessionStore(source)
    session = sessions.create_session(123)
    # Keep a separate connection open to prevent last-close WAL checkpointing.
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    task = repo.create_task("human:123", "3d-viewer", "PRIVATE fixture title", "assignment-1")
    writer.execute("CREATE TABLE concurrent_committed (value INTEGER)")
    writer.execute("INSERT INTO concurrent_committed VALUES(42)")
    writer.commit()
    before = source.read_bytes()
    report = backup_hub(source, target)
    assert source.read_bytes() == before
    assert report["quick_check"] == "ok"
    assert report["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert "PRIVATE" not in json.dumps(report)
    assert session["cookie_token"] not in json.dumps(report)
    with sqlite3.connect(target) as restored:
        assert restored.execute("SELECT value FROM concurrent_committed").fetchone() == (42,)
    assert HubRepository(target).get_task(task["id"])["title"] == "PRIVATE fixture title"
    assert SessionStore(target).session_for(session["cookie_token"])["github_id"] == 123
    writer.close()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_no_overwrite_same_path_and_missing_source(tmp_path):
    source, target = tmp_path / "hub.db", tmp_path / "existing.db"
    HubRepository(source)
    target.write_bytes(b"preserve-existing")
    with pytest.raises(FileExistsError):
        backup_hub(source, target)
    assert target.read_bytes() == b"preserve-existing"
    with pytest.raises(ValueError):
        backup_hub(source, source)
    with pytest.raises(FileNotFoundError):
        backup_hub(tmp_path / "missing.db", tmp_path / "unused.db")
    assert not (tmp_path / "unused.db").exists()


def test_bad_database_removes_only_new_partial(tmp_path):
    source, target = tmp_path / "bad.db", tmp_path / "partial.db"
    source.write_bytes(b"not-a-database-private-content")
    neighbour = tmp_path / "other.db"
    neighbour.write_bytes(b"do-not-touch")
    with pytest.raises(sqlite3.Error):
        backup_hub(source, target)
    assert not target.exists()
    assert neighbour.read_bytes() == b"do-not-touch"
    assert source.read_bytes() == b"not-a-database-private-content"


def test_cli_reports_only_digest_integrity_metadata(tmp_path, capsys):
    source, target = tmp_path / "hub.db", tmp_path / "backup.db"
    HubRepository(source)
    main([str(source), str(target)])
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"sha256", "size_bytes", "quick_check", "permissions"}
    with pytest.raises(SystemExit) as error:
        main([str(source), str(target)])
    assert error.value.code == 1
    assert str(source) not in capsys.readouterr().err


def test_backup_is_transactionally_coherent_while_wal_writer_runs(tmp_path):
    source, target = tmp_path / "hub.db", tmp_path / "snapshot.db"
    HubRepository(source)
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE snapshot_pair (id INTEGER PRIMARY KEY, version INTEGER)")
        db.executemany("INSERT INTO snapshot_pair VALUES (?,0)", [(1,), (2,)])
    stop, ready = threading.Event(), threading.Event()
    errors = []

    def write_transactions():
        try:
            with sqlite3.connect(source, timeout=5) as db:
                while not stop.is_set():
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE snapshot_pair SET version=version+1")
                    db.commit()
                    ready.set()
                    time.sleep(0.001)
        except Exception as error:
            errors.append(type(error).__name__)

    thread = threading.Thread(target=write_transactions)
    thread.start()
    try:
        assert ready.wait(5)
        backup_hub(source, target)
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    with sqlite3.connect(target) as restored:
        values = [row[0] for row in restored.execute("SELECT version FROM snapshot_pair ORDER BY id")]
    assert values[0] >= 1 and values[0] == values[1]
