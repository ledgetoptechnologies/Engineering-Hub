import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_message_bus.feedback import (
    FeedbackConflict,
    FeedbackForbidden,
    FeedbackNotFound,
    FeedbackRepository,
    FeedbackStorageError,
    FeedbackValidationError,
    feedback_dict,
    feedback_metadata,
    feedback_page_dict,
    feedback_revision_dict,
    feedback_revision_metadata,
)


def test_submit_and_get_persists_validated_feedback(tmp_path):
    db_path = tmp_path / "shared.sqlite3"
    repository = FeedbackRepository(db_path)

    submitted = repository.submit(
        "codex", "bug", "Search fails", "Search returns an error.", project="agent-bus"
    )
    loaded = FeedbackRepository(db_path).get(submitted.id)

    assert loaded == submitted
    assert loaded.author == "codex"
    assert loaded.kind == "bug"
    assert loaded.title == "Search fails"
    assert loaded.description == "Search returns an error."
    assert loaded.project == "agent-bus"
    assert loaded.severity == "normal"
    assert loaded.status == "open"
    assert loaded.revision == 1
    assert [item.revision for item in repository.history(submitted.id)] == [1]
    assert FeedbackRepository(db_path).get(submitted.id) == loaded


def test_all_three_agents_share_read_access_and_metadata_is_safe(tmp_path):
    repository = FeedbackRepository(tmp_path / "shared.sqlite3")
    item = repository.submit("claude", "info", "Private title", "Private description")

    assert repository.get(item.id).description == "Private description"
    assert repository.list({"author": "claude"}).items == (item,)
    assert feedback_dict(item)["description"] == "Private description"
    assert "title" not in feedback_metadata(item)
    assert "description" not in item.metadata()

    revision = repository.history(item.id)[0]
    assert feedback_revision_dict(revision)["note"] == ""
    assert "note" not in feedback_revision_metadata(revision)
    page = feedback_page_dict(repository.list())
    assert "title" not in page["items"][0]
    assert "description" not in page["items"][0]
    assert feedback_page_dict(repository.list(), include_text=True)["items"][0]["description"] == "Private description"


def test_submit_enforces_bounds_and_accepts_maximum_lengths(tmp_path):
    repository = FeedbackRepository(tmp_path / "shared.sqlite3")
    item = repository.submit(
        "hermes", "improvement", "t" * 200, "d" * 16_000,
        project="p" * 100, severity="urgent",
    )

    assert len(item.title) == 200
    assert len(item.description) == 16_000
    assert len(item.project) == 100

    with pytest.raises(FeedbackValidationError):
        repository.submit("hermes", "bug", "t" * 201, "description")
    with pytest.raises(FeedbackValidationError):
        repository.submit("hermes", "bug", "title", "d" * 16_001)


@pytest.mark.parametrize(
    "overrides",
    [
        {"actor": "Hermes"},
        {"actor": "admin"},
        {"actor": "hermes "},
        {"actor": None},
        {"kind": "task"},
        {"kind": "BUG"},
        {"title": ""},
        {"title": "   "},
        {"description": None},
        {"project": "../private"},
        {"project": "Uppercase"},
        {"project": "-leading"},
        {"project": "trailing-"},
        {"severity": "critical"},
        {"client_request_id": "   "},
    ],
)
def test_submit_rejects_invalid_values_without_echoing_input(tmp_path, overrides):
    repository = FeedbackRepository(tmp_path / "shared.sqlite3")
    payload = {
        "actor": "codex",
        "kind": "bug",
        "title": "A valid title",
        "description": "valid description",
    }
    payload.update(overrides)

    with pytest.raises(FeedbackValidationError) as error:
        repository.submit(**payload)

    assert str(error.value) == FeedbackValidationError.safe_message
    assert "private" not in str(error.value).lower()


def test_idempotency_is_per_author_canonical_and_returns_current_record(tmp_path):
    db_path = tmp_path / "shared.sqlite3"
    repository = FeedbackRepository(db_path)
    first = repository.submit(
        "codex", "bug", "  Same title  ", "same description", client_request_id="  client-1  "
    )
    updated = repository.update(first.id, "codex", 1, "triaged", "review started")

    replay = FeedbackRepository(db_path).submit(
        "codex", "bug", "Same title", "same description", client_request_id="client-1"
    )
    assert replay.id == first.id
    assert replay.status == "triaged"
    assert replay.revision == 2
    assert updated == replay

    with pytest.raises(FeedbackConflict) as conflict:
        repository.submit(
            "codex", "bug", "Same title", "different description", client_request_id="client-1"
        )
    assert str(conflict.value) == FeedbackConflict.safe_message

    other_author = repository.submit(
        "claude", "bug", "Same title", "same description", client_request_id="client-1"
    )
    assert other_author.id != first.id
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT payload_json,payload_hash FROM feedback_entries WHERE id=?", (first.id,)
        ).fetchone()
    canonical = json.dumps(
        {
            "author": "codex",
            "description": "same description",
            "kind": "bug",
            "project": "agent-bus",
            "severity": "normal",
            "title": "Same title",
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert row[0] == canonical
    assert row[1] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_submit_without_idempotency_key_allows_distinct_reports(tmp_path):
    repository = FeedbackRepository(tmp_path / "shared.sqlite3")
    first = repository.submit("hermes", "info", "Same", "Same")
    second = repository.submit("hermes", "info", "Same", "Same")

    assert first.id != second.id
    assert repository.list({"author": "hermes"}).items == tuple(
        sorted((first, second), key=lambda item: (item.created_at, item.id))
    )


def test_update_is_cas_authorized_and_records_immutable_history(tmp_path):
    repository = FeedbackRepository(tmp_path / "shared.sqlite3")
    item = repository.submit("codex", "improvement", "Add search", "Search would help.")

    with pytest.raises(FeedbackForbidden):
        repository.update(item.id, "claude", 1, "planned", "unauthorized")
    with pytest.raises(FeedbackConflict):
        repository.update(item.id, "codex", 2, "planned", "stale")

    changed = repository.update(item.id, "hermes", 1, "triaged", "reviewed for planning")
    with pytest.raises(FeedbackConflict):
        repository.update(item.id, "codex", 1, "planned", "stale after winning update")
    assert changed.status == "triaged"
    assert changed.revision == 2
    history = repository.history(item.id)
    assert [(entry.revision, entry.actor, entry.status, entry.note) for entry in history] == [
        (1, "codex", "open", ""),
        (2, "hermes", "triaged", "reviewed for planning"),
    ]

    with sqlite3.connect(repository.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE feedback_history SET note='rewritten' WHERE feedback_id=? AND revision=1",
                (item.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM feedback_history WHERE feedback_id=?", (item.id,))


def test_update_rejects_invalid_actor_revision_status_and_note(tmp_path):
    repository = FeedbackRepository(tmp_path / "shared.sqlite3")
    item = repository.submit("codex", "bug", "Crash", "It crashes")

    for actor in ("Hermes", "admin", None):
        with pytest.raises(FeedbackValidationError):
            repository.update(item.id, actor, 1, "triaged")
    for revision in (0, -1, True, 1.0):
        with pytest.raises(FeedbackValidationError):
            repository.update(item.id, "codex", revision, "triaged")
    with pytest.raises(FeedbackValidationError):
        repository.update(item.id, "codex", 1, "closed")
    with pytest.raises(FeedbackValidationError):
        repository.update(item.id, "codex", 1, "triaged", "n" * 16_001)


def test_update_failure_rolls_back_status_revision_and_history(tmp_path):
    db_path = tmp_path / "shared.sqlite3"
    repository = FeedbackRepository(db_path)
    item = repository.submit("codex", "bug", "Crash", "It crashes")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_feedback_history BEFORE INSERT ON feedback_history "
            "WHEN NEW.revision=2 BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )

    with pytest.raises(FeedbackStorageError) as error:
        repository.update(item.id, "codex", 1, "triaged", "do not persist")
    assert "injected" not in str(error.value)
    assert repository.get(item.id) == item
    assert len(repository.history(item.id)) == 1


def test_two_repository_instances_serialize_compare_and_swap_updates(tmp_path):
    db_path = tmp_path / "shared.sqlite3"
    first_repository = FeedbackRepository(db_path)
    second_repository = FeedbackRepository(db_path)
    item = first_repository.submit("codex", "bug", "Crash", "It crashes")
    barrier = Barrier(2)

    def attempt(repository, next_status):
        barrier.wait(timeout=5)
        try:
            return repository.update(item.id, "codex", 1, next_status)
        except FeedbackConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: attempt(*pair), [
            (first_repository, "triaged"), (second_repository, "planned")
        ]))

    assert sum(result is not None for result in results) == 1
    current = first_repository.get(item.id)
    assert current.revision == 2
    assert len(first_repository.history(item.id)) == 2


def test_idempotent_submits_concurrent_across_repository_instances(tmp_path):
    db_path = tmp_path / "shared.sqlite3"
    repositories = [FeedbackRepository(db_path), FeedbackRepository(db_path)]
    barrier = Barrier(2)

    def submit(repository):
        barrier.wait(timeout=5)
        return repository.submit(
            "claude", "info", "One report", "Same payload", client_request_id="concurrent-key"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, repositories))

    assert results[0].id == results[1].id
    assert len(repositories[0].list({"author": "claude"}).items) == 1


def test_list_filters_and_opaque_or_id_cursor_are_stable(tmp_path):
    repository = FeedbackRepository(tmp_path / "shared.sqlite3")
    expected = []
    for index in range(5):
        expected.append(
            repository.submit(
                "codex" if index % 2 == 0 else "claude",
                "bug" if index % 2 == 0 else "info",
                f"Report {index}",
                f"Details {index}",
                project="agent-bus" if index < 4 else "other-project",
                severity="high" if index % 2 == 0 else "low",
            )
        )
    filtered = [item for item in repository.list(limit=10).items if item.project == "agent-bus"]

    page_one = repository.list({"project": "agent-bus"}, limit=2)
    assert list(page_one.items) == filtered[:2]
    assert page_one.has_more
    page_two = repository.list({"project": "agent-bus"}, limit=2, after=page_one.next_cursor)
    assert list(page_two.items) == filtered[2:]
    assert not page_two.has_more

    id_page_two = repository.list({"project": "agent-bus"}, limit=2, after=page_one.items[-1].id)
    assert id_page_two == page_two
    all_items = repository.list(limit=10).items
    assert repository.list({"author": "codex", "severity": "high"}, limit=10).items == tuple(
        item for item in all_items if item.author == "codex" and item.severity == "high"
    )


def test_list_rejects_unknown_filters_invalid_limits_and_cursors(tmp_path):
    repository = FeedbackRepository(tmp_path / "shared.sqlite3")
    for limit in (0, 101, True, 1.5):
        with pytest.raises(FeedbackValidationError):
            repository.list(limit=limit)
    with pytest.raises(FeedbackValidationError):
        repository.list({"description": "secret"})
    with pytest.raises(FeedbackValidationError):
        repository.list({"author": "admin"})
    with pytest.raises(FeedbackValidationError):
        repository.list(after="not-an-existing-id")
    with pytest.raises(FeedbackValidationError):
        repository.list(after="fb1_not-valid!!!")


def test_not_found_errors_are_safe(tmp_path):
    repository = FeedbackRepository(tmp_path / "shared.sqlite3")
    with pytest.raises(FeedbackNotFound) as error:
        repository.get("missing-private-id")
    assert "missing-private-id" not in str(error.value)
    with pytest.raises(FeedbackNotFound):
        repository.history("missing-private-id")


def test_initialization_is_idempotent_and_leaves_existing_message_tables_alone(tmp_path):
    db_path = tmp_path / "shared.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        conn.execute("INSERT INTO messages VALUES ('kept', 'untouched')")

    first = FeedbackRepository(db_path)
    item = first.submit("hermes", "info", "Persist", "Keep me")
    second = FeedbackRepository(db_path)

    assert second.get(item.id) == item
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT body FROM messages WHERE id='kept'").fetchone()[0] == "untouched"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
