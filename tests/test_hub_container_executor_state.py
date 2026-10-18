"""Filesystem primitives use only private temporary SQLite fixtures."""
import sqlite3
import os
import stat

import pytest

from hub_container_executor.state import (
    Generation,
    StateCopyError,
    copy_sqlite_generation,
    retention_plan,
)


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
