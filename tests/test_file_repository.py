import hashlib
import os
import sqlite3
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest

from agent_message_bus import files as files_module
from agent_message_bus.files import (
    FileConflict,
    FileIntegrityError,
    FileNotFound,
    FilePathError,
    FileRepository,
    FileTooLarge,
)


def test_migrates_legacy_schema_without_losing_flat_file(tmp_path):
    db_path = tmp_path / "messages.sqlite3"
    root = tmp_path / "files"
    root.mkdir()
    content = b"legacy fixture"
    (root / "legacy.txt").write_bytes(content)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, sha256 TEXT NOT NULL, "
            "content_type TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            ("legacy.txt", len(content), hashlib.sha256(content).hexdigest(), "text/plain", "created", "updated"),
        )

    repository = FileRepository(db_path, root)
    metadata, actual = repository.read_bytes("legacy.txt")
    assert actual == content
    assert metadata.version == 1
    assert metadata.created_by is None
    assert metadata.updated_by is None
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    assert {"version", "created_by", "updated_by", "storage_key"} <= columns
    assert (root / "legacy.txt").read_bytes() == content


def test_first_legacy_update_captures_an_immutable_readable_revision(tmp_path):
    db_path = tmp_path / "messages.sqlite3"
    root = tmp_path / "files"
    root.mkdir()
    legacy = b"before the migration"
    (root / "old.txt").write_bytes(legacy)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, sha256 TEXT NOT NULL, "
            "content_type TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            ("old.txt", len(legacy), hashlib.sha256(legacy).hexdigest(), "text/plain", "created", "updated"),
        )
    repository = FileRepository(db_path, root)
    changed = repository.put_bytes("old.txt", b"after", author="codex", expected_version=1)
    prior, prior_bytes = repository.read_bytes("old.txt", version=1)
    assert changed.version == 2
    assert prior.version == 1
    assert prior_bytes == legacy


def test_failed_first_update_checkpoints_legacy_bytes_before_mirror_mutation(tmp_path, monkeypatch):
    db_path = tmp_path / "messages.sqlite3"
    root = tmp_path / "files"
    root.mkdir()
    legacy = b"preserve this fixture"
    (root / "legacy.bin").write_bytes(legacy)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, sha256 TEXT NOT NULL, "
            "content_type TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            ("legacy.bin", len(legacy), hashlib.sha256(legacy).hexdigest(), "application/octet-stream", "c", "u"),
        )
    repository = FileRepository(db_path, root)

    def fail_install(path, data, *, allow_replace):
        raise RuntimeError("simulated interruption before mirror install")

    monkeypatch.setattr(repository, "_install_mirror", fail_install)
    with pytest.raises(RuntimeError):
        repository.put_bytes("legacy.bin", b"replacement", author="agent", expected_version=1)
    metadata, content = repository.read_bytes("legacy.bin")
    assert metadata.version == 1
    assert content == legacy
    with sqlite3.connect(db_path) as conn:
        storage_key = conn.execute("SELECT storage_key FROM files WHERE path='legacy.bin'").fetchone()[0]
    assert storage_key == metadata.sha256


def test_delete_requires_matching_version_and_keeps_retrievable_history(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    current = repository.put_bytes("delete-me", b"keep me", author="agent", expected_version=0)
    with pytest.raises(FileConflict):
        repository.delete("delete-me", expected_version=0)
    deleted = repository.delete("delete-me", expected_version=current.version)
    assert deleted == current
    with pytest.raises(FileNotFound):
        repository.get("delete-me")
    metadata, content = repository.read_bytes("delete-me", version=current.version)
    assert metadata == current
    assert content == b"keep me"


def test_read_bytes_honors_size_limit_before_returning_content(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    repository.put_bytes("bounded", b"12345", author="agent", expected_version=0)
    with pytest.raises(FileTooLarge):
        repository.read_bytes("bounded", max_bytes=4)


def test_create_update_cas_provenance_and_version_history(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    created = repository.put_bytes("docs/readme.txt", b"first", author="hermes", expected_version=0,
                                   content_type="text/plain")
    assert (created.version, created.created_by, created.updated_by) == (1, "hermes", "hermes")

    with pytest.raises(FileConflict):
        repository.put_bytes("docs/readme.txt", b"stale", author="codex", expected_version=0)
    updated = repository.put_bytes("docs/readme.txt", b"second", author="claude", expected_version=1,
                                   content_type="text/plain")
    assert (updated.version, updated.created_by, updated.updated_by) == (2, "hermes", "claude")
    assert repository.read_bytes("docs/readme.txt") == (updated, b"second")
    historical, old_bytes = repository.read_bytes("docs/readme.txt", version=1)
    assert historical.version == 1
    assert historical.updated_by == "hermes"
    assert old_bytes == b"first"
    assert [item.version for item in repository.history("docs/readme.txt")] == [2, 1]


def test_two_repository_instances_serialize_explicit_cas_writers(tmp_path):
    db_path = tmp_path / "messages.sqlite3"
    root = tmp_path / "files"
    first = FileRepository(db_path, root)
    second = FileRepository(db_path, root)
    first.put_bytes("shared.bin", b"initial", author="seed", expected_version=0)

    def write(repository, value):
        try:
            return repository.put_bytes("shared.bin", value, author=value.decode(), expected_version=1)
        except FileConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: write(*args), [(first, b"writer-a"), (second, b"writer-b")]))
    assert sum(result is not None for result in results) == 1
    current = first.get("shared.bin")
    assert current.version == 2
    assert first.read_bytes("shared.bin")[0] == current
    assert first.read_bytes("shared.bin")[1] in {b"writer-a", b"writer-b"}
    assert [item.version for item in first.history("shared.bin")] == [2, 1]


def test_list_prefix_and_after_cursor_are_stable_and_discoverable(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    for name in ("a/1", "a/2", "a/3", "b/1"):
        repository.put_bytes(name, name.encode(), author="agent", expected_version=0)
    page1 = repository.list(prefix="a/", limit=2)
    page2 = repository.list(prefix="a/", after=page1[-1].path, limit=2)
    assert [item.path for item in page1] == ["a/1", "a/2"]
    assert [item.path for item in page2] == ["a/3"]
    assert [item.path for item in repository.list(limit=10)] == ["a/1", "a/2", "a/3", "b/1"]


@pytest.mark.parametrize("content", [b"plain text\n", b"\x00\xff\x10binary"])
def test_put_and_read_text_and_binary_bytes(tmp_path, content):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    metadata = repository.put_bytes("nested/item.bin", content, author="agent", expected_version=0)
    returned_metadata, returned_bytes = repository.read_bytes("nested/item.bin")
    assert returned_metadata == metadata
    assert returned_bytes == content
    assert metadata.sha256 == hashlib.sha256(content).hexdigest()


def test_read_rejects_corrupt_content_before_returning_bytes(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    metadata = repository.put_bytes("corrupt.bin", b"original", author="agent", expected_version=0)
    blob = tmp_path / "files" / ".agent-message-bus" / "objects" / metadata.sha256
    blob.chmod(0o644)
    blob.write_bytes(b"tampered")
    with pytest.raises(FileIntegrityError):
        repository.read_bytes("corrupt.bin")
    with pytest.raises(FileIntegrityError):
        repository.open_for_read("corrupt.bin")


def test_rejects_symlinks_and_path_traversal(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "files" / "linked").symlink_to(outside, target_is_directory=True)
    for unsafe in ("../escape", "/absolute", "linked/escape", "a/../b"):
        with pytest.raises(FilePathError):
            repository.put_bytes(unsafe, b"no", author="agent", expected_version=0)
    assert not (outside / "escape").exists()


def test_rejects_symlink_at_destination_without_touching_target(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside data")
    (tmp_path / "files" / "alias.txt").symlink_to(outside)
    with pytest.raises(FilePathError):
        repository.put_bytes("alias.txt", b"replacement", author="agent", expected_version=0)
    assert outside.read_bytes() == b"outside data"
    assert (tmp_path / "files" / "alias.txt").is_symlink()


def test_failed_sql_update_restores_original_mirror_and_metadata(tmp_path):
    db_path = tmp_path / "messages.sqlite3"
    root = tmp_path / "files"
    repository = FileRepository(db_path, root)
    before = repository.put_bytes("kept.txt", b"original", author="agent", expected_version=0)
    objects = root / ".agent-message-bus" / "objects"
    objects_before = {path.name for path in objects.iterdir() if not path.name.startswith(".tmp-")}
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_file_update BEFORE UPDATE ON files "
            "BEGIN SELECT RAISE(ABORT, 'injected update failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.put_bytes("kept.txt", b"replacement", author="agent", expected_version=1)
    assert (root / "kept.txt").read_bytes() == b"original"
    assert repository.get("kept.txt") == before
    assert repository.read_bytes("kept.txt")[1] == b"original"
    assert {path.name for path in objects.iterdir() if not path.name.startswith(".tmp-")} == objects_before


@pytest.mark.parametrize("operation", ["update", "upload", "delete"])
@pytest.mark.parametrize("trigger_action", ["ABORT", "ROLLBACK"])
@pytest.mark.asyncio
async def test_failed_mirror_mutation_serializes_competing_writer(
    tmp_path, monkeypatch, operation, trigger_action,
):
    from agent_message_bus.files import _RemovedMirror

    db_path = tmp_path / "messages.sqlite3"
    root = tmp_path / "files"
    failing = FileRepository(db_path, root)
    winner = FileRepository(db_path, root)
    path = "contended.txt"
    failing.put_bytes(path, b"original", author="seed", expected_version=0)

    if operation == "delete":
        trigger = (
            "CREATE TRIGGER fail_file_delete BEFORE DELETE ON files "
            f"BEGIN SELECT RAISE({trigger_action}, 'injected delete failure'); END"
        )
    else:
        trigger = (
            "CREATE TRIGGER fail_file_update BEFORE UPDATE ON files "
            "WHEN NEW.updated_by = 'failing' "
            f"BEGIN SELECT RAISE({trigger_action}, 'injected update failure'); END"
        )
    with sqlite3.connect(db_path) as conn:
        conn.execute(trigger)

    winner_started = threading.Event()
    winner_lock_attempted = threading.Event()
    winner_at_mirror = threading.Event()
    allow_winner_install = threading.Event()
    winner_thread_id: int | None = None
    original_mutation_lock = files_module._serialize_file_mutations

    def track_winner_lock_attempt(db_path):
        if threading.get_ident() == winner_thread_id:
            winner_lock_attempted.set()
        return original_mutation_lock(db_path)

    monkeypatch.setattr(files_module, "_serialize_file_mutations", track_winner_lock_attempt)
    original_winner_install = winner._install_mirror

    def pause_winner_install(*args, **kwargs):
        winner_at_mirror.set()
        assert allow_winner_install.wait(timeout=5)
        return original_winner_install(*args, **kwargs)

    monkeypatch.setattr(winner, "_install_mirror", pause_winner_install)
    reached_winner_before_restore: list[bool] = []
    future_box = []

    def competing_writer():
        nonlocal winner_thread_id
        winner_thread_id = threading.get_ident()
        winner_started.set()
        return winner.put_bytes(path, b"winner", author="winner", expected_version=1)

    def interleave_restore(restore):
        future = pool.submit(competing_writer)
        future_box.append(future)
        try:
            assert winner_started.wait(timeout=2)
            if not winner_lock_attempted.wait(timeout=2):
                assert winner_at_mirror.wait(timeout=2)
            reached = winner_at_mirror.wait(timeout=0.5)
            reached_winner_before_restore.append(reached)
            if reached:
                allow_winner_install.set()
                future.result(timeout=5)
            restore()
        finally:
            allow_winner_install.set()

    if operation in {"update", "upload"}:
        original_failing_install = failing._install_mirror

        def install_with_interleaved_rollback(*args, **kwargs):
            change = original_failing_install(*args, **kwargs)
            original_rollback = change.rollback
            monkeypatch.setattr(
                change, "rollback",
                lambda: interleave_restore(original_rollback),
            )
            return change

        monkeypatch.setattr(failing, "_install_mirror", install_with_interleaved_rollback)
    else:
        original_removed_rollback = _RemovedMirror.rollback
        monkeypatch.setattr(
            _RemovedMirror, "rollback",
            lambda removed: interleave_restore(lambda: original_removed_rollback(removed)),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        if operation == "update":
            with pytest.raises(sqlite3.IntegrityError, match="injected update failure"):
                failing.put_bytes(path, b"loser", author="failing", expected_version=1)
        elif operation == "upload":
            class Upload:
                content_type = "text/plain"

                def __init__(self):
                    self.stream = BytesIO(b"loser")

                async def read(self, size=-1):
                    return self.stream.read(size)

            with pytest.raises(sqlite3.IntegrityError, match="injected update failure"):
                await failing.save_upload(path, Upload(), author="failing", expected_version=1)
        else:
            with pytest.raises(sqlite3.IntegrityError, match="injected delete failure"):
                failing.delete(path, expected_version=1)

        assert len(future_box) == 1
        winning_metadata = future_box[0].result(timeout=5)

    assert reached_winner_before_restore == [False]
    assert winning_metadata.version == 2
    current_metadata, current_bytes = winner.read_bytes(path)
    assert current_metadata == winning_metadata
    assert current_bytes == b"winner"
    assert (root / path).read_bytes() == b"winner"
    assert [item.version for item in winner.history(path)] == [2, 1]


@pytest.mark.asyncio
async def test_oversized_stream_does_not_replace_existing_file(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files", max_file_bytes=4)
    original = repository.put_bytes("limited.bin", b"safe", author="agent", expected_version=0)

    class Upload:
        content_type = "application/octet-stream"

        def __init__(self):
            self.stream = BytesIO(b"too large")

        async def read(self, size=-1):
            return self.stream.read(size)

    with pytest.raises(FileTooLarge):
        await repository.save_upload("limited.bin", Upload(), expected_version=1, author="agent")
    assert repository.get("limited.bin") == original
    assert repository.read_bytes("limited.bin")[1] == b"safe"
    assert (tmp_path / "files" / "limited.bin").read_bytes() == b"safe"


def test_recreate_after_delete_uses_monotonic_revision_and_rejects_stale_cas(tmp_path):
    db_path = tmp_path / "messages.sqlite3"
    root = tmp_path / "files"
    repository = FileRepository(db_path, root)
    first = repository.put_bytes("aba.bin", b"first", author="agent", expected_version=0)
    repository.delete("aba.bin", expected_version=first.version)

    repository = FileRepository(db_path, root)
    recreated = repository.put_bytes("aba.bin", b"recreated", author="agent", expected_version=0)

    assert recreated.version == 2
    with pytest.raises(FileConflict):
        repository.put_bytes("aba.bin", b"stale v1", author="stale", expected_version=1)
    assert repository.read_bytes("aba.bin")[1] == b"recreated"
    assert [item.version for item in repository.history("aba.bin")] == [2, 1]


def test_rejected_cas_does_not_publish_unique_immutable_object(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    repository.put_bytes("cas.bin", b"current", author="agent", expected_version=0)
    objects = tmp_path / "files" / ".agent-message-bus" / "objects"
    before = {path.name for path in objects.iterdir() if not path.name.startswith(".tmp-")}

    with pytest.raises(FileConflict):
        repository.put_bytes("cas.bin", b"a unique rejected body", author="stale", expected_version=0)

    after = {path.name for path in objects.iterdir() if not path.name.startswith(".tmp-")}
    assert after == before


def test_failed_writer_cleanup_keeps_object_referenced_by_concurrent_repository(tmp_path, monkeypatch):
    db_path = tmp_path / "messages.sqlite3"
    root = tmp_path / "files"
    failing = FileRepository(db_path, root)
    concurrent = FileRepository(db_path, root)
    payload = b"shared by the winning repository"
    cleanup = failing._cleanup_unreferenced_objects

    def fail_mirror(path, data, *, allow_replace):
        raise RuntimeError("simulated post-publication failure")

    def concurrent_commit_then_cleanup(conn, keys):
        concurrent.put_bytes("winner.bin", payload, author="winner", expected_version=0)
        cleanup(conn, keys)

    monkeypatch.setattr(failing, "_install_mirror", fail_mirror)
    monkeypatch.setattr(failing, "_cleanup_unreferenced_objects", concurrent_commit_then_cleanup)
    with pytest.raises(RuntimeError, match="post-publication"):
        failing.put_bytes("loser.bin", payload, author="loser", expected_version=0)

    metadata, actual = concurrent.read_bytes("winner.bin")
    object_path = root / ".agent-message-bus" / "objects" / metadata.sha256
    assert actual == payload
    assert object_path.is_file()


@pytest.mark.asyncio
async def test_save_upload_streams_bounded_chunks_without_materializing_full_payload(tmp_path):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    total_size = 8 * 1024 * 1024

    class GeneratedUpload:
        content_type = "application/octet-stream"

        def __init__(self):
            self.remaining = total_size
            self.digest = hashlib.sha256()
            self.read_sizes = []
            self.sequence = 0

        async def read(self, size=-1):
            self.read_sizes.append(size)
            assert 0 < size <= 1024 * 1024
            amount = min(size, self.remaining)
            if not amount:
                return b""
            chunk = bytes([self.sequence % 251]) * amount
            self.sequence += 1
            self.remaining -= amount
            self.digest.update(chunk)
            return chunk

    upload = GeneratedUpload()
    tracemalloc.start()
    try:
        metadata = await repository.save_upload(
            "streamed.bin", upload, author="agent", expected_version=0
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert metadata.size == total_size
    assert metadata.sha256 == upload.digest.hexdigest()
    assert upload.read_sizes and all(size == 1024 * 1024 for size in upload.read_sizes)
    assert peak < total_size // 2
    assert (tmp_path / "files" / "streamed.bin").stat().st_size == total_size


def test_open_for_read_verifies_in_constant_memory_before_returning_descriptor(tmp_path, monkeypatch):
    repository = FileRepository(tmp_path / "messages.sqlite3", tmp_path / "files")
    payload = b"streamed read" * (256 * 1024)
    created = repository.put_bytes("download.bin", payload, author="agent", expected_version=0)

    def buffered_verifier_must_not_be_used(*args, **kwargs):
        raise AssertionError("open_for_read must not buffer verified bytes")

    monkeypatch.setattr(repository, "_read_verified_fd", buffered_verifier_must_not_be_used)
    metadata, fd = repository.open_for_read("download.bin")
    try:
        assert metadata == created
        assert len(os.read(fd, 1024 * 1024)) == 1024 * 1024
    finally:
        os.close(fd)


def test_legacy_checkpoint_streams_without_using_bounded_read_api(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.sqlite3"
    root = tmp_path / "legacy-files"
    root.mkdir()
    original = b"legacy data" * (256 * 1024)
    (root / "legacy.bin").write_bytes(original)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, sha256 TEXT NOT NULL, "
            "content_type TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            ("legacy.bin", len(original), hashlib.sha256(original).hexdigest(),
             "application/octet-stream", "created", "updated"),
        )
    repository = FileRepository(db_path, root)
    read_row_bytes = repository._read_row_bytes
    monkeypatch.setattr(
        repository, "_read_row_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unbounded legacy read")),
    )

    updated = repository.put_bytes("legacy.bin", b"new", author="agent", expected_version=1)

    monkeypatch.setattr(repository, "_read_row_bytes", read_row_bytes)
    old_metadata, old_bytes = repository.read_bytes("legacy.bin", max_bytes=len(original), version=1)
    assert updated.version == 2
    assert old_metadata.version == 1
    assert old_bytes == original
