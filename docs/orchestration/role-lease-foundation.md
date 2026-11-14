# Role coordination foundation

Roles are durable coordination metadata. They are not a dispatcher, worker,
execution grant, wake mechanism, or proof that a previous session has stopped.
The existing task owner, review and execution admission remain unchanged.

The authenticated Hub owner can list, read and configure roles through the
owner-only `/api/hub/v1/roles` endpoints, using the normal CSRF/origin checks.
Each role names projects, relative paths, standing rules, descriptive tool scope
and an explicit allowlist of installation IDs. Paths and tool scope are context,
not permissions. Changing scope while a lease or out-of-scope claim exists is
refused. An owner can clear an expired lease only with a reason; this records
that a handoff was not established and does not clear claims.

Only an eligible installation's currently authorized session may acquire or
renew a lease. Each takeover advances a generation. Mutations require the exact
session and generation and recheck the live session/grant and lease before the
Hub transaction commits. Releasing writes a versioned handoff and clears the
lease atomically. An expiry takeover is marked `expired_without_handoff`;
it is not proof of delivery or that the prior process stopped.

Sessions need explicit `role.read`, `role.lease`, or `role.claim` grants and
grants for all projects named by the role. Claims additionally require the
underlying `task.read` or `tracking.read` capability. The bounded role and claim
list operations filter inaccessible roles or task kinds before paging. A role
claim is a separate coordination record, survives session replacement and has
its own revision; it does not claim executable queue work or change the task's
actual owner. Unclosed claims may reference tasks whose state later changed.
Releasing such a claim records a reason, not a completion verdict.

This foundation does not implement user-request capture, dispatch records,
progress, automatic stale detection, notifications, or an orchestrator runtime.
Role changes are audited but intentionally omitted from the unscoped bus outbox
and SSE invalidations; clients must explicitly refresh. This avoids disclosing
role activity to recipients without the role's project grants.
Those require separate work and independent acceptance; no installed behavior
is implied by source availability.
