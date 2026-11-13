# Future stale evaluator authority (inert foundation)

The owner-only stale-service grant is disabled when absent. A Hub owner may
authorize a bounded list of active projects for a limited lifetime only when
the ledger has its exact live `PilotOwner`; the owner can revoke by revision.
The grant permits only a future in-process stale evaluator, never execution,
deployment, inbox polling, message delivery, or session impersonation. No
evaluator, timer, sender, automatic stale check, or wake is installed here.
The owner endpoint reports `scheduler: not_installed` and
`delivery: not_configured` even when a grant is authorized.

An internal capability binds the exact Hub repository, PID, live owner lock and
owner epoch. It rechecks project scope, emergency pause, grant revision,
revocation, wall clock and Linux `CLOCK_BOOTTIME` on entry and before the
repository's final owner/context checks and SQLite commit. The grant is tied to the current kernel
boot and owner epoch; a reboot or owner-epoch change requires fresh owner
approval. Unsupported boot/clock sources fail closed. The precommit deadline
check is cooperative: those checks or a blocked synchronous SQLite commit can
outlast the deadline and cannot be forcibly interrupted, so it is not a strict
real-time expiry fence.

Grant and revoke use the same Hub writer transaction as the future evaluator;
whichever commits first is ordered before the other. Updates are audited and
do not enter the unscoped generic bus outbox. Owner configuration is available
only through an authenticated owner session and CSRF-protected mutation.
The internal capability is not a sandbox against hostile code already running
inside the trusted Hub process; any future evaluator must be separately reviewed
and cannot expose this internal transaction boundary to callers.

This configuration does not establish a private Engine, host worker isolation,
staging admission, actual recipient mapping, message delivery, or wake. Any
future service must explicitly opt in, recover deadlines safely across restarts,
revalidate the grant and recipient at each effect, use bounded resources and
avoid silent scheduled AI inbox polling.
