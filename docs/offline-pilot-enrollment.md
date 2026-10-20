# Offline enrollment of an existing Hub ledger

This is an explicit maintenance utility, **not automatic startup, migration,
recovery or executor activation**. It lets the reviewed pilot service use the
existing Hub task/decision/authentication ledger without replacing it with an
empty database. Ordinary startup never invokes it.

Do not run this on a live database, a copied live SQLite file, an SMB-mounted
database, or an installed system without the operator's maintenance approval.
No TrueNAS state has been inspected or enrolled by these source tests.

## Operator prerequisites

1. Stop and verify **all** writers to this Hub database, including the old Hub,
   supervisors, detached services and independently running scripts. A newly
   created flock does not exclude old writers that ignore it. The utility
   cannot establish quiescence from a process list, absent sidecar or new lock.
2. Make a coherent backup and verify it can be restored. Keep it separate from
   the enrollment artifacts. Do not delete SQLite journals or ownership locks
   to make the checks pass; an existing journal/lock is a blocker to inspect.
3. The existing database must be on a supported local Linux filesystem, with
   an existing current-UID-owned mode0700 parent and mode0600 regular database,
   one hardlink, no symlink ancestors, and no WAL/SHM/rollback-journal sidecars.
   The utility does not create directories, alter ownership or permissions,
   discover credentials or access a NAS management interface.

Perform inspection and application as the same intended runtime UID on the same
machine/path. The database's device/inode identity is enrolled; moving/restoring
an enrolled database is not an automatic recovery procedure.

## Inspect, review, then explicitly apply

The commands below are templates for a separately authorized maintenance window;
replace the paths with the verified installation paths. They are not part of
normal Compose startup and must not be added to an entry point or retry loop.

```text
python -m engineering_hub.pilot_enrollment inspect --database /private/hub/hub.sqlite3
```

Inspection opens the cold database read-only and immutable, never constructs
`HubRepository`/`PilotOwner`, initializes a schema, checkpoints a WAL or recovers
a journal. Capture its complete JSON result in a private operator plan file.
Review the database path, fingerprint, table counts and digest. Values from
tasks, auth rows, tokens or decision records are not included in the plan.

```text
python -m engineering_hub.pilot_enrollment apply --plan /private/operator/enrollment-plan.json --expected-digest REVIEWED_DIGEST --writers-stopped --backup-verified
```

The two flags are explicit operator attestations, **not independently verified
facts**. The digest must match the reviewed canonical plan. Changed metadata,
database bytes, schemas, rows, counters, paths or ownership artifacts reject
application; inspect a changed database again instead of editing the plan.
Fingerprints and inode checks detect changed observations, not an adversarial
same-UID writer or an uninterrupted file lifetime across separate commands.
Stopping all writers is therefore a real prerequisite, not optional paperwork.

Application exclusively creates the fixed mode0600 `.pilot-owner.lock`, obtains
its lock, durably records that inode/name, then uses a zero-timeout SQLite write
transaction. It adds **only** the exact `hub_pilot_owner` and
`hub_pilot_owner_epochs` tables and initial enrollment rows. Before committing,
it verifies every original schema object and typed row (including rowids),
request receipt, audit/outbox sequence, fence, SQLite sequence counter and
application/user version is unchanged. It neither copies nor touches the
separate MCP bus database, tokens, files or knowledge store.

After success, retain the receipt and backup. The normal explicit
`PilotOwner(..., require_existing=True)` boundary can then recognize this
enrollment. Starting the approved service, enrolling a bounded executor, proving
deploy/health/rollback, and testing restart/reboot remain separate steps.

## Deliberately limited supported state

Supported schemas are the current eight core Hub tables plus SQLite's sequence
table, and optional complete groups for GitHub auth, Access auth, work-queue
coordination and delivery receipts. DDL must match the reviewed source; unknown
tables, custom indexes, triggers/views, partial groups, or existing pilot,
dispatcher or recovery markers are rejected rather than silently adopted.

Historical Hub runs may only be `succeeded` or `rolled_back`. Optional queue rows
must be terminal (`succeeded`, `failed`, `cancelled`) with no reserved machine.
Queued, claimed, held, pending, running, failed/uncertain Hub runs, or unknown
states block enrollment. The utility does not cancel, reset or erase them.
Task proposals, claims, independent reviews, decisions and authentication history
remain unchanged; enrollment is not permission to replay existing work.
Delivery receipts require confirmed canonical message IDs; pending/ambiguous
acceptance is a blocker, not a reason to retry sending.

For this initial maintenance utility, the database must be at most 256 MiB,
SQLite values/rows must fit a 1 MiB length limit, and the complete logical scan
must stay within one million rows and 512 MiB of canonical typed-row data.
These are refusal limits, not truncation or deletion policies. Larger ledgers
require a separately reviewed implementation; the tool never partially enrolls
a ledger after reaching a limit. Inspection forces SQLite temporary storage
into memory and walks every ancestor without following symlinks, rejecting
group/world-writable nonsticky or foreign-UID ancestors.

## Interrupted or failed application

A failed application before lock creation makes no enrollment artifacts. After
lock creation, any failure preserves the lock and database for explicit
inspection. A rolled-back transaction may leave an unenrolled database beside
a lock; a committed operation may have lost its response. **Do not retry, delete
the lock or fabricate owner rows.** Inspect the artifacts and use the reviewed
backup/recovery procedure. The error output intentionally contains no record
contents or raw SQLite exceptions.

Source tests use disposable Linux fixtures only. They verify read-only planning,
preservation, refusal conditions, transactional rollback, normal owned startup,
and rejection of cooperating preconstructed legacy repository writers. They do
not prove unrelated raw SQLite writers are excluded, operator backups are valid,
TrueNAS ACLs are appropriate, or a real host survived a reboot.
