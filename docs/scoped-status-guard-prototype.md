# Scoped status guard: source-only prototype

This design does not enable scoped work, establish an external writer fence,
authorize a migration, or prove that a machine or Docker Engine is idle. A
successful null status is only an observation of local journal state.

Legacy-only startup makes no new indexes or other migration. On an idle legacy
status request the prototype opens a separate read-only SQLite connection and
uses a single WAL read transaction for scope-table, operation and scratch-lease
checks. It applies a 100,000 SQLite virtual-machine instruction budget and
checks an approximate 250 ms deadline; budget exhaustion, invalid JSON,
unavailable paths, database errors and changed path identities fail closed with
503. These limits are not a strict wall-clock bound: an individual SQLite or
filesystem operation can block beyond a progress callback. The shared service
connection's progress handler is not modified.

The prototype records the worker-root and journal path device/inode identities
and checks them before and after the read. Those checks do **not** prove which
inode SQLite actually held, exclude an ABA replacement, fence another writer,
or detect all restored/rolled-back copies. A separate independently protected
host, writer and rollback boundary is required before such identity matters as
execution authority. Wholesale removal or rollback of every local ownership and
operation marker cannot be distinguished from a never-scoped journal.

Any surviving source-attempt table, scoped ownership table, principal marker,
unsettled operation or unreleased scratch lease causes the status guard to
require the two exact partial indexes below. A legacy journal that happens to
contain an unscoped source-attempt table is also treated conservatively. A large
legacy-only journal may exhaust the status read budget and return 503 even if
all its operations completed. Do not replace 503 with null or infer that 503
means a particular principal is active. The error can reveal coarse occupancy
or unavailability, but not a specific identity.

No code in this prototype creates these indexes during startup or status. A
future owner-controlled, offline migration must verify exact target/ownership,
coordinate all readers and writers, measure the actual journal and free space,
budget creation/rollback time, retain backups, and verify index definitions and
query plans before starting a worker using them. Both indexes can scan the full
existing journal and consume disk; no live migration has been authorized or run.
The exact proposed DDL is:

```sql
CREATE INDEX status_unsettled_operations ON operations(state)
                WHERE state NOT IN ('succeeded','failed') OR state IS NULL;
CREATE INDEX status_unreleased_leases ON scratch_leases(state)
                WHERE state != 'released' OR state IS NULL;
```

The indexes are only local query aids; they are not rollback protection,
authentication, corruption proof or a guarantee of worker quiescence. A
separate proof spanning the relevant effect and release remains required.

The global legacy runtime endpoint remains unavailable once scoped
configuration/history is present. An admin `StagingObserver` still using that
endpoint therefore reports unavailable. The optional, separately provisioned
operator-observer identity and endpoint described below are a source-only
prototype requiring independent review and rollout qualification. Do not grant
global inventory to a scoped project token or silently fall back to a broader
credential.

For a legacy-only runtime request, the scope check uses the same bounded,
isolated read-only snapshot instead of iterating the entire history on the
shared writer connection. A valid active legacy job alone does not hide legacy
runtime, but malformed payloads, scoped markers or any source-attempt table
deny it; a sufficiently large legacy history may exhaust the read budget and
return 503. These compatibility and availability costs need review against the
actual installed journal before rollout. The status index migration does not
remove the budget on the runtime payload-scope scan.

Scoped source records are currently retained without a complete scoped garbage
collection protocol. This can exhaust the journal/source quota. Do not delete or
reassign them until independently reviewed ownership, retention, original
receiver/epoch fencing and bounded cleanup/recovery are implemented.

The optional receiver `observer_credential` must be an object containing only
`token_file`; it is a separate private static bearer and must differ from every
legacy and project bearer. It permits only `GET /v2/operator/runtime` with no
query string. The optional Hub observer config `operator_v2: true` selects this
route and never falls back to the legacy endpoint. Without observer configuration,
the observer continues to use the legacy route, subject to the status and runtime
compatibility effects described above. This global operator inventory can contain names and
images belonging to other projects; provision its credential only to authorized
operators. It remains informational and does not prove physical idleness,
worker fencing or live credential revocation; token rotation requires receiver
restart. These are source-only changes, not a deployed or approved grant.
