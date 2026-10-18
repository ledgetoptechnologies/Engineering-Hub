# Offline legacy MCP archive validator (v1)

Integration provenance: adapted from Hermes's reviewed
`bc496b0b37d7074828e885d589bab6a67e40ef70`. This development integration adds
bounded streaming directory enumeration and bounded SQLite schema enumeration;
the runtime script is therefore not byte-identical to that original checkpoint.
It also isolates historical test resources from the evolving current schema.
Independent review accepted these changes. Scoped Linux verification passed
30 tests with ResourceWarning errors enabled in an offline, read-only-root
validation container (synthetic source/mount flags, not actual archived data).
No live archive bundle, data copy or retrieval API/UI is established by this.

This helper validates a pre-built, coherent legacy `agent-message-bus` bundle. It does **not** copy, import, restore, initialize, migrate, clean up, or connect to any bus. It has no network client or credential-loading path. The only write is a new JSON metadata report created with exclusive-create semantics and mode `0600`; its parent directory must already exist.

## Preconditions and use

Mount the already-checkpointed bundle read-only, then run:

```sh
python scripts/hub_legacy_archive.py validate \
  --bundle /mnt/legacy-mcp-archive \
  --report /var/tmp/legacy-mcp-validation.json
```

The validator checks `os.fstatvfs(fd).f_flag & ST_RDONLY` on the opened bundle root and each opened source directory and regular file. It does not rely on a path-based check of only the top-level directory. A `frozen=true` marker or operator assertion is not accepted. It rejects source symlink path components, symlinks, hard-linked regular files, special files, unlisted files, and SQLite `-wal`, `-shm`, or `-journal` sidecars. SQLite is opened only as `mode=ro&immutable=1`; a checkpointed, consistent backup is an operator precondition, not something this tool can create or prove. The report separately shows SQLite `PRAGMA user_version`; that value is not compared with the bus schema version (legacy file repository initialization can set it to `2` when the bus schema version is `4`).

The report path must be new, not a regular file or even a dangling symlink, and outside the bundle. Directory inode checks also reject a writable bind alias into the archive, and the created report inode is checked again after writing. Use a private, trusted report parent: these checks do not protect against a privileged concurrent operator changing mount topology or replacing a path after validation returns. The validator never creates parent directories. On failure it emits only a bounded error code; it does not print SQL values, manifest contents, audit records, file bytes, knowledge content, or tracebacks.

## Frozen bundle manifest

The root `archive-manifest.json` is strict UTF-8 JSON with duplicate keys and non-standard numeric constants rejected. Its closed top-level keys are:

```json
{
  "schema": "hub_legacy_archive_v1",
  "source": {
    "service": "agent-message-bus",
    "version": "0.6.0",
    "schema_version": 4,
    "image_reference": "ghcr.io/<owner>/internal-mcp:<tag-or-digest>",
    "revision": "<40 lowercase hexadecimal characters>"
  },
  "database": "data/messages.sqlite3",
  "audit": "audit.jsonl",
  "files_root": "files",
  "inventory": [
    {"path": "<bundle-relative-file>", "size": 123, "sha256": "<64 lowercase hexadecimal characters>"}
  ]
}
```

The `source` values are captured metadata only. They are **not** proof of running image identity, source authorization, or provenance. The manifest itself is excluded from its own inventory; every other regular file, including hidden object-store bytes, must appear once with its exact byte size and SHA-256. Empty directories need not be listed. Paths are safe relative POSIX paths, never absolute or containing `..`. No annotations, environment values, service configuration, or secrets belong in the manifest. Operators retain private runtime configuration separately.

The manifest/file scan is bounded at 1 MiB for the manifest, 8 GiB per file, 128 GiB total bytes, 200,000 inventory entries, 1,000,000 SQL rows/audit records, 1 MiB per audit record, and one hour per validation. Exceeding a bound rejects the bundle.

## Semantic checks and report scope

The validator requires the v0.6.0/schema-4 core database tables and their exact columns and ordered primary keys, then checks SQLite integrity and foreign keys, row counts, and deterministic SHA-256 multiset digests over canonical typed SQL rows. The core reflects the v0.6.0 schema plus the file repository's migrations (`file_revisions`, file metadata columns, and the SQLite sequence table). The later `topic_memberships` table is optional: if present, its schema and rows are included in the counts and logical digests and `optional_tables_archived`; it is not required for schema 4 and never proves current transport presence. Unrecognized tables fail closed rather than being silently dropped.

The validator verifies the full JSONL audit hash chain. Knowledge content hashes are recomputed. Current-file and revision metadata are checked against bytes: non-null `storage_key` is an opaque 64-hex object name below `files_root/.agent-message-bus/objects/`; it need not equal the content SHA-256. A legacy `files.storage_key IS NULL` row resolves to `files_root/<path>`.

The mandatory VaultBridge record is `knowledge(project='vaultbridge', path='snapshots/20261014T010743Z/MANIFEST.json')`. It uses the archived snapshot format: top-level `source`, `source_url`, `destination_project`, `snapshot`, `source_note_count`, `verified_note_count`, `source_preserved`, `old_inbox_messages_replayed`, and `notes`. The validator requires the recorded 65-note snapshot metadata, 65 unique safe `source_path` values, valid ISO `modified` values, and one unique `destination_paths` entry per note. Each target must be the corresponding `snapshots/20261014T010743Z/<source_path>` path, match the archived knowledge row's SHA-256, and account for every knowledge row under that snapshot prefix. The manifest itself and unrelated knowledge rows are retained and hashed; other knowledge rows are not treated as part of the dated snapshot.

The report contains only bundle/manifest metadata, captured source metadata explicitly marked unverified, schema/PK metadata, SQL table counts and logical digests, optional archived table names, file/revision verification counts, audit count/head, VaultBridge verification counts, the actual SQLite `user_version`, and `archive_inert: true`. It is an offline integrity result—not a statement that a live service was identified, authorized, migrated, or changed. Archived membership metadata is not live-presence evidence.

Payload queries stream one row at a time instead of materializing batches of large values. Delivery checks reject retained messages without sequence mappings, orphan/nonpositive mappings, and a SQLite delivery high-water below any retained mapping or send-idempotency tombstone.

## Tests

`tests/test_hub_legacy_archive.py` creates synthetic content and rows, but builds the positive database through the actual v0.6.0 `MessageRepository`, `FileRepository`, and `FeedbackRepository` initialization. The six source/resources are frozen under `tests/fixtures/legacy_agent_bus_v060/` and hash-pinned to verified `v0.6.0` bytes, independently of the evolving current package; no sibling checkout, Git tag, or network is needed. It checkpoints the synthetic SQLite database and creates the actual-shape synthetic 65-note VaultBridge manifest. CLI tests use a `sitecustomize` shim to simulate read-only and nested-writable mount flags through file descriptors. The shim is a unit-test boundary only, **not** evidence of a real kernel read-only mount or live/NAS data. No tests read credentials or contact a service.
