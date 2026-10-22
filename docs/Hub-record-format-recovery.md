# Hub record-format recovery gate

Status: source-only recovery contract; **no live recovery or upgrade authorized**.
This covers PR52's persisted-record changes separately from private-ledger
enrollment and application deployment rollback.

## Observed incompatibility

The baseline is commit `a333704dcf8c14ef071d4af434928e657e5baed0`,
`src/engineering_hub/dto.py`, Git blob
`d45fce484f6a83ac6a5a545e314954b8035e44be`.
Its response DTOs forbid extra fields and restrict project IDs to five built-in
values. Candidate task/decision records can contain `details`, `links` and
`allow_text`, and can reference human-created projects. The baseline reader
cannot validate these records. A disposable old-source API reproduction returned
500 from tasks, decisions and overview after candidate writes; an old-source
clean-ledger control returned200. No production ledger was used.

`tests/test_hub_record_recovery_contract.py` retains a provenance-bound old DTO
excerpt and deterministically distinguishes legacy controls from incompatible
new records. It also reopens the current repository and validates full task,
decision and project records without discarding their fields. This is a record
contract test, **not another reproduction of the complete old REST server** and
not a recovery artifact. A legacy `schema` number or a healthy endpoint alone
does not establish record-format downgrade compatibility.

`tests/test_hub_full_state_recovery.py` additionally rehearses an ordinary,
unenrolled cold recovery with the actual Hub and bus repositories. It compares
complete database schemas/rows and file hashes, then verifies archived projects,
context/answers/images, notes, message claims/receipts, immutable file revisions,
sessions and idempotent replays. Source state remains unchanged; a held synthetic
run is never dispatched. This is not protected ownership transfer or NAS proof.

An ordinary same-Compose UI update and future protected executor enrollment are
separate operator changes. Do not require protected mounts or reenrollment merely
to update the ordinary UI. Neither may bypass its applicable recovery checks;
the protected identity-continuity requirements below apply only where enrolled
state is actually involved.

## Recovery choices and mutation boundary

Before upgrade, identify the exact candidate digest, current authority, existing
state layout, recovery owner and independently reviewed recovery strategy.
Inventory and rehearse using disposable copies. Record the last accepted
mutation boundary; session/receipt/owner/schema writes count, not just new tasks.

- Before candidate mutations, reverting may use an independently accepted cold
  checkpoint only after writer exclusion and parity are established. It is not
  automatically safe merely because no application job ran.
- After candidate mutations, **never point a333 at the new authoritative state
  or restore a pre-upgrade backup and call that lossless**. The prior checkpoint
  is now stale. Do not strip new fields, delete new records, rewrite project IDs,
  bypass DTO validation or split the authoritative ledger to make an old image
  appear healthy.
- Prefer reviewed forward recovery with the full new record format. Alternatively
  prepare a separate compatible immutable recovery image, independently reviewed
  and tested against a copy of the complete affected state. Neither artifact is
  supplied or accepted by this document. Without one, keep writers stopped and
  execution held; do not automatically retry, adopt a stale source, replay work
  or release ownership/fencing artifacts.

If the affected process is still running, obtain its accepted maintenance stop
procedure rather than copying a live SQLite main file. An operator may preserve
a coordinated snapshot or SQLite-supported backup as appropriate, but a raw
main-file copy without WAL is not proof of a complete checkpoint. Snapshot
existence alone does not prove restore parity or resolve divergent authorities.

## Preserve before attempting recovery

Retain the complete Hub and bus ledgers, SQLite WAL/SHM/journal state as present,
notes/files and image payloads, audit/delivery/claim history, settings, private
state, ownership/fencing/enrollment/lock artifacts, and both old/new candidate
identities. Record configuration and mount selection without putting credentials
in chat, Git or logs. Preserve protected credentials through the existing secret
handling process; do not copy them into a diagnostic artifact. Retain failed
recovery outputs for inspection. Do not delete the old checkpoint or overwrite
the sole new authoritative state during rehearsal.

There must be one authority and one permitted writer generation. Reconcile any
uncertain executor outcome before resuming; an absent receipt is not permission
to rerun a command. Native application rollback to a healthy application image
does not downgrade the Hub control-plane ledger and is separate evidence.

## Acceptance required before accepting an immutable recovery artifact

1. Bind reviewed source and artifact digest; include new-format and legacy tasks,
   decisions, dynamic projects, contexts/links, answers, images, notes, bus
   messages/receipts, reviews, runs, audit and ownership artifacts in a disposable
   checkpoint. Preserve IDs, revisions and idempotency, including archived data.
2. Stop all writers in the rehearsal, capture a complete checkpoint, recover on
   a separate copy with the proposed artifact, and compare logical state before
   and after. Verify affected API reads, authenticated human/agent access and
   every intentionally supported write; no silent dropping of unknown fields.
3. Prove restart, interrupted recovery and unresolved-work holds without double
   execution, duplicate message consumption or implicit new ownership. Keep the
   original authoritative copy unchanged until the operator accepts parity.
4. Separately verify real NAS ACLs/mounts, effective UID, protected state, memory
   headroom, accepted backup/restore and reboot behavior. Container fixtures do
   not establish these host properties. Beau performs the reviewed NAS change;
   no automatic state mover or configuration mutation is authorized here.

Until these gates pass, PR52's old-image rollback claim is rejected and live
upgrade readiness remains unproven. See the [offline cutover runbook](offline-ledger-cutover-runbook.md)
for additional owner-enrollment constraints; its pre-mutation abort is not a
post-write compatible recovery artifact.
