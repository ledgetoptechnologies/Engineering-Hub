# Offline legacy MCP archive validator (v1)

This helper validates a pre-built, coherent legacy `agent-message-bus` bundle. It does **not** copy, import, restore, initialize, migrate, clean up, or connect to any bus. It has no network client or credential-loading path. The only write is a new JSON metadata report created with exclusive-create semantics and mode `0600`; its parent directory must already exist.

## Preconditions and use

Mount the already-checkpointed bundle read-only, then run:

```sh
python scripts/hub_legacy_archive.py validate \
  --bundle /mnt/legacy-mcp-archive \
  --report /var/tmp/legacy-mcp-validation.json
```

The validator requires the kernel `statvfs(...).f_flag & ST_RDONLY` bit on the bundle mount. A `frozen=true` marker or an operator assertion is not accepted. It rejects source symlink path components, symlinks, hard-linked regular files, special files, unlisted files, and SQLite `-wal`, `-shm`, or `-journal` sidecars. SQLite is opened only as `mode=ro&immutable=1`; a checkpointed, consistent backup is an operator precondition, not something this tool can create or prove. The report separately shows the SQLite `PRAGMA user_version`; that value is not compared with the source schema version (legacy file repository initialization can set it to `2` even when the bus schema version is `4`).

The report path must be new, not a regular file or even a dangling symlink, and outside the bundle. The validator never creates parent directories. On failure it emits only a bounded error code; it does not print SQL values, manifest contents, audit records, file bytes, knowledge content, or tracebacks.

## Frozen manifest format

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

The validator checks the v0.6.0/schema-4 core table columns and ordered primary-key columns, SQLite integrity and foreign keys, row counts, and deterministic SHA-256 multiset digests over canonical typed SQL rows. It verifies the full JSONL audit hash chain. Knowledge content hashes are recomputed. Current-file and revision metadata are checked against bytes: non-null `storage_key` is an opaque 64-hex object name below `files_root/.agent-message-bus/objects/`; it need not equal the content SHA-256. A legacy `files.storage_key IS NULL` row resolves to `files_root/<path>`.

The mandatory VaultBridge record is `knowledge(project='vaultbridge', path='snapshots/20261014T010743Z/MANIFEST.json')`. Its strict JSON shape is `{ "count": 65, "entries": [{"destination_path": "...", "source_sha256": "..."}] }`. The validator requires 65 unique listed destinations and verifies each listed source digest against the corresponding actual `vaultbridge` knowledge row. Other knowledge rows are still included in the database digests and are not removed or reported as content.

The report contains only bundle/manifest metadata, captured source metadata explicitly marked unverified, schema/PK metadata, SQL table counts and logical digests, file/revision verification counts, audit count/head, VaultBridge count, the actual SQLite `user_version`, and `archive_inert: true`. It is an offline integrity result—not a statement that a live service was identified, authorized, migrated, or changed.

## Tests

`tests/test_hub_legacy_archive.py` creates a synthetic database, synthetic content, and synthetic 65-entry VaultBridge manifest. The CLI subprocess tests inject a `sitecustomize` shim that reports the temporary synthetic bundle mount as read-only; those tests verify validator behavior but are **not** evidence of a real read-only mount or any live/NAS data. No tests read credentials or contact a service.
