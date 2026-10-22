# Offline ledger cutover: preparation and checkpoints

This is a procedure to review and rehearse with disposable data, **not permission
to apply it to TrueNAS or the live Hub now**. Current Compose, mounts, settings,
tokens and services remain unchanged. Publication is still a separate gate;
local tests do not prove the exact published image is available. Earlier billing
restrictions must not be assumed current after the repository became public;
verify the candidate's actual workflow and registry results instead.

## Scope

Use the selective layout in [protected-ledger-layout.md](protected-ledger-layout.md).
All host state stays under the existing Engineering-Hub root:

| Existing or future host source | Future container target |
| --- | --- |
| Existing `Engineering-Hub/settings.json` | `/var/lib/hub/settings.json`, read-only file |
| Existing `Engineering-Hub/agent-message-bus/` | `/var/lib/hub/agent-message-bus`, unchanged bus contents |
| Accepted `Engineering-Hub/private/` child | `/var/lib/hub-private`, protected existing ledger and later private staging material |

Do not keep the whole-parent bind alongside these mappings: it exposes the
private child through the shared tree. Settings and bus sources need not move.
Only the authoritative ledger is a candidate for reviewed cold relocation.
This document supplies no complete replacement Compose or automatic mover.
Beau applies any accepted NAS configuration in one later coordinated handoff.
The selective layout requires an explicit replacement entrypoint:
`python -I -B -m engineering_hub.owned_inactive_hub --config /var/lib/hub/settings.json`
for the initial inactive startup. The image's default/ordinary entrypoint and
version-one settings are not a protected-ledger fallback. They must refuse an
unmounted `/var/lib/hub` ledger rather than create an empty image-layer database.
The release rehearsal tests that refusal with writable container root and then
tests the correct protected startup separately. Do not apply this command now.

## 1. Establish the gates before a real maintenance window

Record the exact source commit, published immutable image identity and accepted
old image/configuration for recovery. Verify the installed-image protected
startup profile for that artifact. A locally built image can support a disposable
rehearsal; it is not acceptance of the live published artifact.

The operator must accept the actual host layout, container UID mapping, effective
ACLs and source-parent rename/delete protection. Container UID 568 requires its
private root to be mode0700 and the ledger/owner lock single-link mode0600 on
supported local storage. Neither this runbook nor startup repairs ownership,
permissions, directories or database contents. Do not recursively change shared
data or SMB permissions. NAS administrator access is not isolated by these checks.

Confirm recoverable coverage of settings, complete bus state, the original
ledger and the private child, including any child datasets. Do not assume a
parent snapshot policy includes new child datasets. Online ZFS snapshots are
valid; capture and demonstrated coherent recovery are different checks. No
snapshot deletion or retention change is part of this procedure.

**Checkpoint:** no live action until publication, layout, recovery coverage,
maintenance ownership and the operator's final application plan are accepted.

## 2. Classify the ledger; do not turn a refusal into a migration

The existing enrollment CLI supports an **unenrolled, already cold** ledger.
An enrolled ledger and its lock bind database and lock device/inode identities.
Copying them to a new inode does not preserve that binding. A pre-existing owner
marker or lock requires a separately reviewed relocation/recovery design: stop,
preserve it and do not delete markers, invent an enrollment waiver or retry apply.

Enrollment also refuses SQLite sidecars, unsupported schema/objects, incomplete
auth/queue/delivery table groups, nonterminal processing runs, nonterminal work
jobs, reserved machines and ambiguous delivery receipt IDs. It has bounded file,
cell, row and logical-data limits. Refusal is not permission to drop records,
cancel jobs, clear reservations, resend messages or widen those limits.

The current SMB source may not meet the CLI's private0700/0600 metadata gate.
Do not repair it merely to make `inspect` succeed. The separately reviewed cold
copy must meet destination requirements before enrollment inspection. There is
no generic source copier or existing-enrollment rehoming tool in this runbook.

## 3. Freeze writers and preserve the cold source

Stop all Hub and independent writers to the ledger and settings being changed;
include background scripts and any old service that could reopen the source.
An exclusive new owner lock does not prove legacy writers stopped. Record who
verified exclusion and how. Keep both old and new app admission closed.

Do not copy only a live SQLite main file or discard WAL/SHM/journal files. If
sidecars remain, stop for a reviewed coherent recovery/checkpoint procedure;
this document does not authorize executing one. Retain the original complete
cold state and old settings separately for rollback, never as a second running
authority. For the optional shared-child alternative, stop every writer to bus
databases, audit, files, knowledge and settings before moving those sources too.

An operator-reviewed copy may populate a previously accepted private destination
without changing the source. Verify byte parity before enrollment and preserve
the source hash and schema/table row counts and logical hashes. Reports contain
metadata and fingerprints, not row contents, session cookies, credential values
or private keys. Existing plan fingerprints are not proof of writer exclusion.

**Checkpoint:** original source unchanged; destination complete, private and
unenrolled; no ambiguous sidecars, ownership or work effects; rollback material
verified. Otherwise stop without replacing the current configuration.

## 4. Inspect and explicitly enroll only the accepted destination

The following CLI examples are for a reviewed Linux maintenance context or
disposable rehearsal. They must not be pasted into the running NAS app now.
Use the accepted container path below only when its private mapping is verified.
Keep the plan/result files in protected storage, never shared SMB notes, Git or
chat. Capture inspect stdout to a new operator-controlled private plan file;
do not overwrite an existing plan or any ledger file.

```sh
python -m engineering_hub.pilot_enrollment inspect --database /var/lib/hub-private/hub.sqlite3
```

Inspect emits a JSON document containing `plan` and `digest`. Preserve that
complete document unchanged; do not pass only its nested plan to the CLI. Review
the exact destination identity, schema/logical fingerprints and digest, then use
the accepted private plan file and its exact digest:

```sh
python -m engineering_hub.pilot_enrollment apply --plan /var/lib/hub-private/OPERATOR-REVIEWED-PLAN.json --expected-digest REPLACE_WITH_REVIEWED_64_HEX_DIGEST --writers-stopped --backup-verified
```

The two flags are operator attestations, not automated checks or permission to
skip the preceding gates. Apply rechecks the plan, creates the exclusive owner
lock and adds ownership records through its existing transaction. It does not
start a runtime or enroll a bus. A previously unenrolled database receives a new
owner database ID; it is not a retained pre-existing enrollment identity.

On any failure, retain the destination, plan and any newly created lock/journal
artifacts for inspection. Do not remove them, retry automatically or roll back
by stripping ownership tables. Compare the preserved non-owner logical snapshot
and the original source after enrollment; do not expect the enrolled copy's
whole-file hash to equal the unenrolled source hash.

## 5. Apply the separately reviewed configuration and admit inactive Hub

Only the final accepted handoff may replace the parent mapping with the three
selective mappings and change settings to version-two protected storage. All
sources must already exist; long bind syntax must use `create_host_path: false`.
Keep ports8770/8771, tokens and human Access policy unchanged. Do not include a
second authoritative ledger under the shared target.

The settings file is read-only in the container and loaded at startup. Atomic
replacement on the host may leave a running file bind on the old inode: make
reviewed changes before recreating the app, then verify the selected config.
Start only the explicit owned-inactive entrypoint after its prerequisites pass;
ordinary startup rejects version two. No queue/executor, credential creation,
processing, reconciliation, boot autostart or production activation is included.

Check the existing auth, tasks, decisions, notes/messages and historical records;
verify disabled execution and preserved delivery receipts without deliberate
redelivery. A check that writes a note, task, session or receipt is a new mutation,
not a read-only parity check. Record that boundary. Stop and reopen gracefully
only under the accepted maintenance plan, preserving owner/lock identity.

## 6. Abort and rollback boundaries

- **Before the new ledger accepts mutations:** keep the new service stopped.
  An accepted abort can restore the original image/settings/mappings and reopen
  the untouched original authority only after every new writer is stopped and
  the source/recovery evidence is checked. Retain the destination artifacts; do
  not delete them as automatic cleanup. Never run both authorities.
- **After any new authoritative mutation:** the original ledger is stale.
  Do not restore it as authority merely because startup or a smoke check failed.
  Freeze writers, preserve both ledgers plus complete bus/settings state and
  ownership artifacts, and obtain a reviewed recovery/reconciliation procedure.
  Reverting an image does not make ledger divergence safe or remove enrollment.

Opening the owner normally advances its epoch and guarded constructors can
perform schema writes. Track those effects too: lossless abort claims require
an explicitly accepted boundary, not an assumption that “inactive” means zero
writes. Session creation is also a mutation even without a submitted task.
These are operator recovery gates, not a claim that the original unenrolled
source becomes automatically unreadable or that software can reconcile two
divergent authorities. Do not present a test of inactive startup as proof that
every possible host-side restore choice is automatically prevented.

## Disposable rehearsal acceptance and one future handoff

Rehearse on synthetic data only: successful copy/enrollment/parity and inactive
recreation; plan tampering or changed destination refusal; pre-existing owner,
lock, sidecar and unresolved-work refusal; interrupted enrollment artifact
retention; pre-mutation abort and an explicit operator stop decision rather than
adopting the stale source after private mutations.
Use real CLI inspect/apply without manual owner insertion or production bypasses.
Record exact artifact and test results, and distinguish skipped checks.

Then prepare one operator handoff listing the accepted paths/mappings, exact
image, private plan/digest location, writer-exclusion and recovery evidence,
expected preserved state, chosen abort boundary and verification sequence. Until
that handoff is reviewed and applied, current Compose stays unchanged. Tests do
not prove NAS ACLs, a production cutover, snapshot restore or reboot recovery.
