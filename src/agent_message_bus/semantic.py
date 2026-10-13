from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import re
import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from .files import FileError, FileRepository
from .knowledge import KnowledgeError, KnowledgeRepository

MODEL_NAME = "BAAI/bge-small-en-v1.5"
MODEL_CACHE_IMAGE_PATH = Path("/opt/fastembed-cache")
MAX_QUERY_CHARS = 200
MAX_QUERY_TERMS = 20
MAX_RESULTS = 100
MAX_KNOWLEDGE_BYTES = 1024 * 1024
MAX_FILE_READ_BYTES = 1024 * 1024
MAX_INDEX_CHARS = 160_000
MAX_CHUNKS_PER_DOCUMENT = 128
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 120
PAGE_SIZE = 256
SNIPPET_CHARS = 320
_UNSET = object()

_SUPPORTED_TEXT_SUFFIXES = {
    ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs", ".css", ".csv", ".go",
    ".h", ".hpp", ".html", ".ini", ".java", ".js", ".json", ".jsx",
    ".kt", ".md", ".mjs", ".php", ".py", ".rb", ".rs", ".rst", ".sh",
    ".sql", ".svg", ".swift", ".tex", ".toml", ".ts", ".tsx",
    ".txt", ".xml", ".yaml", ".yml", ".zsh",
}

_BEARER_SECRET = re.compile(r"(?i)(\bbearer\s+)[^\s,;\"']+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Z0-9_.-])((?P<quote>[\"'])?[A-Z0-9_.-]*"
    r"(?:TOKEN|API[_-]?KEY|SECRET|PASSWORD|CREDENTIAL|PRIVATE[_-]?KEY)"
    r"[A-Z0-9_.-]*(?P=quote)\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}\]#]+)"
)
_ENV_ASSIGNMENT = re.compile(
    r"(?i)(\b[A-Z_][A-Z0-9_]{2,}\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_PROVIDER_KEY = re.compile(
    r"(?i)\b(?:AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|sk-(?:proj-)?[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{20,})\b"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_INDEX_SYNC_LOCKS_GUARD = threading.Lock()
_INDEX_SYNC_LOCKS: dict[str, Any] = {}


def _shared_index_sync_lock(index_path: Path) -> threading.RLock:
    key = str(index_path.resolve())
    with _INDEX_SYNC_LOCKS_GUARD:
        return _INDEX_SYNC_LOCKS.setdefault(key, threading.RLock())


class SemanticError(ValueError):
    """Base error for semantic search validation and runtime failures."""


class SemanticValidationError(SemanticError):
    pass


class SemanticUnavailableError(SemanticError):
    pass


class SemanticIndexError(SemanticError):
    pass


@dataclass(frozen=True)
class _Document:
    source: str
    project: str
    path: str
    title: str
    digest: str
    fingerprint: str
    version: int | str | None
    text: str
    truncated: bool
    content_indexed: bool
    metadata_only: bool


class SemanticIndex:
    """Local sentence-embedding search over knowledge and stored-file snapshots.

    The rebuildable SQLite index and writable model cache live below ``cache_root``;
    by default that is ``<db_path.parent>/search``. Container images include the
    ONNX model at ``/opt/fastembed-cache`` so query-time loading is offline.
    ``embedder`` is an injection point for deterministic unit tests.
    """

    def __init__(self, db_path: str | Path, file_root: str | Path,
                 cache_root: str | Path | None = None, *,
                 model_cache_root: str | Path | None = None,
                 model_name: str = MODEL_NAME, embedder: Any | None = None,
                 enabled: bool = True):
        self.db_path = Path(db_path).expanduser()
        self.file_root = Path(file_root).expanduser()
        self.cache_root = Path(cache_root).expanduser() if cache_root is not None else self.db_path.parent / "search"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_root / "index.sqlite3"
        self._index_sync_lock = _shared_index_sync_lock(self.index_path)
        if model_cache_root is not None:
            self.model_cache_root = Path(model_cache_root).expanduser()
        elif _has_model_snapshot(MODEL_CACHE_IMAGE_PATH, model_name):
            self.model_cache_root = MODEL_CACHE_IMAGE_PATH
        else:
            self.model_cache_root = self.cache_root / "model-cache"
        self.model_name = model_name
        self.enabled = bool(enabled)
        self._embedder = embedder
        self._lock = threading.RLock()
        self._last_error: str | None = None
        self._last_sync_error: str | None = None
        self._model_hash: str | None = None
        self._model_version: str | None = None
        self._model_dim: int | None = None
        self._model_manifest: tuple[list[tuple[Path, str]], str] | None = None
        self._stored_model_hash_cache: str | None | object = _UNSET
        self._files: FileRepository | None = None
        self._initialize_index()
        self._files = FileRepository(self.db_path, self.file_root)

    def _initialize_index(self) -> None:
        with self._connect_index() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS documents (
                    source TEXT NOT NULL,
                    project TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    version_json TEXT,
                    truncated INTEGER NOT NULL,
                    content_indexed INTEGER NOT NULL,
                    metadata_only INTEGER NOT NULL,
                    PRIMARY KEY (source, project, path)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS chunks (
                    source TEXT NOT NULL,
                    project TEXT NOT NULL,
                    path TEXT NOT NULL,
                    chunk_no INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    vector BLOB NOT NULL,
                    dimensions INTEGER NOT NULL,
                    PRIMARY KEY (source, project, path, chunk_no),
                    FOREIGN KEY(source, project, path)
                      REFERENCES documents(source, project, path) ON DELETE CASCADE
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_semantic_documents_project ON documents(project, source)")
            conn.execute("CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def _connect_index(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.index_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _connect_source(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _probe_model(self) -> tuple[str | None, str | None, list[tuple[Path, str]]]:
        if self._embedder is not None:
            if self._model_hash is None:
                name = str(getattr(self._embedder, "model_name", "injected-embedder"))
                self._model_hash = hashlib.sha256(name.encode("utf-8")).hexdigest()
                self._model_version = str(getattr(self._embedder, "model_version", "injected"))
            return self._model_hash, self._model_version, []
        if self._model_manifest is not None:
            return self._model_manifest[1], self._model_version, self._model_manifest[0]
        try:
            files = _model_snapshot_files(self.model_cache_root, self.model_name)
        except OSError:
            self._last_error = "model_cache_unreadable"
            return None, _fastembed_version(), []
        if not files:
            return None, _fastembed_version(), []
        manifest = hashlib.sha256()
        file_hashes: list[tuple[Path, str]] = []
        try:
            for path in files:
                digest = _sha256_file(path)
                file_hashes.append((path, digest))
                try:
                    name = path.relative_to(self.model_cache_root).as_posix()
                except ValueError:
                    name = path.name
                manifest.update(name.encode("utf-8"))
                manifest.update(b"\0")
                manifest.update(digest.encode("ascii"))
                manifest.update(b"\n")
        except OSError:
            self._last_error = "model_cache_unreadable"
            return None, _fastembed_version(), []
        self._model_manifest = (file_hashes, manifest.hexdigest())
        self._model_hash = manifest.hexdigest()
        self._model_version = _fastembed_version()
        if self._last_error in {"model_cache_unreadable", "model_not_found", "dependency_missing"}:
            self._last_error = None
        return self._model_hash, self._model_version, file_hashes

    def _ensure_embedder(self) -> Any:
        if not self.enabled:
            raise SemanticUnavailableError("semantic search is disabled")
        if self._embedder is not None:
            return self._embedder
        model_hash, model_version, files = self._probe_model()
        if model_hash is None or not files:
            error = self._last_error or "model_not_found"
            self._last_error = error
            raise SemanticUnavailableError(
                f"semantic model {self.model_name} is unavailable locally ({error}); "
                "install the model before enabling semantic search"
            )
        if model_version is None:
            self._last_error = "dependency_missing"
            raise SemanticUnavailableError("fastembed is not installed")
        try:
            from fastembed import TextEmbedding

            self._embedder = TextEmbedding(
                model_name=self.model_name,
                cache_dir=str(self.model_cache_root),
                providers=["CPUExecutionProvider"],
                threads=2,
                local_files_only=True,
            )
            self._model_hash = model_hash
            self._model_version = model_version
            self._last_error = None
            return self._embedder
        except Exception as exc:
            self._last_error = "model_load_failed"
            raise SemanticUnavailableError(
                f"semantic model could not be loaded locally ({type(exc).__name__})"
            ) from exc

    def status(self) -> dict[str, Any]:
        """Return safe model/index metadata without loading or downloading the model."""
        with self._lock:
            model_version = self._model_version or _fastembed_version()
            state, error, ready = "error", "model_not_found", False
            if not self.enabled:
                state, error, ready = "disabled", None, False
            elif self._last_sync_error is not None:
                state, error, ready = "error", self._last_sync_error, False
            elif self._embedder is not None:
                state, error, ready = "available", None, True
            else:
                try:
                    files = _model_snapshot_files(self.model_cache_root, self.model_name)
                except OSError:
                    files = []
                    error = "model_cache_unreadable"
                if files and self._last_error in {"model_cache_unreadable", "model_not_found", "dependency_missing"}:
                    self._last_error = None
                if self._last_error is not None:
                    error = self._last_error
                elif files:
                    if model_version is None:
                        error = "dependency_missing"
                    else:
                        state, error, ready = "available", None, True
            try:
                with self._connect_index() as conn:
                    documents = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
                    chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
                    metadata = {row["key"]: row["value"] for row in conn.execute(
                        "SELECT key,value FROM index_meta WHERE key IN ('model_hash','model_version','model_dimension')"
                    )}
            except sqlite3.Error:
                documents, chunks = 0, 0
                metadata = {}
                if state != "disabled":
                    state, error, ready = "error", "index_unavailable", False
            try:
                dimension = int(metadata["model_dimension"]) if "model_dimension" in metadata else self._model_dim
            except (TypeError, ValueError):
                dimension = None
            return {
                "state": state,
                "ready": ready,
                "enabled": self.enabled,
                "model": self.model_name,
                "model_version": model_version,
                "model_hash": self._model_hash,
                "indexed_model_version": metadata.get("model_version"),
                "indexed_model_hash": metadata.get("model_hash"),
                "embedding_dimension": dimension,
                "index_path": str(self.index_path),
                "indexed_documents": documents,
                "indexed_chunks": chunks,
                "error": error,
            }

    def search(self, query: str, project: str | None = None, limit: int = 20,
               source: str | None = None) -> list[dict[str, Any]]:
        query = _validate_query(query)
        if project is not None:
            try:
                KnowledgeRepository.validate_project(project)
            except KnowledgeError as exc:
                raise SemanticValidationError(str(exc)) from exc
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
            raise SemanticValidationError(f"limit must be an integer from 1 to {MAX_RESULTS}")
        if source is not None and (not isinstance(source, str) or source not in {"knowledge", "file"}):
            raise SemanticValidationError("source must be 'knowledge', 'file', or None")
        query = _scrub_secrets(query).strip()
        if not query:
            raise SemanticValidationError("query contains no searchable text after secret redaction")
        if len(query) > MAX_QUERY_CHARS or len(query.split()) > MAX_QUERY_TERMS:
            raise SemanticValidationError(
                f"query must be at most {MAX_QUERY_CHARS} characters and {MAX_QUERY_TERMS} terms"
            )

        with self._lock:
            embedder = self._ensure_embedder()
            with self._index_sync_lock:
                self._synchronize(embedder)
            query_vector = _normalize_vector(_embed(embedder, [query])[0])
            if self._model_dim is not None and self._model_dim != len(query_vector):
                raise SemanticIndexError("query and document embedding dimensions do not match")
            self._model_dim = len(query_vector)
            with self._connect_index() as conn:
                conn.execute("INSERT INTO index_meta(key,value) VALUES('model_dimension',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(self._model_dim),))
            results: dict[tuple[str, str, str], tuple[float, sqlite3.Row, str]] = {}
            with self._connect_index() as conn:
                cursor = conn.execute(
                    """SELECT c.source,c.project,c.path,c.text,c.vector,c.dimensions,
                              d.title,d.digest,d.version_json,d.truncated,d.content_indexed,d.metadata_only
                       FROM chunks c JOIN documents d
                         ON d.source=c.source AND d.project=c.project AND d.path=c.path
                       WHERE (? IS NULL OR d.project=?) AND (? IS NULL OR c.source=?)""",
                    (project, project, source, source),
                )
                while rows := cursor.fetchmany(256):
                    for row in rows:
                        stored = _unpack_vector(row["vector"], int(row["dimensions"]))
                        score = _cosine(query_vector, stored)
                        key = (row["source"], row["project"], row["path"])
                        previous = results.get(key)
                        if previous is None or score > previous[0]:
                            results[key] = (score, row, row["text"])
            ranked = sorted(results.values(), key=lambda item: (-item[0], item[1]["source"],
                                                                  item[1]["project"], item[1]["path"]))[:limit]
            return [_result_dict(score, row, snippet) for score, row, snippet in ranked]

    def synchronize(self) -> dict[str, int]:
        """Incrementally refresh indexed sources and reconcile deleted documents."""
        with self._lock:
            embedder = self._ensure_embedder()
            with self._index_sync_lock:
                return self._synchronize(embedder)

    def _synchronize(self, embedder: Any) -> dict[str, int]:
        model_hash, model_version, _ = self._probe_model()
        if model_hash is None:
            model_hash = hashlib.sha256(self.model_name.encode("utf-8")).hexdigest()
        self._stored_model_hash_cache = _UNSET
        self._stored_model_hash()
        self._last_sync_error = None
        seen: set[tuple[str, str, str]] = set()
        updated = 0
        try:
            for document in self._knowledge_documents():
                key = (document.source, document.project, document.path)
                seen.add(key)
                if self._index_document(document, embedder, model_hash):
                    updated += 1
            for metadata in self._file_metadata_pages():
                document = self._file_document(metadata)
                key = (document.source, document.project, document.path)
                seen.add(key)
                if self._index_document(document, embedder, model_hash):
                    updated += 1
            with self._connect_index() as conn:
                existing = conn.execute("SELECT source,project,path FROM documents").fetchall()
                stale = [(row["source"], row["project"], row["path"])
                         for row in existing if (row["source"], row["project"], row["path"]) not in seen]
                if stale:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        conn.executemany("DELETE FROM documents WHERE source=? AND project=? AND path=?", stale)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
            with self._connect_index() as conn:
                indexed_docs = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
                indexed_chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
                conn.execute("BEGIN IMMEDIATE")
                for key, value in (("model_name", self.model_name),
                                   ("model_version", model_version or "unknown"),
                                   ("model_hash", model_hash)):
                    conn.execute("INSERT INTO index_meta(key,value) VALUES(?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
                if self._model_dim is not None:
                    conn.execute("INSERT INTO index_meta(key,value) VALUES('model_dimension',?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (str(self._model_dim),))
                conn.commit()
            self._stored_model_hash_cache = model_hash
            return {"updated": updated, "deleted": len(stale), "documents": indexed_docs,
                    "chunks": indexed_chunks}
        except Exception as exc:
            self._last_sync_error = "index_sync_failed"
            if isinstance(exc, (SemanticError,)):
                raise
            raise SemanticIndexError(f"semantic index synchronization failed ({type(exc).__name__})") from exc

    def _knowledge_documents(self) -> Iterator[_Document]:
        cursor: tuple[str, str] | None = None
        while True:
            with self._connect_source() as conn:
                try:
                    if cursor is None:
                        rows = conn.execute(
                            "SELECT project,path,title,version,sha256 FROM knowledge "
                            "ORDER BY project,path LIMIT ?", (PAGE_SIZE,)
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT project,path,title,version,sha256 FROM knowledge "
                            "WHERE project>? OR (project=? AND path>?) ORDER BY project,path LIMIT ?",
                            (cursor[0], cursor[0], cursor[1], PAGE_SIZE),
                        ).fetchall()
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc).lower():
                        return
                    raise
            if not rows:
                return
            for row in rows:
                cursor = (row["project"], row["path"])
                title = str(row["title"])
                path = str(row["path"])
                fingerprint = _fingerprint("knowledge", row["sha256"], title, path)
                if not self._needs_refresh("knowledge", str(row["project"]), path, fingerprint):
                    with self._connect_index() as conn:
                        indexed = conn.execute(
                            "SELECT truncated FROM documents WHERE source='knowledge' AND project=? AND path=?",
                            (row["project"], path),
                        ).fetchone()
                    yield _Document("knowledge", str(row["project"]), path, _scrub_secrets(title),
                                    str(row["sha256"]), fingerprint, int(row["version"]), "",
                                    bool(indexed["truncated"]) if indexed else False, True, False)
                    continue
                with self._connect_source() as conn:
                    content_row = conn.execute(
                        "SELECT content FROM knowledge WHERE project=? AND path=? AND version=? AND sha256=?",
                        (row["project"], path, row["version"], row["sha256"]),
                    ).fetchone()
                if content_row is None:
                    continue
                raw_bytes = str(content_row["content"]).encode("utf-8")
                if hashlib.sha256(raw_bytes).hexdigest() != row["sha256"]:
                    self._last_sync_error = "source_integrity_error"
                    continue
                limited = raw_bytes[:MAX_KNOWLEDGE_BYTES].decode("utf-8", errors="ignore")
                truncated = len(raw_bytes) > MAX_KNOWLEDGE_BYTES
                safe_text = _scrub_secrets(f"{title}\n{path}\n{limited}")
                safe_text, char_truncated = _limit_text(safe_text)
                truncated = truncated or char_truncated
                chunks = _chunk_text(safe_text)
                if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
                    chunks = chunks[:MAX_CHUNKS_PER_DOCUMENT]
                    truncated = True
                yield _Document("knowledge", str(row["project"]), path, _scrub_secrets(title), str(row["sha256"]),
                                fingerprint, int(row["version"]), "\n".join(chunks), truncated,
                                True, False)

    def _file_metadata_pages(self) -> Iterator[Any]:
        files = self._files
        if files is None:
            return
        cursor: str | None = None
        while True:
            rows = files.list(prefix=None, after=cursor, limit=PAGE_SIZE)
            if not rows:
                return
            for metadata in rows:
                cursor = metadata.path
                yield metadata
            if len(rows) < PAGE_SIZE:
                return

    def _file_document(self, metadata: Any) -> _Document:
        storage_path = str(metadata.path)
        project, path = _project_file_path(storage_path)
        title = path.rsplit("/", 1)[-1]
        content_type = str(metadata.content_type or "application/octet-stream")
        size = int(metadata.size)
        digest = str(metadata.sha256)
        suffix = Path(title).suffix.casefold()
        supports_text = suffix in _SUPPORTED_TEXT_SUFFIXES or content_type.casefold().startswith("text/")
        version: int | str | None = getattr(metadata, "version", None)
        if supports_text:
            safe_title = _scrub_secrets(title)
            fingerprint = _fingerprint("file", digest, safe_title, storage_path, content_type, size)
            if not self._needs_refresh("file", project, path, fingerprint):
                flags = self._indexed_file_flags(project, path)
                if flags is not None:
                    truncated, content_indexed, metadata_only = flags
                    return _Document("file", project, path, safe_title, digest, fingerprint, version,
                                     "", truncated, content_indexed, metadata_only)
            try:
                data, actual_metadata = self._read_file_bytes(storage_path, MAX_FILE_READ_BYTES, version)
                if actual_metadata is not None:
                    metadata = actual_metadata
                    size = int(metadata.size)
                    digest = str(metadata.sha256)
                    content_type = str(metadata.content_type or content_type)
                    version = getattr(metadata, "version", version)
                    fingerprint = _fingerprint("file", digest, safe_title, storage_path, content_type, size)
                text = data.decode("utf-8-sig")
                if len(data) >= size and hashlib.sha256(data).hexdigest() != digest:
                    raise SemanticIndexError("stored file digest does not match metadata")
                truncated = size > len(data)
                safe_text = _scrub_secrets(f"{safe_title}\n{path}\n{text}")
                safe_text, char_truncated = _limit_text(safe_text)
                truncated = truncated or char_truncated
                chunks = _chunk_text(safe_text)
                if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
                    chunks = chunks[:MAX_CHUNKS_PER_DOCUMENT]
                    truncated = True
                return _Document("file", project, path, safe_title, digest, fingerprint, version,
                                 "\n".join(chunks), truncated, True, False)
            except (UnicodeDecodeError, OSError, FileError, SemanticIndexError):
                # Keep searchable metadata for invalid encodings, unavailable blobs, and
                # concurrent writes; file_get/read_bytes remains the authority.
                metadata_text = _metadata_text(safe_title, path, content_type, size)
                return _Document("file", project, path, safe_title, digest, fingerprint, version,
                                 metadata_text, size > MAX_FILE_READ_BYTES, False, True)
        safe_title = _scrub_secrets(title)
        fingerprint = _fingerprint("file", digest, safe_title, storage_path, content_type, size)
        return _Document("file", project, path, safe_title, digest, fingerprint, version,
                         _metadata_text(safe_title, path, content_type, size), False, False, True)

    def _read_file_bytes(self, path: str, max_bytes: int,
                         version: int | str | None) -> tuple[bytes, Any | None]:
        if self._files is None:
            raise FileError("file service is not available")
        read_version = version if isinstance(version, int) and not isinstance(version, bool) else None
        metadata, data = self._files.read_bytes(path, max_bytes=max_bytes, version=read_version)
        return bytes(data), metadata

    def _needs_refresh(self, source: str, project: str, path: str,
                       fingerprint: str) -> bool:
        with self._connect_index() as conn:
            row = conn.execute(
                "SELECT fingerprint FROM documents WHERE source=? AND project=? AND path=?",
                (source, project, path),
            ).fetchone()
        return row is None or row["fingerprint"] != fingerprint or self._stored_model_hash() != self._model_hash

    def _indexed_file_flags(self, project: str, path: str) -> tuple[bool, bool, bool] | None:
        with self._connect_index() as conn:
            row = conn.execute(
                "SELECT truncated,content_indexed,metadata_only FROM documents "
                "WHERE source='file' AND project=? AND path=?",
                (project, path),
            ).fetchone()
        if row is None:
            return None
        return bool(row["truncated"]), bool(row["content_indexed"]), bool(row["metadata_only"])

    def _stored_model_hash(self) -> str | None:
        if self._stored_model_hash_cache is not _UNSET:
            return self._stored_model_hash_cache  # type: ignore[return-value]
        with self._connect_index() as conn:
            row = conn.execute("SELECT value FROM index_meta WHERE key='model_hash'").fetchone()
        self._stored_model_hash_cache = str(row["value"]) if row is not None else None
        return self._stored_model_hash_cache

    def _index_document(self, document: _Document, embedder: Any, model_hash: str) -> bool:
        if not document.text and document.content_indexed:
            # An unchanged source document may only carry metadata fields. Preserve its
            # current vectors and refresh authoritative version/title/digest fields.
            with self._connect_index() as conn:
                conn.execute(
                    "UPDATE documents SET title=?,digest=?,version_json=?,truncated=?,content_indexed=?,metadata_only=? "
                    "WHERE source=? AND project=? AND path=?",
                    (document.title, document.digest, _json(document.version), int(document.truncated),
                     int(document.content_indexed), int(document.metadata_only), document.source,
                     document.project, document.path),
                )
            return False
        with self._connect_index() as conn:
            current = conn.execute(
                "SELECT fingerprint FROM documents WHERE source=? AND project=? AND path=?",
                (document.source, document.project, document.path),
            ).fetchone()
        if current is not None and current["fingerprint"] == document.fingerprint \
                and self._stored_model_hash() == model_hash:
            with self._connect_index() as conn:
                conn.execute(
                    "UPDATE documents SET title=?,digest=?,version_json=?,truncated=?,content_indexed=?,metadata_only=? "
                    "WHERE source=? AND project=? AND path=?",
                    (document.title, document.digest, _json(document.version), int(document.truncated),
                     int(document.content_indexed), int(document.metadata_only), document.source,
                     document.project, document.path),
                )
            return False
        parts = _chunk_text(document.text)
        if len(parts) > MAX_CHUNKS_PER_DOCUMENT:
            parts = parts[:MAX_CHUNKS_PER_DOCUMENT]
        vectors = [_normalize_vector(vector) for vector in _embed(embedder, parts)] if parts else []
        if len(vectors) != len(parts):
            raise SemanticIndexError("embedding model returned an unexpected vector count")
        if vectors:
            dimension = len(vectors[0])
            if any(len(vector) != dimension for vector in vectors):
                raise SemanticIndexError("embedding model returned inconsistent vector dimensions")
            if self._model_dim is not None and self._model_dim != dimension:
                raise SemanticIndexError("embedding model changed vector dimensions")
            self._model_dim = dimension
        with self._connect_index() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM documents WHERE source=? AND project=? AND path=?",
                             (document.source, document.project, document.path))
                conn.execute(
                    """INSERT INTO documents
                       (source,project,path,title,digest,fingerprint,version_json,truncated,content_indexed,metadata_only)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (document.source, document.project, document.path, document.title, document.digest,
                     document.fingerprint, _json(document.version), int(document.truncated),
                     int(document.content_indexed), int(document.metadata_only)),
                )
                conn.executemany(
                    "INSERT INTO chunks(source,project,path,chunk_no,text,vector,dimensions) VALUES(?,?,?,?,?,?,?)",
                    [(document.source, document.project, document.path, index, part,
                      _pack_vector(vector), len(vector)) for index, (part, vector) in enumerate(zip(parts, vectors))],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True


def _validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise SemanticValidationError(f"query must be 1-{MAX_QUERY_CHARS} non-whitespace characters")
    if len(query.strip().split()) > MAX_QUERY_TERMS:
        raise SemanticValidationError(f"query must contain at most {MAX_QUERY_TERMS} terms")
    return query.strip()


def _scrub_secrets(text: str) -> str:
    text = _PRIVATE_KEY_BLOCK.sub("[redacted private key]", text)
    text = _BEARER_SECRET.sub(r"\1[redacted]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _ENV_ASSIGNMENT.sub(r"\1[redacted]", text)
    return _PROVIDER_KEY.sub("[redacted key]", text)


def _limit_text(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_INDEX_CHARS:
        return text, False
    return text[:MAX_INDEX_CHARS], True


def _chunk_text(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):
            boundary = text.rfind(" ", start + CHUNK_CHARS // 2, end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)
    return chunks


def _fingerprint(source: str, digest: str, *metadata: Any) -> str:
    payload = json.dumps([source, digest, *metadata], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metadata_text(title: str, path: str, content_type: str, size: int) -> str:
    return _scrub_secrets(f"{title}\n{path}\nFile metadata: {content_type}; {size} bytes; content not indexed.")


def _project_file_path(storage_path: str) -> tuple[str, str]:
    project, separator, relative_path = storage_path.partition("/")
    if not separator:
        return "", storage_path
    try:
        project = KnowledgeRepository.validate_project(project)
    except KnowledgeError:
        return "", storage_path
    return project, relative_path


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _embed(embedder: Any, texts: Sequence[str]) -> list[Sequence[float]]:
    try:
        return list(embedder.embed(list(texts)))
    except Exception as exc:
        raise SemanticIndexError(f"embedding failed ({type(exc).__name__})") from exc


def _normalize_vector(vector: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    if not values or any(not math.isfinite(value) for value in values):
        raise SemanticIndexError("embedding model returned an invalid vector")
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude <= 0:
        raise SemanticIndexError("embedding model returned a zero vector")
    return tuple(value / magnitude for value in values)


def _pack_vector(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(raw: bytes, dimensions: int) -> tuple[float, ...]:
    if dimensions <= 0 or len(raw) != dimensions * 4:
        raise SemanticIndexError("stored embedding vector has invalid dimensions")
    return struct.unpack(f"<{dimensions}f", raw)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise SemanticIndexError("query and document embedding dimensions do not match")
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right))))


def _result_dict(score: float, row: sqlite3.Row, snippet: str) -> dict[str, Any]:
    snippet = snippet[:SNIPPET_CHARS]
    if len(snippet) == SNIPPET_CHARS:
        snippet += "…"
    return {
        "score": round(score, 6),
        "source": str(row["source"]),
        "project": str(row["project"]) or None,
        "path": str(row["path"]),
        "title": str(row["title"]),
        "snippet": snippet,
        "digest": str(row["digest"]),
        "version": json.loads(row["version_json"]) if row["version_json"] is not None else None,
        "truncated": bool(row["truncated"]),
        "content_indexed": bool(row["content_indexed"]),
        "metadata_only": bool(row["metadata_only"]),
    }


def _fastembed_version() -> str | None:
    try:
        return importlib.metadata.version("fastembed")
    except importlib.metadata.PackageNotFoundError:
        return None


def _model_snapshot_files(cache_root: Path, model_name: str) -> list[Path]:
    if not cache_root.is_dir():
        return []
    model_file = "model_optimized.onnx" if model_name == MODEL_NAME else "model.onnx"
    try:
        snapshots = sorted(path for path in cache_root.rglob(model_file) if path.is_file())
    except OSError:
        return []
    if not snapshots:
        # Support fastembed's legacy pre-Hugging-Face directory structure.
        legacy = cache_root / f"fast-{model_name.rsplit('/', 1)[-1]}" / model_file
        if legacy.is_file():
            snapshots = [legacy]
    if not snapshots:
        return []
    model_path = snapshots[0]
    root = model_path.parent
    files = sorted(path for path in root.iterdir() if path.is_file())
    names = {path.name for path in files}
    if not {model_file, "tokenizer.json", "config.json"}.issubset(names):
        return []
    return files


def _has_model_snapshot(cache_root: Path, model_name: str) -> bool:
    return bool(_model_snapshot_files(cache_root, model_name))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
