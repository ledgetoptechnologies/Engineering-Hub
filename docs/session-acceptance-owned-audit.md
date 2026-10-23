# Owned SQLite session-acceptance audit — source component only

`OwnedSessionAcceptanceAudit(repository)` is an explicit synchronous callback for
the existing broker. No current runtime imports/selects it, and nothing starts,
enrolls credentials, opens a session, provisions storage or changes a live app.

The exact `HubRepository` must have its current exact `PilotOwner`. Construction
and each append use that repository's owner-checked transaction and the same Hub
SQLite database. The new `hub_session_acceptance_audit` table stores canonical
intent/result records keyed by opaque request ID/event. The sink checks its exact
table/index schema and denies extra indexes or triggers; it never repairs an
existing incompatible table. It requires existing WAL journaling and FULL or
EXTRA synchronous mode on each actual writing connection without changing those
settings. No secret or fresh challenge is recorded.

The callback accepts only exact typed `SessionAcceptanceAudit` records with
strict actor, UUID4 epoch/generation, SHA256 digests, request ID, integer revision,
event/outcome and `execution_authorization is False` fields. The current owner
epoch must match. A result requires its matching committed intent, including all
operator/epoch/target/manifest/report/executor/revision bindings. An accepted
result follows accept intent, declined follows decline, and failed can follow
either. Duplicate/replayed/conflicting records raise and never overwrite.
Reopening a ledger preserves history, not acceptance: a new owner epoch cannot
append a result under the previous epoch or use those rows to open a gate.

`True` is returned only after the repository transaction's SQLite commit returned
successfully and a final owner check passed. Exceptions are sanitized and seal
the sink; they propagate to the broker's fail-closed handling. The caller retains
the owner/repository. If commit completed but the final owner check fails, the row
may remain durable although the callback raises. Preserve evidence: this is not
rollback, a successful session result, permission to retry, or an accepted-bit
recovery mechanism. The callback never reads audit history to authorize work.

## Durability trust and limits

SQLite WAL FULL/EXTRA commit requests the VFS/OS synchronization needed for its
commit guarantees. This source verifies the relevant PRAGMAs and successful commit,
not the physical truth of fsync on a particular NAS, mount, network filesystem,
controller cache or device. Existing private storage permissions, local filesystem
and exclusive-writer/restore acceptance remain operator obligations. Successful
tests on ephemeral Linux fixture storage are not power-loss, ZFS snapshot, NAS
ACL, protected restore or reboot evidence. Trusted same-UID/root administrators
can still modify or restore database files; this is not anti-rollback protection.

The existing bus `AuditLog.append()` is intentionally unsuitable: it swallows
write failures and does not provide this durable acknowledgement contract. No
additional always-on audit service, credential or separate authority is needed.
Session intent/result durability remains distinct from task approval and from
the broker's in-memory gate opening; no cross-component transaction is claimed.
