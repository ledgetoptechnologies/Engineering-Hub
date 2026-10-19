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

Clean-archive follow-up: Windows `core.autocrlf` also changes hash-pinned bytes
when exporting an archive unless attributes pin LF. The six current source
resources still match v0.6.0 in Git; the observed hash mismatch was line-ending
conversion, not semantic schema drift. `.gitattributes` now preserves original
LF for the frozen fixture Python/SQL files. A clean archive at `9149352` passed
all 30 scoped Linux tests in 3.18s (ResourceWarning errors), image index
`sha256:4460f23a036b8902e434d98609db23cb3b1cb05ecf4998c209910ae72e96bb39`.
Frozen fixture isolation additionally prevents future current-schema drift.

## Actual kernel read-only smoke (synthetic data only)

`docker/Dockerfile.hub-archive-acceptance` derives a disposable synthetic bundle
from a verified local validation image. It creates fixtures only during build;
runtime remains UID/GID 10001. An omitted base selects `scratch` and cannot run
the fixture builder; the operator must supply a reviewed validation base.
BuildKit resolves local tags: verify the actual selected digest in the build
output, not merely the tag spelling. No production image is published by this.

Root verified base index
`sha256:4460f23a036b8902e434d98609db23cb3b1cb05ecf4998c209910ae72e96bb39`
and resulting fixture index
`sha256:7d314907358d44fb93447bb21d111dfa3502f4aed910e77899334021c5ffc1cc`.
Actual Linux Docker runs used no network, no host bind mount/socket/credentials,
all capabilities dropped, no-new-privileges, one CPU, 512 MiB memory and 64 PIDs.
Only a private 16 MiB `/tmp` tmpfs received the metadata report.

- Read-only root: validator succeeded; real `statvfs` reported read-only, report
  mode was `0600`, owner UID 10001, and all 65 synthetic VaultBridge notes verified.
- Writable root: validator rejected with `readonly_mount_required`.
- Read-only root with a writable anonymous tmpfs at `/archive-fixture/files`:
  validator rejected with `readonly_mount_required`.

The synthetic archive manifest SHA-256 was
`210a001cf83d6d258f2d339ec1535881b9b4c80d8332e56d6ab8d6edebbb6756`.
All runs terminated and their disposable containers were removed with `--rm`.
This proves the kernel mount checks on fixtures, not coherent export, original
data parity, NAS deployment, UI retrieval or migration acceptance. The legacy
live bus remains untouched.

## Source-only historical message reader

`engineering_hub.archive_messages.ArchiveMessageReader` is explicit private
composition, not an installed endpoint. The caller supplies an absolute Linux
database path, independently approved original SHA-256 from the complete offline
validation, a byte cap and a bounded cooperative deadline. Computing a new hash
from an arbitrary database does not approve that database. Import starts nothing;
the caller must close the reader. No environment variable activates it.

The reader rejects writable mounts, symlink components, hard links, SQLite
companions, unexpected message schemas and Hub control schemas. It pins owned
descriptors and verifies inode metadata and the original database digest around
reads. SQLite uses immutable read-only access to the pinned descriptor. Close
serializes against reads; inherited-process use is denied. Blocking filesystem
I/O cannot be force-cancelled by these cooperative deadlines. Large archives may
exceed the configured budget because each read hashes the whole database twice;
do not silently disable integrity checks to hide that limitation.

Only the server's existing authenticated principal may be passed to the reader.
Agents see rows where they are sender or recipient. Owner-wide history requires
the server-verified human identity; a syntactically valid `human:*` string is not
authentication. Unauthorized and absent message lookups both return no item.
Pagination is ascending canonical message ID, not chronological delivery order.
Responses are bounded projections marked `archive_inert=true`. Pending/claimed
statuses remain historical data; there are no send/claim/ack/reject methods.
Archived text is untrusted content, never an instruction or execution grant.

Independent source review accepted the reader. All 46 focused Linux tests passed
with ResourceWarning errors (0.11s); their positive mount flags are mocked.
Separately, `docker/Dockerfile.hub-archive-reader-acceptance` built synthetic
history on validation base
`sha256:a0e362a2878bbbc0d23905ef3e5ee785c31ac1175d546e94ef18464466e5fc90`.
Resulting fixture image
`sha256:a61a7bd688efacc4c3144ac413270cadceb2448ced1c30628fc063845ae98ba1`
was run without network, host mounts, sockets, credentials or capabilities, as
UID10001, one CPU, 512 MiB memory, 64 PIDs. Real kernel read-only access returned
three agent-visible rows versus four synthetic owner-visible rows and hid the
unrelated private message. A writable-root run was denied. All statuses stayed
inert. The independently recorded synthetic fixture digest was
`f0b63a49334789a1a9f16a746991968dbe92cb0a0e26c06e81f96fae1fc66771`.
This is filesystem/reader fixture acceptance, not actual archived-data approval,
REST/MCP/UI authentication, production deployment, or migration completion.

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
