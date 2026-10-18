from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
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


def _initialize_v060_stores(db_path: Path, files_root: Path, extraction_root: Path) -> None:
    """Initialize a synthetic DB through the actual v0.6.0 store code/resources."""
    source_repo = Path(__file__).resolve().parents[2] / "internal-MCP-messaging"
    archived = subprocess.run(
        ["git", "archive", "--format=tar", "v0.6.0", "src/agent_message_bus"],
        cwd=source_repo, capture_output=True, check=True, timeout=30,
    )
    extraction_root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as source_archive:
        source_archive.extractall(extraction_root, filter="data")
    package_root = extraction_root / "src" / "agent_message_bus"
    package_name = "legacy_bus_v060_" + hashlib.sha256(str(extraction_root).encode()).hexdigest()[:12]
    package_spec = importlib.util.spec_from_file_location(
        package_name, package_root / "__init__.py", submodule_search_locations=[str(package_root)],
    )
    assert package_spec is not None and package_spec.loader is not None
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)

    def load_module(name: str):
        module_spec = importlib.util.spec_from_file_location(
            f"{package_name}.{name}", package_root / f"{name}.py",
        )
        assert module_spec is not None and module_spec.loader is not None
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        module_spec.loader.exec_module(module)
        return module

    load_module("database").MessageRepository(db_path)
    load_module("files").FileRepository(db_path, files_root)
    load_module("feedback").FeedbackRepository(db_path)
    connection = sqlite3.connect(db_path)
    try:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert checkpoint is not None and checkpoint[0] == 0
    finally:
        connection.close()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def _build_bundle(bundle: Path) -> dict[str, str]:
    """Build a synthetic archive using the actual v0.6.0 store initialization."""

    import sqlite3

    bundle.mkdir()
    (bundle / "data").mkdir()
    files_root = bundle / "files"
    (files_root / ".agent-message-bus" / "objects").mkdir(parents=True)
    db_path = bundle / "data" / "messages.sqlite3"
    _initialize_v060_stores(db_path, files_root, bundle.parent / "legacy-v060-source")
    conn = sqlite3.connect(db_path)
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
    notes: list[dict[str, object]] = []
    for number in range(1, 66):
        source_path = f"Shared/note-{number:03}.md"
        path = f"snapshots/20261014T010743Z/{source_path}"
        content = f"Synthetic VaultBridge note {number}; {marker}"
        digest = _sha256(content.encode())
        notes.append({
            "source_path": source_path,
            "sha256": digest,
            "modified": now,
            "destination_paths": [path],
        })
        conn.execute(
            "INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("vaultbridge", path, f"Synthetic note {number}", content, 1, digest,
             "synthetic", "synthetic", now, now),
        )
    vb_manifest = json.dumps({
        "source": "VaultBridge",
        "source_url": "http://192.168.50.80:30491",
        "destination_project": "vaultbridge",
        "snapshot": "20261014T010743Z",
        "source_note_count": 65,
        "verified_note_count": 65,
        "source_preserved": True,
        "old_inbox_messages_replayed": False,
        "notes": notes,
    }, separators=(",", ":"))
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


def _readonly_subprocess_env(
    tmp_path: Path, bundle: Path, *, writable_relative: str | None = None,
) -> dict[str, str]:
    """Test shim only: simulate mount flags; it is not kernel-mount evidence."""
    patch_dir = tmp_path / "python-startup"
    patch_dir.mkdir(exist_ok=True)
    realpath = str(bundle.absolute())
    (patch_dir / "sitecustomize.py").write_text(
        "import os\nfrom types import SimpleNamespace\n"
        "_real_fstatvfs = os.fstatvfs\n"
        f"_bundle = {realpath!r}\n"
        f"_writable_relative = {writable_relative!r}\n"
        "_readonly = getattr(os, 'ST_RDONLY', 1)\n"
        "def _fstatvfs(fd):\n"
        "    result = _real_fstatvfs(fd)\n"
        "    try:\n"
        "        path = os.path.abspath(os.readlink(f'/proc/self/fd/{fd}'))\n"
        "        relative = os.path.relpath(path, _bundle)\n"
        "    except OSError:\n"
        "        return result\n"
        "    if path == _bundle or path.startswith(_bundle + os.sep):\n"
        "        flags = result.f_flag | _readonly\n"
        "        if relative == _writable_relative:\n"
        "            flags &= ~_readonly\n"
        "        return SimpleNamespace(f_flag=flags)\n"
        "    return result\n"
        "os.fstatvfs = _fstatvfs\n",
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
    assert "topic_memberships" not in report["database"]["table_counts"]
    assert report["database"]["optional_tables_archived"] == []
    assert report["database"]["schema_primary_keys"]["messages"] == ["id"]
    assert set(report["database"]["logical_digests"]) == set(report["database"]["table_counts"])
    assert all(len(digest) == 64 for digest in report["database"]["logical_digests"].values())
    assert report["files"]["current_rows_verified"] == 2
    assert report["files"]["revision_rows_verified"] == 2
    assert report["audit"]["records_verified"] == 2
    assert report["vaultbridge"]["source_notes_verified"] == 65
    assert report["vaultbridge"]["destination_rows_verified"] == 65
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


def test_nested_writable_source_mount_is_refused_when_bundle_root_is_readonly(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True,
        env=_readonly_subprocess_env(tmp_path, bundle, writable_relative="audit.jsonl"), timeout=60,
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
    connection.close()
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["vaultbridge"]["source_notes_verified"] == 65
    assert report["vaultbridge"]["destination_rows_verified"] == 65
    assert report["database"]["table_counts"]["knowledge"] == 67


def test_vaultbridge_manifest_requires_source_path_provenance(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    database = bundle / "data" / "messages.sqlite3"
    with sqlite3.connect(database) as connection:
        manifest_path = "snapshots/20261014T010743Z/MANIFEST.json"
        contents = connection.execute(
            "SELECT content FROM knowledge WHERE project='vaultbridge' AND path=?", (manifest_path,),
        ).fetchone()[0]
        manifest = json.loads(contents)
        manifest["notes"][0]["destination_paths"] = [
            "snapshots/20261014T010743Z/Shared/not-the-source.md"
        ]
        updated = json.dumps(manifest, separators=(",", ":"))
        connection.execute(
            "UPDATE knowledge SET content=?,sha256=? WHERE project='vaultbridge' AND path=?",
            (updated, _sha256(updated.encode()), manifest_path),
        )
    connection.close()
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "validation failed: vaultbridge_archive_invalid"
    assert not report_path.exists()



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
    connection.close()
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


def test_optional_topic_memberships_are_digested_as_archive_metadata(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    with sqlite3.connect(bundle / "data" / "messages.sqlite3") as connection:
        connection.execute(
            """CREATE TABLE topic_memberships (
                topic TEXT NOT NULL, agent TEXT NOT NULL, session_hash TEXT NOT NULL,
                subscription_generation TEXT NOT NULL, stream_generation TEXT NOT NULL,
                generation TEXT NOT NULL, registered_at TEXT NOT NULL,
                PRIMARY KEY (topic, agent)
            )"""
        )
        connection.execute(
            "INSERT INTO topic_memberships VALUES(?,?,?,?,?,?,?)",
            ("synthetic", "hermes", "a" * 64, "subscription", "stream", "generation", "2026-10-14T01:07:43Z"),
        )
    connection.close()
    _refresh_inventory(bundle)
    report_path = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--bundle", str(bundle), "--report", str(report_path)],
        capture_output=True, text=True, env=_readonly_subprocess_env(tmp_path, bundle), timeout=60,
    )

    assert result.returncode == 0, result.stderr
    database = json.loads(report_path.read_text(encoding="utf-8"))["database"]
    assert database["optional_tables_archived"] == ["topic_memberships"]
    assert database["table_counts"]["topic_memberships"] == 1
    assert len(database["logical_digests"]["topic_memberships"]) == 64



def test_database_rejects_extra_table_with_sqlite_like_prefix(tmp_path: Path) -> None:
    bundle = tmp_path / "synthetic-bundle"
    _build_bundle(bundle)
    import sqlite3

    with sqlite3.connect(bundle / "data" / "messages.sqlite3") as connection:
        connection.execute("CREATE TABLE sqliteXextra(secret TEXT)")
        connection.execute("INSERT INTO sqliteXextra VALUES('synthetic hidden table')")
    connection.close()
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
            connection.execute("UPDATE knowledge SET content=? WHERE path='snapshots/20261014T010743Z/Shared/note-001.md'", (sentinels["marker"],))
        connection.close()
    elif mutation == "object":
        object_path = bundle / "files" / ".agent-message-bus" / "objects" / ("c" * 64)
        object_path.write_bytes(b"tampered-object")
    else:
        with sqlite3.connect(bundle / "data" / "messages.sqlite3") as connection:
            connection.execute(
                "INSERT INTO feedback_history VALUES(?,?,?,?,?,?)",
                ("missing-parent", 1, "hermes", "open", sentinels["marker"], "2026-10-14T01:07:43Z"),
            )
        connection.close()
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
