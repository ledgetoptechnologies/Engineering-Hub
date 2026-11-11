# Staging session Hub authorization bridge

This is an optional source feature. It does not apply grants, recover actual
tasks, deploy a build, replace credentials, or configure a listener. Foundation
sessions remain disabled by default. `HUB_AGENT_SESSIONS_HUB_BRIDGE=enabled`
additionally requires the explicitly configured session foundation in either
staging or production mode; the default is `disabled`. Existing approved
installations have no Hub rights. Production configuration and onboarding are
covered separately in [revocable-agent-production.md](revocable-agent-production.md).

Connection approval and Hub authorization are separate. Only the configured
connection owner, using the normal human browser session, Origin and CSRF
checks, can replace or revoke a grant. Agent bearers never fall back to cookies.
Project creation, update and archive remain human operations. Components are
metadata inside an existing project and confer no deployment or access rights.

## Owner HTTP contract

Prefix: `/api/hub/v1/agent-connections`. The original `/capabilities` response
retains exactly `enabled`, `environment`, and `owner`. Older/disabled backends
return 404 for the new `/hub-capabilities`; clients should hide this feature.

`GET /hub-capabilities` returns `{enabled:true, capabilities:[...],
features:{task_recovery:true}}`. The capability catalog is finite:

`project.read`, `component.read`, `component.write`, `task.read`, `task.propose`,
`task.claim`, `task.candidate`, `task.review`, `planning.read`, `planning.write`,
`decision.request`, `decision.read`, `entity.withdraw`. `decision.read` is an
explicit independent grant: it permits reading any decision and answer within
the granted active projects, including sensitive answer text. Grant it only
where that visibility is intended. It grants no answer or execution authority.
Existing grants do not acquire this new capability automatically.

`hub_decision_request` accepts an optional `client_request_id`. Reusing the
same ID for the same normalized request by the same installation and project
returns the original creation receipt, including after an answer; use
`decision.read` for current state. Changing the request under that ID conflicts.
Without an ID, each call creates a new decision.

The first keyed request creates a persistent receipt table atomically with the
decision. Back up the complete database and retain its receipts. Earlier cold
enrollment tools reject this additional table; do not drop it to force a
rollback. Earlier binaries do not honor keyed retries even before the table
exists. Stop old writers and hold/reconcile outstanding requests before any
version change. Use a reviewed compatible binary or a verified coherent backup
and an operator-approved recovery plan; restoring an older backup can lose later
decisions and receipts. This does not establish a live rollback procedure.

There are no implicit capability dependencies. A permitted mutation returns
its own result; a later read needs its separate read capability. A grant is the
explicit cross product of its projects and capabilities. Wildcards, unknown
capabilities, duplicate entries and empty arrays are rejected. Both target and
secondary project references require authorization. Human `GET /api/hub/v1/projects`
provides the active project IDs/names for the picker.

`GET /installations/{UUID}/hub-grant` returns:

```json
{
  "installation_id": "UUID",
  "revision": 0,
  "state": "absent",
  "projects": [],
  "capabilities": [],
  "expires_at": null,
  "approved_by": null,
  "approved_at": null,
  "effective": {
    "state": "absent",
    "reason": "grant_absent",
    "projects": [],
    "capabilities": [],
    "expires_at": null
  }
}
```

`PUT` at that path accepts exactly `{expected_revision, projects, capabilities,
lifetime_seconds}`; lifetime defaults to 86400 and is bounded 60..604800 seconds,
with actual expiry capped by the connection expiry. Projects have 1..32 unique
active existing IDs; capabilities have 1..12 unique catalog entries. Revision
starts at zero for absence and increments on each replacement/revoke, separately
from the installation revision. `POST .../hub-grant/revoke` accepts exactly
`{expected_revision}` and retains prior metadata for owner history.

Top-level state precedence is inactive connection, absent row, revoked grant,
expired grant, active grant. The server computes effective rights. Archived
projects are excluded and cannot be read or mutated by an installation, even
for withdrawal. An otherwise active grant with no active projects has effective
state `inactive`, reason `no_active_projects`, empty rights and null effective
expiry. Reasons are `connection_inactive`, `grant_absent`, `owner_revoked`,
`grant_expired`, `no_active_projects`, `authorized`. All non-active effective
states return empty project/capability arrays and null effective expiry.

Errors include `grant_revision_conflict` (409), `invalid_hub_grant` (422),
`hub_grant_required` (403), `hub_capability_forbidden` (403),
`hub_project_forbidden` (403), `project_archived` (409), and existing installation,
project, session and validation errors. Strict body validation uses the existing
`invalid_input` (422) response. Grants cannot authorize unknown projects.

## Exact claim ownership and bounded human recovery

An installation actor is `installation:<canonical UUID>`, never its family.
Tasks store `owner_session_id`, immutable `owner_family`, `candidate_family`
when a candidate exists, and `proposed_family` for installation proposals.
Installation review objects add `family`; installation withdrawals add
`withdrawn_family` and `withdrawn_session_id`. The strict DTO accepts these
actor/provenance fields; legacy records retain their old representation.
The UI must support this actor union before enabling the feature.

Candidate continuation requires the exact claiming session. Another session of
the same installation cannot continue it. Review independence compares family
across legacy, installation and session identities; a second PC cannot bypass
same-family review denial.

Owner `GET /installations/{UUID}/task-claims?limit=50&after={UUID}` returns
`{installation_id, items, next_after}`. Limits are 1..100. Each item contains
`{task, claim_session_id, claim_state, recovery}`. Claim states are `active`,
`expired`, `revoked`, `unknown`. Recovery is `{state, reason, target_session_ids}`;
state is `recoverable` or `blocked`, with reason `ready`,
`claim_session_active`, `invalid_claim_provenance`, `task_state_ineligible`,
`task_execution_present`, `recovery_history_full`, or `no_authorized_target`.
The server supplies live eligible target IDs only for a recoverable claim.
The POST always rechecks the authoritative state.

Owner `POST /tasks/{UUID}/recover-claim` accepts exactly:

```json
{
  "expected_revision": 4,
  "target_session_id": "UUID",
  "reason": "Owner explanation, 1..500 characters",
  "client_request_id": "ASCII-key"
}
```

Request keys match `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. The original owner
session must be expired or revoked, and the target must be a live session of
that same installation with a current project/`task.claim` grant. Legacy,
unknown or inconsistent claim provenance refuses. Claimed/in_review/verified
metadata tasks qualify; every accepted work-job row, including terminal rows,
and every pending/running/held/failed associated run vetoes recovery. Worker
leases, execution reservations and native holds are never changed.

Success preserves candidate/family/stable owner, clears current review, returns
to `claimed`, and increments task revision once. It records an immutable receipt
with `id`, `task_id`, `installation_id`, `family`, `prior_session_id`,
`target_session_id`, `prior_revision`, `resulting_revision`, `actor`, `reason`,
`at`, `prior_candidate_commit`, `prior_review`. Response is
`{task, recovery, replayed}`. The owner/request-key fingerprint includes the task,
expected revision, target and reason. An identical retry rechecks current target
authorization and returns the current task and original receipt with
`replayed:true`; it never performs another rebind. Changed payload conflicts.

`GET /tasks/{UUID}/claim-recoveries` returns `{task, recoveries}` to the owner.
History is capped at 128 per task; further mutation refuses instead of trimming
evidence. Errors include `claim_session_active` (409),
`task_execution_present` (409), `invalid_recovery_target` (422),
`recovery_history_full` (409), revision/idempotency conflict (409), and existing
session/grant denials. There is no agent recovery tool.

After human owner authentication, an inactive installation is an owner operation
conflict (`installation_inactive`, 409); an expired/revoked recovery target is
`invalid_recovery_target` (422), including a target that expires before commit.
These do not signal lost browser sign-in. Actual human authentication failures
remain 401; the agent session authentication endpoints retain their existing 401.

New connection audit actions are `hub_grant.updated` and `hub_grant.revoked`.
Hub task recovery action is `task.claim_recovered`. Public records contain no
credentials, bearer headers, cookies, claim secrets or private keys.

## Admission and storage boundary

A typed immutable principal is resolved from the exact configured session
repository. The trusted request context carries installation, session, family,
expiry, authorization revision, capability and project scope. Public actor
strings cannot create this context. Context tokens reset on every exit.

Lock ordering is session SQLite writer, Hub writer, then shared-knowledge writer.
The session writer is held through synchronous metadata operations. Revocation
uses the same writer, so a mutation either commits before revocation or observes
revocation and refuses. Actual Hub and knowledge commit hooks recheck session,
grant revision and expiry; knowledge provenance is committed with the planning
record in that separate database. Hub audit provenance is stored transactionally
in `hub_session_provenance`; planning attribution uses
`knowledge_session_provenance` with project/path/version. Idempotency uses the
stable installation actor while audits retain the acting session.

An OS lock and process identity check refuse a second bridge owner. This is one
owned process/repository composition, not protection against arbitrary direct
database writers. No admission guard spans network calls, async execution or a
remote worker. Failed owner construction does not replace the running owner.

Only the enumerated project/component/task/planning/decision tools are bridged.
Global overview/run tools, generic knowledge/files, admin operations, execution,
worker admission/leases and registry forwarding remain closed to sessions.
Shared MCP discovery retains its existing catalog; unsupported calls and all
resource/batch/unknown-method requests are independently denied. The finite
browser gateway table exposes owner APIs with normal Access/session/CSRF checks,
not raw session enrollment/exchange routes. Native wake integration is separate.

Tests use synthetic state in the approved isolated cached container. The wire
fixture is exported from actual owner HTTP, strict DTO and MCP results, including
an in-process public/private gateway run with a synthetic Access verifier. This
is not installed/deployed acceptance or proof of automatic native wake delivery.

## Compatible rollback and containment

Persisted installation actors and provenance are a storage compatibility
boundary. The old b307/f837 foundation DTOs reject those task/decision records;
an old runtime may boot with a green health response while overview/tasks fail.
Do not remap actors to families, delete records or rewrite accepted history.

Only roll back to an independently verified image that supports this actor
format and its matching UI contracts. Preserve the post-enablement databases
and reject an unvalidated prior-image target. Disabling the bridge in the same
compatible image is containment: existing owner reads remain supported while
new grant routes and session-to-Hub calls close. It is not an old-foundation
rollback. Foundation session tools may remain available if separately enabled.

An isolated test creates installation records, closes the bridge-enabled app,
and reopens the same state with this image's bridge disabled. It verifies strict
owner overview/task/decision reads and closed new-grant/session-Hub access.
The exact immutable f837 DTO source fixture (SHA-256
`3503494a2864784512cb4b0ce7e82d09419ff6099f0463eda8fa713cf1231981`)
demonstrates baseline rejection. This is format evidence and containment testing,
not proof of a full immutable-image rollback. Operator verification of a
compatible target image, gateway and matching UI remains required before use.
