# Private terminal evidence

`hub_staging_service.terminal_receipt` implements a bounded secondary evidence
journal. It is not the authoritative worker database and cannot authorize a
terminal SQL transition, scratch release, cleanup, replay or adoption of a
previous runtime.

Each slot is one private 128 KiB regular file with two fixed 64 KiB frames. Before
admission, every byte is physically written and both file and parent directory
are fsynced. The first frame binds the original command/job/request fingerprint,
worker identity, original database device/inode and optional immutable lease
intent digest. The second records one bounded terminal observation and only
runtime facts actually captured by the caller. Both writes use fixed offsets;
terminal publication needs no new filename, append growth or rename.

The body is written and fsynced before the checksum/commit footer is written and
fsynced. Readback and exact private root/file identity checks follow. Incomplete,
replaced, multiply linked, wrong-mode or mismatched slots are uncertain. A live
write/fsync failure latches uncertainty even if readable checksum-valid bytes
remain. Reopening valid bytes yields evidence only; it cannot prove that a prior
process observed a successful fsync.

Metadata is bounded before serialization, including aggregate JSON escaping
cost, nesting and element counts. Do not put source archives, raw logs,
credentials or client data into this journal. The fixed allocation is not a
reason to construct an unbounded JSON object first.

## Integration requirements

Worker configuration may opt in with `terminal_receipts` containing exactly
`path` (an absolute, direct private child of `worker_root`) and `max_slots`
(1 through 256). The existing worker journal filesystem is used. No host path
or volume is added to the Hub Compose by this option. Absence preserves the
legacy behavior only where the journal has never opted in; removing a previously
recorded configuration cannot make retained evidence disappear.

The receipt directory belongs to the worker and is never mounted into source
containers. It may use the existing native worker journal filesystem; this
design does not require a third filesystem or alter the Hub's Compose mounts.
Preparing and binding the receipt precedes the SQLite accepted commit and thread
start. Persist the original slot capture/binding with admission. Unknown orphan
files, missing/replaced captures or configuration removal cannot bypass a hold.

Record the terminal observation before attempting the authoritative SQL terminal
commit. Receipt or SQL failure must veto new admission and retain uncertainty;
the execution thread must still clear its active handle safely. Scratch cleanup
retains the existing SQL-terminal-plus-original-absence barrier. Receipt recovery
is a read of the original operation's evidence, never permission to rerun it.

Slots are not automatically reused or deleted. Bound their count and include
their actual bytes in storage admission. An evidence retention/cleanup procedure
must preserve original IDs, open/held work and required recovery records.

The worker persists a receipt binding with its accepted job in the same SQLite
transaction, validates bounded retained slots against the original rows on
startup, and exposes original-command evidence through an internal read-only
method. No new HTTP or MCP route is supplied. The live per-command I/O failure
latch survives receipt-object reconstruction; a SQL-only failure can retain a
valid observation while the authoritative operation stays held. A persistence
fault blocks new jobs, deposits and cleanup effects but permits original-ID
reads. The execution thread clears its active handle even on persistence errors;
an empty active handle does not establish that admission or recovery is ready.

## Limits and acceptance

Prewriting blocks reduces allocation needed for a later receipt. It does not
guarantee writes or fsync under ENOSPC: filesystem journal metadata, CoW/shared
extents, device failure and unrelated writers can still cause failure. Accept
the actual filesystem and injected full-storage/power-loss behavior separately.
Do not label local overlay/tempfile tests as ext4 quota or physical crash proof.

Focused Linux tests exercise actual file writes/fsync plus short writes, torn
frames, replacement, symlink/hardlink rejection and failure injection. Actual
worker integration, private storage protection and application recovery need
their own evidence. A source-produced CI report is a different diagnostic
artifact and remains untrusted for provenance or execution authority.

The current receipts cover job completion. Destructive cleanup-command receipts
and their own full-storage recovery remain separate work; this mechanism does
not silently widen the job receipt into cleanup authority.
