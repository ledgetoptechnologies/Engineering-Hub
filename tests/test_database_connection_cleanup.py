"""Repository-owned SQLite handles close; borrowed/public handles stay caller-owned."""
import gc
import sqlite3
import warnings
from contextlib import closing

import pytest

from agent_message_bus import database
from agent_message_bus.database import InvalidTransition, MessageError, MessageRepository, NotFound
from agent_message_bus.knowledge import KnowledgeConflict, KnowledgeNotFound, KnowledgeRepository


@pytest.fixture
def tracked_connections(monkeypatch):
    opened = []
    raw_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            try:
                return super().close()
            finally:
                self.closed = True

    def connect(*args, **kwargs):
        conn = raw_connect(*args, **kwargs, factory=TrackedConnection)
        opened.append(conn)
        return conn

    monkeypatch.setattr(database.sqlite3, "connect", connect)
    yield opened
    # Do not rely on GC to release a handle even if a test assertion fails.
    for conn in opened:
        if not conn.closed:
            conn.close()


def test_repository_methods_close_all_owned_handles(tmp_path, tracked_connections):
    opened = tracked_connections
    repo = MessageRepository(tmp_path / "messages.sqlite3")

    def closed():
        assert opened and all(conn.closed for conn in opened)

    closed()  # initialization used to leak its connection.
    message = repo.insert(sender="hermes", recipient="codex", message_type="request",
                          body={"test": True}, client_request_id="cleanup-test")
    closed()
    replayed, replay = repo.insert_with_outcome(
        sender="hermes", recipient="codex", message_type="request",
        body={"test": True}, client_request_id="cleanup-test")
    assert replay and replayed.id == message.id
    closed()
    assert repo.get(message.id) == message
    closed()
    assert repo.get_send_receipt("hermes", "cleanup-test")["message_id"] == message.id
    closed()
    assert repo.list_for_recipient("codex") == [message]
    closed()
    assert repo.delivery_head() == repo.sequence_for(message.id) == repo.oldest_available_seq()
    closed()
    page, cursor = repo.list_after("codex", after=0)
    assert [row["id"] for row in page] == [message.id] and cursor == repo.delivery_head()
    closed()
    assert repo.claim(message.id, "codex").status == "claimed"
    closed()
    assert repo.acknowledge(message.id, "codex").status == "acknowledged"
    closed()
    other = repo.insert(sender="hermes", recipient="codex", message_type="request", body={})
    repo.claim(other.id, "codex")
    assert repo.reject(other.id, "codex", "synthetic reason").status == "rejected"
    closed()
    assert repo.expire_claims() == 0
    closed()
    assert repo.cleanup() == {"acknowledged_deleted": 0, "rejected_deleted": 0}
    closed()


def test_internal_transaction_context_commits_and_rolls_back_before_close(tmp_path, tracked_connections):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    with repo._connection() as conn:
        conn.execute("CREATE TABLE cleanup_test (value TEXT)")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO cleanup_test VALUES ('committed')")
    assert conn.closed
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with repo._connection() as conn:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO cleanup_test VALUES ('rolled back')")
            raise RuntimeError("synthetic failure")
    assert conn.closed
    with repo._connection() as conn:
        assert [row[0] for row in conn.execute("SELECT value FROM cleanup_test")] == ["committed"]
    assert conn.closed and all(conn.closed for conn in tracked_connections)


def test_failed_operations_close_handles_and_preserve_state(tmp_path, tracked_connections):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    message = repo.insert(sender="hermes", recipient="codex", message_type="request", body={})
    operations = [
        (NotFound, lambda: repo.get("missing")),
        (NotFound, lambda: repo.get_send_receipt("hermes", "missing")),
        (NotFound, lambda: repo.sequence_for("missing")),
        (MessageError, lambda: repo.list_after("codex", after=repo.delivery_head() + 1)),
        (InvalidTransition, lambda: repo.claim(message.id, "claude")),
        (InvalidTransition, lambda: repo.acknowledge(message.id, "codex")),
        (MessageError, lambda: repo.insert(sender="hermes", recipient="codex", message_type="request",
                                          body={}, in_reply_to="missing")),
    ]
    for error, operation in operations:
        with pytest.raises(error):
            operation()
        assert all(conn.closed for conn in tracked_connections)
    assert repo.get(message.id).status == "pending"
    assert repo.delivery_head() == 1


def test_failed_initialization_closes_owned_handle(tmp_path, monkeypatch, tracked_connections):
    def fail_migration(*args):
        raise RuntimeError("synthetic migration failure")
    monkeypatch.setattr(MessageRepository, "_migrate_messages", fail_migration)
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        MessageRepository(tmp_path / "messages.sqlite3")
    assert len(tracked_connections) == 1 and tracked_connections[0].closed


@pytest.mark.parametrize("repository_class", [MessageRepository, KnowledgeRepository])
def test_connect_setup_failure_closes_unreturned_handle(tmp_path, monkeypatch, repository_class):
    raw_connect = sqlite3.connect
    opened = []

    class FailingSetup(sqlite3.Connection):
        closed = False
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA foreign_keys = ON":
                raise sqlite3.OperationalError("synthetic setup failure")
            return super().execute(sql, *args, **kwargs)
        def close(self):
            try:
                return super().close()
            finally:
                self.closed = True

    def connect(*args, **kwargs):
        conn = raw_connect(*args, **kwargs, factory=FailingSetup)
        opened.append(conn)
        return conn
    monkeypatch.setattr(database.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="synthetic setup failure"):
        repository_class(tmp_path / "messages.sqlite3")
    assert len(opened) == 1 and opened[0].closed


def test_public_connect_and_borrowed_get_remain_caller_owned(tmp_path, tracked_connections):
    repo = MessageRepository(tmp_path / "messages.sqlite3")
    message = repo.insert(sender="hermes", recipient="codex", message_type="request", body={})
    conn = repo.connect()
    try:
        with conn:
            assert repo.get(message.id, conn=conn) == message
            with pytest.raises(NotFound):
                repo.get("missing", conn=conn)
        assert not conn.closed  # Public SQLite transaction context does not transfer ownership.
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()
    assert all(conn.closed for conn in tracked_connections)


def test_initialization_and_internal_operations_emit_no_unclosed_database_warning(tmp_path):
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always", ResourceWarning)
        repo = MessageRepository(tmp_path / "messages.sqlite3")
        message = repo.insert(sender="hermes", recipient="codex", message_type="request", body={})
        repo.list_for_recipient("codex")
        repo.get(message.id)
        del repo
        gc.collect()
    assert not [warning for warning in observed
                if issubclass(warning.category, ResourceWarning)
                and "unclosed database" in str(warning.message)]


@pytest.mark.asyncio
async def test_health_probe_closes_its_caller_owned_connection(tmp_path, tracked_connections):
    pytest.importorskip("fcntl", reason="Real bus app requires Linux FileRepository")
    import httpx
    from agent_message_bus.app import create_app
    from agent_message_bus.auth import AuthSettings
    app = create_app(db_path=tmp_path / "health.sqlite3",
                     audit_path=tmp_path / "audit.jsonl", file_root=tmp_path / "files",
                     auth=AuthSettings("test-hermes", "test-codex", claude_token="test-claude"),
                     push_events_operational=False, startup_cleanup=False)
    before = len(tracked_connections)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1") as client:
        response = await client.get("/health")
    assert response.status_code == 200 and response.json()["ok"] is True
    assert len(tracked_connections) > before
    assert all(conn.closed for conn in tracked_connections)


def test_knowledge_methods_close_handles_and_preserve_cas(tmp_path, tracked_connections):
    repo = KnowledgeRepository(tmp_path / "knowledge.sqlite3")
    assert tracked_connections and all(conn.closed for conn in tracked_connections)
    original = repo.put(project="cleanup", path="notes.md", title="Note", content="synthetic content",
                        author="test", expected_version=0)
    assert all(conn.closed for conn in tracked_connections)
    assert repo.get("cleanup", "notes.md") == original
    assert repo.list(project="cleanup") == [original]
    assert repo.search(query="synthetic", project="cleanup") == [original]
    assert all(conn.closed for conn in tracked_connections)
    with pytest.raises(KnowledgeConflict):
        repo.put(project="cleanup", path="notes.md", title="Wrong", content="wrong content",
                 author="test", expected_version=0)
    assert all(conn.closed for conn in tracked_connections)
    assert repo.get("cleanup", "notes.md") == original
    with pytest.raises(KnowledgeNotFound):
        repo.get("cleanup", "missing.md")
    assert all(conn.closed for conn in tracked_connections)
    updated = repo.put(project="cleanup", path="notes.md", title="Updated", content="second content",
                       author="test", expected_version=1)
    assert updated.version == 2 and repo.get("cleanup", "notes.md") == updated
    assert all(conn.closed for conn in tracked_connections)
    conn = repo.connect()
    try:
        with conn:
            assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == 1
        assert not conn.closed  # Public raw-connect behavior is unchanged.
    finally:
        conn.close()


@pytest.mark.parametrize("repository_kind", ["knowledge", "semantic"])
def test_knowledge_or_semantic_initialization_failure_closes_handle(
        tmp_path, monkeypatch, repository_kind):
    if repository_kind == "semantic":
        pytest.importorskip("fcntl", reason="Real SemanticIndex requires Linux fcntl")
        from agent_message_bus.semantic import SemanticIndex
    raw_connect = sqlite3.connect
    opened = []
    failed_table = "knowledge" if repository_kind == "knowledge" else "documents"

    class FailingDDL(sqlite3.Connection):
        closed = False
        def execute(self, sql, *args, **kwargs):
            if sql.strip().startswith("CREATE TABLE IF NOT EXISTS " + failed_table):
                raise sqlite3.OperationalError("synthetic initialization failure")
            return super().execute(sql, *args, **kwargs)
        def close(self):
            try:
                return super().close()
            finally:
                self.closed = True
    def connect(*args, **kwargs):
        conn = raw_connect(*args, **kwargs, factory=FailingDDL)
        opened.append(conn)
        return conn
    monkeypatch.setattr(database.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="synthetic initialization failure"):
        if repository_kind == "knowledge":
            KnowledgeRepository(tmp_path / "knowledge.sqlite3")
        else:
            SemanticIndex(tmp_path / "source.sqlite3", tmp_path / "files",
                          cache_root=tmp_path / "index", embedder=FakeEmbedder())
    assert len(opened) == 1 and opened[0].closed


@pytest.fixture
def semantic_class():
    pytest.importorskip("fcntl", reason="SemanticIndex's real FileRepository requires Linux fcntl")
    from agent_message_bus.semantic import SemanticIndex
    return SemanticIndex


class FakeEmbedder:
    model_name = "connection-cleanup-test"
    model_version = "test"

    def embed(self, texts):
        for _text in texts:
            yield [1.0, 0.0, 0.0]


def test_semantic_index_init_status_sync_search_close_all_handles(
        tmp_path, tracked_connections, semantic_class):
    source = tmp_path / "source.sqlite3"
    knowledge = KnowledgeRepository(source)
    knowledge.put(project="cleanup", path="note.md", title="Note", content="synthetic content",
                  author="test", expected_version=0)
    index = semantic_class(source, tmp_path / "files", cache_root=tmp_path / "index",
                           embedder=FakeEmbedder())
    assert all(conn.closed for conn in tracked_connections)
    assert index.status()["state"] == "available"
    assert all(conn.closed for conn in tracked_connections)
    assert index.synchronize()["documents"] == 1
    assert all(conn.closed for conn in tracked_connections)
    assert index.search("synthetic")[0]["path"] == "note.md"
    assert all(conn.closed for conn in tracked_connections)
    # Repeated search takes the unchanged-document/index refresh paths.
    assert index.search("synthetic")[0]["version"] == 1
    assert all(conn.closed for conn in tracked_connections)
    knowledge.put(project="cleanup", path="note.md", title="New", content="different content",
                  author="test", expected_version=1)
    assert index.search("different")[0]["version"] == 2
    assert all(conn.closed for conn in tracked_connections)
    with closing(knowledge.connect()) as conn, conn:
        conn.execute("DELETE FROM knowledge")
    assert index.synchronize()["deleted"] == 1
    assert index.search("synthetic") == []
    assert all(conn.closed for conn in tracked_connections)


@pytest.mark.parametrize("factory", ["_connect_index", "_connect_source"])
def test_semantic_factory_setup_failure_closes_handle(
        tmp_path, monkeypatch, semantic_class, factory):
    raw_connect = sqlite3.connect
    index = semantic_class(tmp_path / "source.sqlite3", tmp_path / "files",
                           cache_root=tmp_path / "index", embedder=FakeEmbedder())
    opened = []

    class FailingSetup(sqlite3.Connection):
        closed = False
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("PRAGMA"):
                raise sqlite3.OperationalError("synthetic setup failure")
            return super().execute(sql, *args, **kwargs)
        def close(self):
            try:
                return super().close()
            finally:
                self.closed = True
    def connect(*args, **kwargs):
        conn = raw_connect(*args, **kwargs, factory=FailingSetup)
        opened.append(conn)
        return conn
    monkeypatch.setattr(database.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="synthetic setup failure"):
        getattr(index, factory)()
    assert len(opened) == 1 and opened[0].closed


def test_semantic_index_write_failure_rolls_back_and_closes_handle(
        tmp_path, monkeypatch, semantic_class):
    from agent_message_bus.semantic import SemanticIndexError
    source = tmp_path / "source.sqlite3"
    knowledge = KnowledgeRepository(source)
    knowledge.put(project="cleanup", path="note.md", title="Note", content="synthetic content",
                  author="test", expected_version=0)
    index = semantic_class(source, tmp_path / "files", cache_root=tmp_path / "index",
                           embedder=FakeEmbedder())
    raw_connect = sqlite3.connect
    opened = []
    class FailChunkInsert(sqlite3.Connection):
        closed = False
        def executemany(self, sql, *args, **kwargs):
            if sql.startswith("INSERT INTO chunks"):
                raise sqlite3.OperationalError("synthetic chunk write failure")
            return super().executemany(sql, *args, **kwargs)
        def close(self):
            try:
                return super().close()
            finally:
                self.closed = True
    def index_connection():
        conn = raw_connect(index.index_path, timeout=15, isolation_level=None, factory=FailChunkInsert)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        opened.append(conn)
        return conn
    monkeypatch.setattr(index, "_connect_index", index_connection)
    with pytest.raises(SemanticIndexError):
        index.synchronize()
    assert opened and all(conn.closed for conn in opened)
    with closing(raw_connect(index.index_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
