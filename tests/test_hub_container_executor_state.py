"""Filesystem primitives use only private temporary SQLite fixtures."""
import sqlite3
import os
import stat
import time

import pytest

from hub_container_executor.state import (
    Generation,
    StateCopyError,
    copy_sqlite_generation,
    retention_plan,
)
from hub_container_executor import state as state_module


def private_dir(path):
    path.mkdir(mode=0o700)
    return path


def test_sqlite_backup_api_copies_only_a_coherent_committed_generation(tmp_path):
    root = private_dir(tmp_path / "state")
    source = root / "source.sqlite3"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE valueset(value TEXT NOT NULL)")
    writer.execute("INSERT INTO valueset VALUES ('committed')")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO valueset VALUES ('uncommitted')")
    os.chmod(source, 0o600)
    before = (source.stat().st_size, source.stat().st_mtime_ns)

    destination = root / "candidate.sqlite3"
    copied = copy_sqlite_generation(source, destination, maximum_bytes=2 * 1024 * 1024)
    after = (source.stat().st_size, source.stat().st_mtime_ns)
    assert after == before
    assert copied.size_bytes <= 2 * 1024 * 1024
    assert copied.sha256.startswith("sha256:")
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    reader = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        assert reader.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert reader.execute("SELECT value FROM valueset").fetchall() == [("committed",)]
    finally:
        reader.close()
        writer.rollback()
        writer.close()


def test_sqlite_copy_refuses_existing_destination_bad_permissions_and_oversize(tmp_path):
    root = private_dir(tmp_path / "state")
    source = root / "source.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE t (v INTEGER)")
    connection.commit()
    connection.close()
    os.chmod(source, 0o600)
    destination = root / "copy.sqlite3"
    destination.write_bytes(b"operator-owned")
    with pytest.raises(StateCopyError, match="already exists"):
        copy_sqlite_generation(source, destination, maximum_bytes=1024 * 1024)
    assert destination.read_bytes() == b"operator-owned"

    with pytest.raises(StateCopyError, match="byte limit"):
        copy_sqlite_generation(source, root / "too-small.sqlite3", maximum_bytes=1)
    assert not (root / "too-small.sqlite3").exists()

    os.chmod(source, 0o640)
    with pytest.raises(StateCopyError, match="private regular file"):
        copy_sqlite_generation(source, root / "unprivate.sqlite3", maximum_bytes=1024 * 1024)
    os.chmod(source, 0o600)
    link = root / "linked.sqlite3"
    link.symlink_to(source)
    with pytest.raises(StateCopyError, match="already exists"):
        copy_sqlite_generation(source, link, maximum_bytes=1024 * 1024)
    assert link.is_symlink()

    loose = tmp_path / "loose"
    loose.mkdir(mode=0o750)
    with pytest.raises(StateCopyError, match="private directory"):
        copy_sqlite_generation(source, loose / "copy.sqlite3", maximum_bytes=1024 * 1024)


def test_retention_plan_preserves_current_and_unverified_previous_generation():
    generations = [
        Generation("g1", 6, verified=True),
        Generation("g2", 6, verified=False, previous=True),
        Generation("g3", 6, verified=True, current=True),
    ]
    plan = retention_plan(generations, max_generations=2, max_storage_bytes=12)
    assert plan.delete_ids == ("g1",)
    assert plan.keep_ids == ("g2", "g3")
    with pytest.raises(StateCopyError, match="protected generations exceed"):
        retention_plan(generations, max_generations=1, max_storage_bytes=5)


@pytest.fixture
def copy_paths(tmp_path):
    root = private_dir(tmp_path / "state")
    source = root / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE fixture(v TEXT)")
        db.execute("INSERT INTO fixture VALUES ('unchanged source')")
    source.chmod(0o600)
    return source, root / "copy.sqlite3"


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), -float("inf"), "tomorrow", 10**1000])
def test_invalid_copy_deadline_denied_without_destination(copy_paths, deadline):
    source, destination = copy_paths
    with pytest.raises(StateCopyError, match="invalid state-copy deadline"):
        copy_sqlite_generation(source, destination, maximum_bytes=1024 * 1024,
                               deadline_monotonic=deadline)
    assert not destination.exists()


def test_already_expired_deadline_does_not_open_or_create_destination(copy_paths, monkeypatch):
    source, destination = copy_paths
    monkeypatch.setattr(state_module, "_private_dir", lambda *a: pytest.fail("Expired copy must not inspect paths"))
    with pytest.raises(StateCopyError, match="cooperative deadline"):
        copy_sqlite_generation(source, destination, maximum_bytes=1024 * 1024,
                               deadline_monotonic=time.monotonic() - 1)
    assert not destination.exists()


@pytest.mark.parametrize("where", ["backup", "destination_connect", "destination_integrity"])
def test_sqlite_errors_remove_only_new_partial_destination_and_close_connections(copy_paths, monkeypatch, where):
    source, destination = copy_paths
    original = sqlite3.connect
    before = source.read_bytes()
    opened, closed = [], []

    class Connection(sqlite3.Connection):
        def backup(self, target, **kwargs):
            if where == "backup":
                raise sqlite3.OperationalError("private SQL diagnostic")
            return super().backup(target, **kwargs)

        def execute(self, query, *args, **kwargs):
            if where == "destination_integrity" and self is opened[-1] and len(opened) == 3 and query == "PRAGMA integrity_check":
                raise sqlite3.OperationalError("private SQL diagnostic")
            return super().execute(query, *args, **kwargs)

        def close(self):
            closed.append(self)
            return super().close()

    def connect(database, **kwargs):
        if where == "destination_connect" and str(destination) in str(database):
            raise sqlite3.OperationalError("private connection diagnostic")
        connection = original(database, factory=Connection, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(state_module.sqlite3, "connect", connect)
    with pytest.raises(StateCopyError, match="SQLite backup failed") as error:
        copy_sqlite_generation(source, destination, maximum_bytes=1024 * 1024)
    assert not destination.exists() and source.read_bytes() == before
    assert set(opened) == set(closed)
    assert "private SQL" not in str(error.value) and str(source) not in str(error.value)


def test_backup_page_callback_honors_deadline_and_cleans_partial_copy(copy_paths, monkeypatch):
    source, destination = copy_paths
    original = sqlite3.connect
    now = [100.0]
    callbacks = []
    before = source.read_bytes()
    monkeypatch.setattr(state_module.time, "monotonic", lambda: now[0])

    class Connection(sqlite3.Connection):
        def backup(self, target, *, pages, progress, sleep):
            assert pages == 64 and sleep == .01

            def tick(status, remaining, total):
                callbacks.append((status, remaining, total))
                now[0] = 101.0
                return progress(status, remaining, total)

            return super().backup(target, pages=pages, progress=tick, sleep=sleep)

    monkeypatch.setattr(state_module.sqlite3, "connect", lambda database, **kw: original(database, factory=Connection, **kw))
    with pytest.raises(StateCopyError, match="cooperative deadline"):
        copy_sqlite_generation(source, destination, maximum_bytes=1024 * 1024, deadline_monotonic=101.0)
    assert callbacks and not destination.exists() and source.read_bytes() == before


def test_integrity_check_uses_vm_progress_handler_and_deadline(copy_paths, monkeypatch):
    source, destination = copy_paths
    original = sqlite3.connect
    now = [100.0]
    handlers = []
    monkeypatch.setattr(state_module.time, "monotonic", lambda: now[0])

    class Connection(sqlite3.Connection):
        def set_progress_handler(self, callback, steps):
            assert steps == 1000
            handlers.append(callback)
            return super().set_progress_handler(callback, steps)

        def execute(self, query, *args, **kwargs):
            if query == "PRAGMA integrity_check":
                now[0] = 101.0
                assert handlers[-1]() == 1
                raise sqlite3.OperationalError("interrupted")
            return super().execute(query, *args, **kwargs)

    monkeypatch.setattr(state_module.sqlite3, "connect", lambda database, **kw: original(database, factory=Connection, **kw))
    with pytest.raises(StateCopyError, match="cooperative deadline"):
        copy_sqlite_generation(source, destination, maximum_bytes=1024 * 1024, deadline_monotonic=101.0)
    assert handlers and not destination.exists()


def test_hashing_checks_deadline_during_actual_chunks_and_copy_cleanup(copy_paths, monkeypatch):
    source, destination = copy_paths
    now = [100.0]
    original_hash = state_module.hashlib.sha256
    before = source.read_bytes()
    monkeypatch.setattr(state_module.time, "monotonic", lambda: now[0])
    updates = []

    class Digest:
        def __init__(self):
            self.inner = original_hash()

        def update(self, chunk):
            self.inner.update(chunk)
            updates.append(len(chunk))
            now[0] = 101.0

        def hexdigest(self):
            return self.inner.hexdigest()

    monkeypatch.setattr(state_module.hashlib, "sha256", Digest)
    with pytest.raises(StateCopyError, match="cooperative deadline"):
        copy_sqlite_generation(source, destination, maximum_bytes=1024 * 1024, deadline_monotonic=101.0)
    assert updates and not destination.exists() and source.read_bytes() == before


def test_success_with_future_deadline_preserves_source_and_expected_digest(copy_paths):
    import hashlib
    source, destination = copy_paths
    before = source.read_bytes()
    report = copy_sqlite_generation(source, destination, maximum_bytes=1024 * 1024,
                                    deadline_monotonic=time.monotonic() + 5)
    assert source.read_bytes() == before
    assert report.sha256 == "sha256:" + hashlib.sha256(destination.read_bytes()).hexdigest()


def test_failure_cleanup_never_unlinks_a_replaced_destination(copy_paths, monkeypatch):
    source, destination = copy_paths
    def replace_then_fail(path, **kwargs):
        replacement = destination.with_name("replacement.sqlite3")
        replacement.write_bytes(b"operator replacement")
        replacement.chmod(0o600)
        replacement.replace(destination)
        raise sqlite3.OperationalError("injected SQL error after replacement")

    monkeypatch.setattr(state_module, "_hash_file", replace_then_fail)
    with pytest.raises(StateCopyError, match="SQLite backup failed"):
        copy_sqlite_generation(source, destination, maximum_bytes=1024 * 1024)
    assert destination.read_bytes() == b"operator replacement"


def test_actual_sqlite_vm_progress_interrupts_long_query_at_deadline(copy_paths):
    source, _ = copy_paths
    deadline = time.monotonic() + 0.15
    connection = state_module._copy_connection(source, readonly=True, deadline=deadline)
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            # No injected clock/query/connection: the real SQLite VM calls the
            # installed progress handler while executing this bounded counter.
            connection.execute("""WITH RECURSIVE n(x) AS (
                VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000000
            ) SELECT sum(x) FROM n""").fetchone()
        assert time.monotonic() >= deadline
        assert time.monotonic() - started < 3
    finally:
        connection.close()
