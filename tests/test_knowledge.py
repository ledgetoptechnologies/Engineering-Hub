import hashlib
import sqlite3

import pytest

from agent_message_bus.knowledge import (
    MAX_CONTENT_BYTES,
    KnowledgeConflict,
    KnowledgeError,
    KnowledgeIntegrityError,
    KnowledgeNotFound,
    KnowledgeRepository,
)


def test_put_get_versioning_provenance_hash_and_durability(tmp_path):
    db_path = tmp_path / "bus.sqlite3"
    repo = KnowledgeRepository(db_path)
    first = repo.put(
        project="agent-bus",
        path="architecture/overview.md",
        title="Architecture",
        content="Shared durable knowledge.",
        author="hermes",
        expected_version=0,
    )
    assert first.version == 1
    assert first.sha256 == hashlib.sha256(first.content.encode()).hexdigest()
    assert first.created_by == first.updated_by == "hermes"

    updated = repo.put(
        project="agent-bus",
        path="architecture/overview.md",
        title="Architecture overview",
        content="Updated shared durable knowledge.",
        author="claude",
        expected_version=1,
    )
    assert updated.version == 2
    assert updated.created_by == "hermes"
    assert updated.updated_by == "claude"
    assert updated.created_at == first.created_at
    assert updated.sha256 == hashlib.sha256(updated.content.encode()).hexdigest()

    reopened = KnowledgeRepository(db_path)
    assert reopened.get("agent-bus", "architecture/overview.md") == updated


def test_stale_or_missing_expected_version_never_overwrites(tmp_path):
    repo = KnowledgeRepository(tmp_path / "bus.sqlite3")
    original = repo.put(project="shared", path="facts.md", title="Facts", content="original",
                        author="hermes", expected_version=0)

    for expected in (0, original.version + 1):
        with pytest.raises(KnowledgeConflict) as conflict:
            repo.put(project="shared", path="facts.md", title="Lost update", content="replacement",
                     author="codex", expected_version=expected)
        assert conflict.value.current_version == 1

    assert repo.get("shared", "facts.md") == original
    with pytest.raises(KnowledgeConflict) as missing:
        repo.put(project="shared", path="new.md", title="New", content="not created",
                 author="codex", expected_version=1)
    assert missing.value.current_version == 0
    with pytest.raises(KnowledgeNotFound):
        repo.get("shared", "new.md")


def test_list_prefix_and_literal_search_are_bounded_and_discoverable(tmp_path):
    repo = KnowledgeRepository(tmp_path / "bus.sqlite3")
    docs = [
        ("notes/roadmap.md", "Roadmap", "Search token: 100%_ready"),
        ("notes/design.md", "Design", "A search token in a design note."),
        ("notes-old/archive.md", "Old notes", "A historical item."),
        ("misc.md", "Miscellaneous", "Different project."),
    ]
    for path, title, content in docs:
        repo.put(project="project-a", path=path, title=title, content=content,
                 author="hermes", expected_version=0)
    repo.put(project="project-b", path="elsewhere.md", title="Across projects",
             content="A search token lives here too.", author="codex", expected_version=0)

    listed = repo.list(project="project-a", path_prefix="notes", limit=500)
    assert [item.path for item in listed] == ["notes/design.md", "notes/roadmap.md"]
    assert [item.path for item in repo.list(project="project-a", limit=1)] == ["misc.md"]

    exact = repo.search(query="100%_ready", project="project-a")
    assert [item.path for item in exact] == ["notes/roadmap.md"]
    assert [item.path for item in repo.search(query="ready 100%", project="project-a")] == ["notes/roadmap.md"]
    shared = repo.search(query="SEARCH TOKEN", limit=1)
    assert len(shared) == 1
    assert shared[0].project == "project-a"
    assert repo.search(query="SEARCH TOKEN", limit=1000)
    assert len(repo.search(query="SEARCH TOKEN", limit=1000)) == 3


def test_rejects_unsafe_paths_invalid_project_and_oversized_content(tmp_path):
    repo = KnowledgeRepository(tmp_path / "bus.sqlite3")
    for path in ("../escape.md", "/absolute.md", "a/../b.md", "a//b.md", "a\\b.md", "./note.md", ""):
        with pytest.raises(KnowledgeError):
            repo.put(project="shared", path=path, title="Unsafe", content="x",
                     author="hermes", expected_version=0)
    for project in ("../shared", "bad/name", "", "contains space"):
        with pytest.raises(KnowledgeError):
            repo.put(project=project, path="safe.md", title="Unsafe", content="x",
                     author="hermes", expected_version=0)
    with pytest.raises(KnowledgeError):
        repo.put(project="shared", path="large.md", title="Large", content="é" * (MAX_CONTENT_BYTES // 2 + 1),
                 author="hermes", expected_version=0)
    assert repo.list(project="shared") == []


def test_get_detects_tampered_content_digest(tmp_path):
    db_path = tmp_path / "bus.sqlite3"
    repo = KnowledgeRepository(db_path)
    repo.put(project="shared", path="integrity.md", title="Integrity", content="trusted",
             author="hermes", expected_version=0)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE knowledge SET content=? WHERE project=? AND path=?",
                     ("tampered", "shared", "integrity.md"))
    with pytest.raises(KnowledgeIntegrityError):
        repo.get("shared", "integrity.md")


def test_list_and_search_return_shared_provenance_and_validate_inputs(tmp_path):
    repo = KnowledgeRepository(tmp_path / "bus.sqlite3")
    entry = repo.put(project="ops", path="runbook.md", title="Runbook", content="Restart worker safely.",
                     author="claude", expected_version=0)
    assert repo.list(project="ops")[0].updated_by == "claude"
    assert repo.search(query="restart", project="ops")[0] == entry
    for query in ("", "  ", "x" * 201, " ".join(["x"] * 21)):
        with pytest.raises(KnowledgeError):
            repo.search(query=query)
    for expected in (-1, True):
        with pytest.raises(KnowledgeError):
            repo.put(project="ops", path="bad-version.md", title="Invalid", content="x",
                     author="hermes", expected_version=expected)
