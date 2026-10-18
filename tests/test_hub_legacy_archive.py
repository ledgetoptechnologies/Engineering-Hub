from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "hub_legacy_archive.py"
REVISION = "a" * 40


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _audit_record(previous: str, marker: str) -> dict[str, object]:
    record: dict[str, object] = {
        "timestamp": "2026-10-14T01:07:43Z",
        "operation": "synthetic",
        "agent": "hermes",
        "message_id": marker,
        "recipient": "codex",
        "result": "ok",
        "correlation_id": None,
        "request_id": marker,
        "prev_hash": previous,
    }
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["hash"] = _sha256(payload)
    return record


def _build_bundle(bundle: Path) -> dict[str, str]:
    """Create an entirely synthetic v1 bundle with all legacy core tables."""
    import sqlite3

    bundle.mkdir()
    (bundle / "data").mkdir()
    files_root = bundle / "files"
    (files_root / ".agent-message-bus" / "objects").mkdir(parents=True)
    db_path = bundle / "data" / "messages.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE messages (
          id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
          message_type TEXT NOT NULL, priority TEXT NOT NULL, body_json TEXT NOT NULL,
          status TEXT NOT NULL, correlation_id TEXT, in_reply_to TEXT,
          hop_count INTEGER NOT NULL, requires_user_approval INTEGER NOT NULL,
          created_at TEXT NOT NULL, claimed_by TEXT, claim_expires_at TEXT,
          acknowledged_at TEXT, rejected_at TEXT, rejection_reason TEXT
        );
        CREATE TABLE message_delivery_sequence (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE
        );
        CREATE TABLE send_idempotency (
          sender TEXT NOT NULL, client_request_id TEXT NOT NULL, message_id TEXT NOT NULL,
          recipient TEXT NOT NULL, created_at TEXT NOT NULL, seq INTEGER NOT NULL,
          fingerprint_version INTEGER NOT NULL, fingerprint TEXT NOT NULL,
          PRIMARY KEY (sender, client_request_id)
        );
        CREATE TABLE files (
          path TEXT PRIMARY KEY, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
          content_type TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          version INTEGER NOT NULL, created_by TEXT, updated_by TEXT, storage_key TEXT
        );
        CREATE TABLE file_revisions (
          path TEXT NOT NULL, version INTEGER NOT NULL, size INTEGER NOT NULL,
          sha256 TEXT NOT NULL, content_type TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, created_by TEXT, updated_by TEXT,
          storage_key TEXT NOT NULL, PRIMARY KEY (path, version)
        );
        CREATE TABLE knowledge (
          project TEXT NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL,
          content TEXT NOT NULL, version INTEGER NOT NULL, sha256 TEXT NOT NULL,
          created_by TEXT NOT NULL, updated_by TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, PRIMARY KEY (project, path)
        );
        CREATE TABLE topic_memberships (
          topic TEXT NOT NULL, agent TEXT NOT NULL, session_hash TEXT NOT NULL,
          subscription_generation TEXT NOT NULL, stream_generation TEXT NOT NULL,
          generation TEXT NOT NULL, registered_at TEXT NOT NULL, PRIMARY KEY(topic, agent)
        );
        CREATE TABLE feedback_entries (
          id TEXT PRIMARY KEY, author TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
          description TEXT NOT NULL, project TEXT NOT NULL, severity TEXT NOT NULL,
          status TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, client_request_id TEXT, payload_json TEXT, payload_hash TEXT,
          UNIQUE(author, client_request_id)
        );
        CREATE TABLE feedback_history (
          feedback_id TEXT NOT NULL REFERENCES feedback_entries(id), revision INTEGER NOT NULL,
          actor TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(feedback_id, revision)
        );
        PRAGMA user_version=2;
        """
    )
    marker = "SYNTHETIC-SECRET-MARKER-DO-NOT-REPORT"
    message_id = "synthetic-message-id"
    conn.execute(
        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (message_id, "hermes", "codex", "task", "normal", json.dumps({"note": marker}),
         "pending", None, None, 0, 0, "2026-10-14T01:07:43Z", None, None, None, None, None),
    )
    conn.execute("INSERT INTO message_delivery_sequence(message_id) VALUES(?)", (message_id,))
    conn.execute(
        "INSERT INTO send_idempotency VALUES(?,?,?,?,?,?,?,?)",
        ("hermes", "synthetic-request", message_id, "codex", "2026-10-14T01:07:43Z",
         1, 1, "b" * 64),
    )

    object_records: list[tuple[str, str, bytes]] = []
    for key, contents in (("a" * 64, b"revision-one"), ("c" * 64, b"current-object")):
        object_records.append((key, _sha256(contents), contents))
        (files_root / ".agent-message-bus" / "objects" / key).write_bytes(contents)
    flat = b"legacy-flat-file"
    (files_root / "legacy").mkdir()
    (files_root / "legacy" / "flat.txt").write_bytes(flat)
    now = "2026-10-14T01:07:43Z"
    conn.execute(
        "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("docs/current.bin", len(b"current-object"), _sha256(b"current-object"),
         "application/octet-stream", now, now, 2, "hermes", "hermes", "c" * 64),
    )
    conn.execute(
        "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("legacy/flat.txt", len(flat), _sha256(flat), "text/plain", now, now, 1,
         "hermes", "hermes", None),
    )
    conn.execute(
        "INSERT INTO file_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("docs/current.bin", 1, len(b"revision-one"), _sha256(b"revision-one"),
         "application/octet-stream", now, now, "hermes", "hermes", "a" * 64),
    )
    conn.execute(
        "INSERT INTO file_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("legacy/flat.txt", 1, len(b"revision-one"), _sha256(b"revision-one"),
         "text/plain", now, now, "hermes", "hermes", "a" * 64),
    )
    notes: list[dict[str, str]] = []
    for number in range(1, 66):
        path = f"notes/note-{number:03}.md"
        content = f"Synthetic VaultBridge note {number}; {marker}"
        digest = _sha256(content.encode())
        notes.append({"destination_path": path, "source_sha256": digest})
        conn.execute(
            "INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("vaultbridge", path, f"Synthetic note {number}", content, 1, digest,
             "synthetic", "synthetic", now, now),
        )
    vb_manifest = json.dumps({"count": 65, "entries": notes}, separators=(",", ":"))
    vb_digest = _sha256(vb_manifest.encode())
    conn.execute(
        "INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("vaultbridge", "snapshots/20261014T010743Z/MANIFEST.json", "Synthetic manifest",
         vb_manifest, 1, vb_digest, "synthetic", "synthetic", now, now),
    )
    conn.commit()
    conn.close()

    records = [_audit_record("", marker), _audit_record("0" * 64, "second-synthetic-entry")]
    records[1] = _audit_record(str(records[0]["hash"]), "second-synthetic-entry")
    (bundle / "audit.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )

    inventory = []
    for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
        relative = path.relative_to(bundle).as_posix()
        contents = path.read_bytes()
        inventory.append({"path": relative, "size": len(contents), "sha256": _sha256(contents)})
    manifest = {
        "schema": "hub_legacy_archive_v1",
        "source": {
            "service": "agent-message-bus", "version": "0.6.0", "schema_version": 4,
            "image_reference": "ghcr.io/ledger-top/internal-mcp:0.6.0", "revision": REVISION,
        },
        "database": "data/messages.sqlite3", "audit": "audit.jsonl", "files_root": "files",
        "inventory": inventory,
    }
    (bundle / "archive-manifest.json").write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    return {"marker": marker, "manifest_sha256": _sha256((bundle / "archive-manifest.json").read_bytes())}


def _readonly_subprocess_env(tmp_path: Path, bundle: Path) -> dict[str, str]:
    patch_dir = tmp_path / "python-startup"
    patch_dir.mkdir(exist_ok=True)
    realpath = str(bundle.absolute())
    (patch_dir / "sitecustomize.py").write_text(
        "import os\nfrom types import SimpleNamespace\n"
        "_real_statvfs = os.statvfs\n"
        f"_bundle = {realpath!r}\n"
        "def _statvfs(path):\n"
        "    result = _real_statvfs(path)\n"
        "    if os.path.abspath(os.fspath(path)) == _bundle:\n"
        "        return SimpleNamespace(f_flag=result.f_flag | getattr(os, 'ST_RDONLY', 1))\n"
        "    return result\n"
        "os.statvfs = _statvfs\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(patch_dir) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _refresh_inventory(bundle: Path) -> None:
    manifest_path = bundle / "archive-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inventory"] = []
    for path in sorted(p for p in bundle.rglob("*") if p.is_file() and p != manifest_path):
        data = path.read_bytes()
        manifest["inventory"].append({
            "path": path.relative_to(bundle).as_posix(), "size": len(data), "sha256": _sha256(data),
        })
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")


def test_cli_validates_synthetic_v1_bundle_without_emitting_payloads(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    sentinels = _build_bundle(bundle)
    source_before = {
        path.relative_to(bundle).as_posix(): path.read_bytes()
        for path in bundle.rglob("*") if path.is_file()
    }
    report_path = tmp_path / "reports" / "validation.json"
    report_path.parent.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 0, f"validator failed with metadata-only output: {result.stderr}"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["archive_inert"] is True
    assert report["manifest_sha256"] == sentinels["manifest_sha256"]
    assert report["source_captured_unverified"]["schema_version"] == 4
    assert report["database"]["pragma_user_version"] == 2
    assert report["database"]["table_counts"]["knowledge"] == 66
    assert report["database"]["schema_primary_keys"]["messages"] == ["id"]
    assert set(report["database"]["logical_digests"]) == set(report["database"]["table_counts"])
    assert all(len(digest) == 64 for digest in report["database"]["logical_digests"].values())
    assert report["files"]["current_rows_verified"] == 2
    assert report["files"]["revision_rows_verified"] == 2
    assert report["audit"]["records_verified"] == 2
    assert report["vaultbridge"]["listed_entries_verified"] == 65
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    source_after = {
        path.relative_to(bundle).as_posix(): path.read_bytes()
        for path in bundle.rglob("*") if path.is_file()
    }
    assert source_after == source_before
    serialized = report_path.read_text(encoding="utf-8") + result.stdout + result.stderr
    assert sentinels["marker"] not in serialized
    assert "synthetic-message-id" not in serialized


def test_null_inventory_is_rejected_with_bounded_manifest_error(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    manifest_path = bundle / "archive-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inventory"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: manifest_inventory_invalid"
    assert not report_path.exists()


def test_writable_mount_is_refused_before_report_creation(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=os.environ.copy(), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: readonly_mount_required"
    assert not report_path.exists()


def test_malformed_audit_secret_is_not_reported(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    sentinels = _build_bundle(bundle)
    secret = "SYNTHETIC-MALFORMED-AUDIT-SECRET"
    (bundle / "audit.jsonl").write_text(secret + "\n", encoding="utf-8")
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert secret not in result.stdout + result.stderr
    assert sentinels["marker"] not in result.stdout + result.stderr
    assert not report_path.exists()


def test_hardlinked_source_file_is_rejected(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    os.link(bundle / "files" / "legacy" / "flat.txt", bundle / "extra-hardlink")
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: hardlinked_file"
    assert not report_path.exists()


def test_database_reader_is_immutable_query_only_and_untrusted_schema_off(tmp_path: Path) -> None:
    database = tmp_path / "readonly.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES('synthetic')")
    spec = importlib.util.spec_from_file_location("hub_legacy_archive", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    connection = module._connect_immutable(str(database), time.monotonic() + 5)
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden(value TEXT)")
    finally:
        connection.close()


def test_vaultbridge_manifest_checks_listed_rows_without_dropping_unrelated_knowledge(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    import sqlite3

    unrelated = "Synthetic VaultBridge row outside the dated snapshot"
    with sqlite3.connect(bundle / "data" / "messages.sqlite3") as connection:
        connection.execute(
            "INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("vaultbridge", "unrelated.md", "Unrelated synthetic row", unrelated, 1,
             _sha256(unrelated.encode()), "synthetic", "synthetic",
             "2026-10-14T01:07:43Z", "2026-10-14T01:07:43Z"),
        )
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["vaultbridge"]["listed_entries_verified"] == 65
    assert report["database"]["table_counts"]["knowledge"] == 67


def test_large_synthetic_knowledge_table_verifies_across_batches(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    import sqlite3

    timestamp = "2026-10-14T01:07:43Z"
    with sqlite3.connect(bundle / "data" / "messages.sqlite3") as connection:
        for number in range(300):
            content = f"Synthetic unrelated project note {number}"
            connection.execute(
                "INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("synthetic", f"notes/{number:03}.md", f"Synthetic note {number}", content, 1,
                 _sha256(content.encode()), "synthetic", "synthetic", timestamp, timestamp),
            )
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["database"]["table_counts"]["knowledge"] == 366


@pytest.mark.parametrize("existing_kind", ["regular", "dangling_symlink"])
def test_report_destination_must_be_new_and_never_followed(
    tmp_path: Path, existing_kind: str,
) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    report_path = tmp_path / "validation.json"
    link_target = tmp_path / "must-not-be-created.json"
    if existing_kind == "regular":
        report_path.write_text("preserve-existing-report", encoding="utf-8")
    else:
        report_path.symlink_to(link_target)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: report_already_exists"
    if existing_kind == "regular":
        assert report_path.read_text(encoding="utf-8") == "preserve-existing-report"
    else:
        assert report_path.is_symlink()
        assert not link_target.exists()


def test_input_symlink_is_rejected(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    (bundle / "linked-file").symlink_to(bundle / "audit.jsonl")
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: symlink_entry"
    assert not report_path.exists()


def test_unregistered_file_is_rejected_even_when_hidden_or_flat(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    (bundle / ".unregistered-synthetic-object").write_bytes(b"unregistered")
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: inventory_mismatch"
    assert not report_path.exists()


def test_manifest_rejects_duplicate_json_keys_without_echoing_manifest(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    manifest_path = bundle / "archive-manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(manifest_text[:-1] + ',"schema":"SYNTHETIC-MANIFEST-SECRET"}', encoding="utf-8")
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: duplicate_json_key"
    assert "SYNTHETIC-MANIFEST-SECRET" not in result.stdout + result.stderr
    assert not report_path.exists()


def test_sqlite_wal_sidecar_is_rejected_even_when_inventoried(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    (bundle / "data" / "messages.sqlite3-wal").write_bytes(b"synthetic uncheckpointed sidecar")
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: manifest_inventory_invalid"
    assert not report_path.exists()


def test_database_rejects_extra_table_with_sqlite_like_prefix(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    import sqlite3

    with sqlite3.connect(bundle / "data" / "messages.sqlite3") as connection:
        connection.execute("CREATE TABLE sqliteXextra(secret TEXT)")
        connection.execute("INSERT INTO sqliteXextra VALUES('synthetic hidden table')")
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: database_schema_invalid"
    assert not report_path.exists()


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [("audit", "audit_chain_invalid"), ("knowledge", "knowledge_content_mismatch"),
     ("object", "file_content_mismatch"), ("foreign_key", "sqlite_foreign_key_failed")],
)
def test_semantic_integrity_failures_are_metadata_only(
    tmp_path: Path, mutation: str, expected_error: str,
) -> None:
    bundle = tmp_path / "synthetic-bundle"
    sentinels = _build_bundle(bundle)
    import sqlite3

    if mutation == "audit":
        audit_path = bundle / "audit.jsonl"
        records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        records[0]["result"] = sentinels["marker"]
        audit_path.write_text("".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records))
    elif mutation == "knowledge":
        with sqlite3.connect(bundle / "data" / "messages.sqlite3") as connection:
            connection.execute("UPDATE knowledge SET content=? WHERE path='notes/note-001.md'", (sentinels["marker"],))
    elif mutation == "object":
        object_path = bundle / "files" / ".agent-message-bus" / "objects" / ("c" * 64)
        object_path.write_bytes(b"tampered-object")
    else:
        with sqlite3.connect(bundle / "data" / "messages.sqlite3") as connection:
            connection.execute(
                "INSERT INTO feedback_history VALUES(?,?,?,?,?,?)",
                ("missing-parent", 1, "hermes", "open", sentinels["marker"], "2026-10-14T01:07:43Z"),
            )
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == f"validation failed: {expected_error}"
    assert sentinels["marker"] not in result.stdout + result.stderr
    assert not report_path.exists()
