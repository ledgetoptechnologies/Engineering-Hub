from __future__ import annotations

import asyncio
import hashlib
import io
import re
import sqlite3
import threading
import time

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from agent_message_bus.files import FileRepository
from agent_message_bus.knowledge import KnowledgeRepository
from agent_message_bus.semantic import (
    MAX_FILE_READ_BYTES,
    SemanticIndex,
    SemanticUnavailableError,
    SemanticValidationError,
)


class FakeEmbedder:
    """Deterministic test-only vectors; real model behavior is probed separately."""

    model_name = "test-fake"

    def embed(self, texts):
        for text in texts:
            normalized = set(re.findall(r"[a-z]+", text.casefold()))
            if normalized & {"automobile", "vehicle", "car"}:
                yield [1.0, 0.0, 0.0]
            elif normalized & {"feline", "kitten", "cat"}:
                yield [0.0, 1.0, 0.0]
            else:
                yield [0.0, 0.0, 1.0]


class RecordingFakeEmbedder(FakeEmbedder):
    def __init__(self):
        self.inputs = []

    def embed(self, texts):
        self.inputs.extend(texts)
        yield from super().embed(texts)


def _repos(tmp_path):
    db_path = tmp_path / "messages.sqlite3"
    files_root = tmp_path / "files"
    knowledge = KnowledgeRepository(db_path)
    files = FileRepository(db_path, files_root)
    return db_path, files_root, knowledge, files


def _put(knowledge, path, title, content, expected_version=0):
    return knowledge.put(project="alpha", path=path, title=title, content=content,
                        author="test", expected_version=expected_version)


def test_search_retrieves_paraphrase_and_persists_index(tmp_path):
    db_path, file_root, knowledge, _ = _repos(tmp_path)
    _put(knowledge, "planning/transport.md", "Transportation memo",
         "A compact automobile reduces city congestion.")
    _put(knowledge, "pets/cats.md", "Household pets",
         "A kitten needs food, water, and regular care.")
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())

    result = index.search("vehicle", project="alpha", limit=1)

    assert result[0]["path"] == "planning/transport.md"
    assert result[0]["source"] == "knowledge"
    assert result[0]["digest"] == hashlib.sha256(
        "A compact automobile reduces city congestion.".encode()
    ).hexdigest()
    assert result[0]["score"] > 0.9
    assert index.status()["state"] == "available"

    reopened = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())
    assert reopened.search("car", project="alpha", limit=1)[0]["path"] == "planning/transport.md"


def test_sync_refreshes_cas_updates_and_reconciles_deletes(tmp_path):
    db_path, file_root, knowledge, _ = _repos(tmp_path)
    original = _put(knowledge, "notes/item.md", "Memo", "A vehicle is useful.")
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())
    assert index.search("car")[0]["version"] == 1

    updated = knowledge.put(project="alpha", path="notes/item.md", title="Memo",
                            content="A kitten needs care.", author="test", expected_version=1)
    assert updated.version == 2
    assert index.search("kitten")[0]["version"] == 2
    assert index.search("car")[0]["score"] == 0.0

    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM knowledge WHERE project=? AND path=?", ("alpha", original.path))
    assert index.search("kitten") == []


def test_search_indexes_text_files_and_binary_metadata(tmp_path):
    db_path, file_root, _, files = _repos(tmp_path)
    text = b"A compact automobile reduces city congestion."
    asyncio.run(files.save_upload("guides/transit.txt", UploadFile(
        file=io.BytesIO(text), filename="transit.txt",
        headers=Headers({"content-type": "text/plain"}),
    )))
    binary = b"\x00\x01\x02\xff"
    asyncio.run(files.save_upload("images/vehicle.png", UploadFile(
        file=io.BytesIO(binary), filename="vehicle.png",
        headers=Headers({"content-type": "image/png"}),
    )))
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())

    text_result = index.search("car", project="guides", source="file", limit=10)
    assert any(item["path"] == "transit.txt" and item["content_indexed"] for item in text_result)
    binary_result = index.search("vehicle", project="images", source="file", limit=10)
    image = next(item for item in binary_result if item["path"] == "vehicle.png")
    assert image["content_indexed"] is False
    assert image["metadata_only"] is True


def test_file_content_updates_and_deletes_refresh_semantic_results(tmp_path):
    db_path, file_root, _, files = _repos(tmp_path)
    first = files.put_bytes("guides/transport.txt", b"A compact automobile is useful.",
                            author="test", expected_version=0, content_type="text/plain")
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())
    assert index.search("vehicle", source="file")[0]["version"] == first.version

    updated = files.put_bytes("guides/transport.txt", b"A kitten needs attentive care.",
                              author="test", expected_version=first.version, content_type="text/plain")
    changed = index.search("kitten", source="file")[0]
    assert changed["version"] == updated.version
    assert index.search("vehicle", source="file")[0]["score"] == 0.0

    files.delete("guides/transport.txt", expected_version=updated.version)
    assert index.search("kitten", source="file") == []


def test_search_validates_query_project_source_and_limit(tmp_path):
    db_path, file_root, _, _ = _repos(tmp_path)
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())

    for kwargs in ({"query": "  "}, {"query": "ok", "project": "../bad"},
                   {"query": "ok", "limit": 0}, {"query": "ok", "source": "message"}):
        with pytest.raises(SemanticValidationError):
            index.search(**kwargs)


def test_sync_scans_knowledge_beyond_first_hundred_rows(tmp_path):
    db_path, file_root, knowledge, _ = _repos(tmp_path)
    for number in range(120):
        content = "An automobile has a unique maintenance schedule." if number == 119 else "An ordinary note."
        _put(knowledge, f"batch/{number:03}.md", f"Note {number}", content)
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())

    results = index.search("vehicle", limit=1)

    assert results[0]["path"] == "batch/119.md"
    assert index.status()["indexed_documents"] == 120


def test_file_repository_cursor_scans_every_metadata_page(tmp_path):
    db_path, file_root, _, _ = _repos(tmp_path)
    with sqlite3.connect(db_path) as conn:
        for number in range(300):
            path = f"files/vehicle-{number:03}.bin" if number == 299 else f"files/archive-{number:03}.bin"
            conn.execute(
                "INSERT INTO files(path,size,sha256,content_type,created_at,updated_at,version) "
                "VALUES(?,?,?,?,?,?,1)",
                (path, 12, hashlib.sha256(path.encode()).hexdigest(), "application/octet-stream",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())

    result = index.search("vehicle", project="files", source="file", limit=1)

    assert result[0]["path"] == "vehicle-299.bin"
    assert index.status()["indexed_documents"] == 300


def test_index_scrubs_bearer_and_environment_secret_values(tmp_path):
    db_path, file_root, knowledge, _ = _repos(tmp_path)
    _put(knowledge, "notes/credentials.md", "Credential handling",
         "A vehicle is useful. Authorization: Bearer very-secret-bearer-value "
         "API_TOKEN=very-secret-environment-value "
         "AWS_ACCESS_KEY_ID=AKIA1234567890ABCDEF SERVICE_REGION=very-secret-region-value")
    embedder = RecordingFakeEmbedder()
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=embedder)

    index.search("vehicle")

    combined_inputs = "\n".join(embedder.inputs)
    assert "very-secret-bearer-value" not in combined_inputs
    assert "very-secret-environment-value" not in combined_inputs
    assert "AKIA1234567890ABCDEF" not in combined_inputs
    assert "very-secret-region-value" not in combined_inputs


def test_missing_model_status_is_safe_and_search_fails_explicitly(tmp_path):
    db_path, file_root, _, _ = _repos(tmp_path)
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search")

    status = index.status()

    assert status["state"] == "error"
    assert status["error"] == "model_not_found"
    assert index._embedder is None
    with pytest.raises(SemanticUnavailableError, match=r"unavailable locally \(model_not_found\)"):
        index.search("vehicle")


def test_disabled_status_is_distinct_from_missing_model_error(tmp_path):
    db_path, file_root, _, _ = _repos(tmp_path)
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search",
                          embedder=FakeEmbedder(), enabled=False)

    assert index.status()["state"] == "disabled"
    with pytest.raises(SemanticUnavailableError, match="disabled"):
        index.search("vehicle")


def test_oversized_text_file_is_marked_truncated_and_metadata_only(tmp_path):
    db_path, file_root, _, files = _repos(tmp_path)
    content = b"ordinary text " * ((MAX_FILE_READ_BYTES // 14) + 2)
    files.put_bytes("archive/large.txt", content, author="test", expected_version=0,
                    content_type="text/plain")
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())

    result = next(item for item in index.search("ordinary", project="archive", source="file")
                  if item["path"] == "large.txt")

    assert result["truncated"] is True
    assert result["content_indexed"] is False
    assert result["metadata_only"] is True


def test_large_legacy_knowledge_document_reports_chunk_truncation(tmp_path):
    db_path, file_root, _, _ = _repos(tmp_path)
    content = "ordinary material " * 8_000
    digest = hashlib.sha256(content.encode()).hexdigest()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO knowledge(project,path,title,content,version,sha256,created_by,updated_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("alpha", "legacy/long.md", "Legacy notes", content, 1, digest,
             "test", "test", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())

    result = index.search("ordinary", project="alpha", limit=1)[0]

    assert result["truncated"] is True


def test_project_qualified_files_use_project_and_relative_path(tmp_path):
    db_path, file_root, _, files = _repos(tmp_path)
    files.put_bytes("demo/docs/guide.txt", b"A compact automobile guide.",
                    author="test", expected_version=0, content_type="text/plain")
    files.put_bytes("demo2/docs/guide.txt", b"A compact automobile guide for another project.",
                    author="test", expected_version=0, content_type="text/plain")
    files.put_bytes("legacy-guide.txt", b"A compact automobile guide without a project.",
                    author="test", expected_version=0, content_type="text/plain")
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())

    demo = index.search("vehicle", project="demo", source="file")
    demo2 = index.search("vehicle", project="demo2", source="file")
    global_files = index.search("vehicle", project=None, source="file")

    assert [(item["project"], item["path"]) for item in demo] == [("demo", "docs/guide.txt")]
    assert [(item["project"], item["path"]) for item in demo2] == [("demo2", "docs/guide.txt")]
    assert any(item["project"] is None and item["path"] == "legacy-guide.txt"
               for item in global_files)


@pytest.mark.parametrize("payload", [
    b"ordinary text " * ((MAX_FILE_READ_BYTES // 14) + 2),
    b"\xffordinary invalid utf-8 bytes",
], ids=["oversized", "invalid-utf8"])
def test_repeated_search_preserves_file_indexing_flags_and_vector(tmp_path, payload):
    db_path, file_root, _, files = _repos(tmp_path)
    files.put_bytes("demo/archive/notes.txt", payload, author="test", expected_version=0,
                    content_type="text/plain")
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=FakeEmbedder())

    first = next(item for item in index.search("ordinary", project="demo", source="file")
                 if item["path"] == "archive/notes.txt")
    with sqlite3.connect(index.index_path) as conn:
        chunks_before = conn.execute(
            "SELECT text,vector FROM chunks WHERE source='file' AND project='demo' AND path='archive/notes.txt'"
        ).fetchall()
    second = next(item for item in index.search("ordinary", project="demo", source="file")
                  if item["path"] == "archive/notes.txt")
    with sqlite3.connect(index.index_path) as conn:
        chunks_after = conn.execute(
            "SELECT text,vector FROM chunks WHERE source='file' AND project='demo' AND path='archive/notes.txt'"
        ).fetchall()

    assert (second["truncated"], second["content_indexed"], second["metadata_only"]) == (
        first["truncated"], first["content_indexed"], first["metadata_only"]
    )
    assert chunks_after == chunks_before
    if payload.startswith(b"\xff"):
        assert first["content_indexed"] is False
        assert first["metadata_only"] is True
    else:
        assert first["truncated"] is True
        assert first["content_indexed"] is False
        assert first["metadata_only"] is True


def test_scrubs_quoted_json_and_yaml_secret_keys_before_embedding(tmp_path):
    db_path, file_root, knowledge, _ = _repos(tmp_path)
    _put(knowledge, "notes/config.json", "Configuration",
         '{"password":"DUMMY_SECRET_123", "api-key": "DUMMY_KEY_456"}\n'
         '"client_secret": "DUMMY_SECRET_789"\n'
         "'token': 'DUMMY_YAML_TOKEN_123'")
    embedder = RecordingFakeEmbedder()
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search", embedder=embedder)

    index.search("configuration")

    combined_inputs = "\n".join(embedder.inputs)
    for secret in ("DUMMY_SECRET_123", "DUMMY_KEY_456", "DUMMY_SECRET_789", "DUMMY_YAML_TOKEN_123"):
        assert secret not in combined_inputs


def test_status_reports_readiness_without_hashing_model_files(tmp_path, monkeypatch):
    from agent_message_bus import semantic

    db_path, file_root, _, _ = _repos(tmp_path)
    model_root = tmp_path / "model-cache" / "snapshot"
    model_root.mkdir(parents=True)
    for name in ("model_optimized.onnx", "tokenizer.json", "config.json"):
        (model_root / name).write_bytes(b"model metadata fixture")
    monkeypatch.setattr(semantic, "_fastembed_version", lambda: "test-version")
    monkeypatch.setattr(semantic, "_sha256_file", lambda path: pytest.fail("status hashed model file"))
    index = SemanticIndex(db_path, file_root, cache_root=tmp_path / "search",
                          model_cache_root=model_root.parent)

    status = index.status()
    repeated = index.status()

    assert status["state"] == "available"
    assert status["ready"] is True
    assert status["model_hash"] is None
    assert repeated["ready"] is True
    assert index._embedder is None


def test_synchronization_is_serialized_across_index_instances(tmp_path):
    db_path, file_root, _, _ = _repos(tmp_path)
    cache_root = tmp_path / "search"
    indexes = [SemanticIndex(db_path, file_root, cache_root=cache_root, embedder=FakeEmbedder())
               for _ in range(2)]
    original = [index._synchronize for index in indexes]
    guard = threading.Lock()
    active = 0
    max_active = 0

    def instrument(sync):
        def wrapped(embedder):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                return sync(embedder)
            finally:
                with guard:
                    active -= 1
        return wrapped

    for index, sync in zip(indexes, original):
        index._synchronize = instrument(sync)
    start = threading.Barrier(3)
    failures = []

    def run(index):
        try:
            start.wait()
            index.synchronize()
        except Exception as exc:  # surfaced in the main test thread below
            failures.append(exc)

    workers = [threading.Thread(target=run, args=(index,)) for index in indexes]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=5)

    assert not failures
    assert all(not worker.is_alive() for worker in workers)
    assert max_active == 1
