# Modular staging service, version 1

Beau approved implementation on 24 October 2026. This separates execution on the
staging PC from EH authentication, project authorization, queue, decisions and
history. It does not authorize production, DNS/Access-policy changes, retirement
of old held campaigns, or repairing the existing offline TEST filesystem.

## Ownership

- Codex: EH backend, shared queue integration, private client, project grants,
  resource/cleanup orchestration, preview ticket gateway and integration.
- Claude: `hub-ui` resource list, keep/cleanup controls and preview frame.
- Hermes: staging inventory, deployment layout and owned cleanup review. Initial
  inventory is read-only; implementation paths are assigned explicitly.
- Worker package: `hub_staging_service`; resource repository:
  `engineering_hub.staging_resources`. These are separate modules, not uploaded
  executable code inside EH. A worker execution journal is not another scheduler.

## Resource wire contract

```typescript
type Kind = 'workspace' | 'build' | 'test_run' | 'preview';
type Resource = {
  id: string; kind: Kind; project: string; topic: string | null;
  job_id: string; source_commit: string; artifact_digest: string | null;
  created_by: string; created_at: string; updated_at: string;
  last_viewed_at: string | null; expires_at: string | null;
  state: 'provisioning' | 'ready' | 'failed' | 'expiring' |
    'deleting' | 'deleted' | 'delete_failed';
  size_bytes: number | null; preview_path: string | null;
  keep: null | {mode: 'until' | 'pinned'; until: string | null;
    by: string; reason: string; at: string};
  protected: {reason: 'active_job' | 'current_release' | 'rollback_target' |
    'held_campaign' | 'decision_evidence' | 'latest_failure'; ref: string}[];
  revision: number;
};
```

All timestamps are UTC ISO8601; IDs are canonical UUIDs. New Hub submissions
must supply a Git commit object and an independently approved exact task revision.
Hub and worker verify the archive against the commit tree. This establishes
object/tree consistency, not repository membership, authorship or a signature;
Git SHA-1 collision resistance is a limitation. Resource IDs and canonical ownership are
issued by EH, not supplied by a browser or inferred from Docker discovery.
Unknown size stays null. A project record alone grants no execution or read access.

V1 accepts ZIP sources for fixed offline build/test profiles and serves static
website output. It does not host arbitrary long-running backend services or
download dependencies during a job. Preview builds must use relative asset URLs;
paths are canonical ASCII, with no query strings or SPA fallback routing yet.

Use the [committed-source packager](staging-source-package.md) to prepare a
development ref without including working-tree changes or fetching missing Git
objects. It records the archive digest and exact dependency/cache-label inputs.
The package contains the separate commit-object proof; worker acceptance is a
separate step, and a matching reviewed CI cache/profile must still be enrolled before
repository tests can run. Packaging neither submits work nor grants access.

### EH API

- `GET /api/hub/v1/staging/status`: configured state, worker reachability and
  supported profiles. Never expose credentials, host paths or internal URLs.
  Shape: `{configured, running, reachable, profiles, previews_configured}`;
  the first three fields are booleans. An optional `state` explains an absent,
  invalid, disabled or unavailable configuration. Reachable is an HTTP status
  observation, not verified execution readiness.
- `GET /api/hub/v1/staging/resources?project=&kind=&state=&after=&limit=`:
  `{items: Resource[], next_after: string | null}`; limit 1–100.
- `GET /api/hub/v1/staging/resources/{id}`: one Resource.
- `POST /api/hub/v1/staging/jobs` requires `review_task_id`,
  `review_task_revision`, `source_commit_object_base64`, exact source commit,
  archive digest and archive bytes. The review must belong to the submitter,
  name the same commit and be independently approved. The queue rechecks it at
  submission, claim and durable dispatch intent; active work fences edits.
  Legacy unreviewed queued rows remain blocked and need explicit reconciliation;
  they are never upgraded or replayed automatically.
  A legacy attempt without a complete matching source binding is reported as
  `phase=held`, `hold_reason=source_binding_unavailable`, and is not polled or
  reposted automatically. An unclaimed queued row remains queued in the
  canonical queue without a fabricated machine reservation. A claimed or held
  attempt retains its existing reservation. A pre-existing durable no-effect
  proof remains recoverable, and historical terminal results are not rewritten.
  Restoring a field or adding a later no-effect marker alone never clears the
  sticky manual hold.
- `POST /api/hub/v1/staging/resources/{id}/keep`:
  `{mode:'until'|'pinned'|'none', until?:string, reason:string,
  expected_revision:number}` → Resource. Human mutation requires existing CSRF.
- `POST /api/hub/v1/staging/cleanup/plan`:
  `{project?:string, kinds?:Kind[]}` →
  `{plan_id:string,digest:string,remove:{id:string,kind:Kind,size_bytes:number|null,
  why:'expired'}[],kept:{id:string,why:string}[],total_bytes:number|null,
  expires_at:string}`.
- `POST /api/hub/v1/staging/cleanup/apply`: `{plan_id,digest}` →
  `{removed:string[],failed:{id:string,code:string}[],skipped_changed:string[]}`.
- `POST /api/hub/v1/staging/resources/{id}/preview-session`: `{}` →
  `{url:string,expires_at:string}`; authenticated human + CSRF + project read
  permission, ready immutable preview only. Never return the upstream worker URL.

Policy management is owner-only. Grants bind actual authenticated actor IDs,
project and actions `read`, `build`, `test`, `preview`, `cleanup`. Agents cannot
grant themselves permissions, pin forever or delete another actor's resources.
No default project grant is created by existence or lead-agent assignment.
Administrative actor IDs are explicitly operator configured; no first-user wins.

## Execution boundary

EH's existing WorkQueue remains the authoritative machine reservation and
production-first ordering source. A staged build/test action is a sandbox job,
not production deployment or evidence that arbitrary code was independently
reviewed. Existing deployment/review/held-work gates remain unchanged.
The `staging-service.` action-key prefix is reserved for this module; queue
admission fails closed if its staging submission table or binding is absent.
Configured and shared compositions now use that same canonical key. Previously
configured `modular.<64 lowercase hex>` work remains in the existing ledger;
open legacy rows cannot be claimed or automatically reconciled, polled, replayed,
or released, including rows with an earlier no-effect proof or worker receipt.
An operator must review the original reservation and evidence through a separately
trusted recovery process. Do not re-enroll or resubmit to bypass it. Terminal
history remains readable. Unrelated generic `modular.*` actions are unchanged
unless bound to a staging submission.

Worker core API: `submit(payload)`, `status(job_id)`, `metadata(resource_id)`,
`file(resource_id, relative_path)` and `cleanup(payload)`. HTTP equivalents:
`POST /v1/jobs`, `GET /v1/jobs/{id}`,
`GET /v1/resources/{id}/metadata`,
`GET /v1/resources/{id}/files/{relative_path}`, `POST /v1/cleanup`.
Submission includes EH-issued job/command/resource UUIDs, project, source commit,
source ZIP SHA256, bounded ZIP base64 and a configured recipe. Service profile
selects an immutable cached image ID and container command; the caller cannot
choose host commands, Dockerfiles, image URLs, host mounts or Docker contexts.

The worker admits one operation at a time (busy returns409); it does not create
a second scheduling queue. Identical operation retries return their original
receipt; conflicting payloads fail. Unknown or restart-interrupted operations
become held and are inspected, never blindly replayed. Isolated execution uses
no host network/devices/Docker socket inside jobs, no privileges, dropped
capabilities, a non-root user, read-only root, bounded memory/CPU/PIDs/logs/disk
and job-only storage. Images are cached and pinned; no implicit pull.

## Cleanup policy

Defaults: previews3 days, terminal workspaces24 hours, build outputs14 days and
test payloads/logs30 days. Human-selected keep-until or pin overrides expiry.
Current release, last good rollback, active jobs, held campaigns, decision
evidence and the latest diagnostic failure are protected. Old 5550/8a5b state
and unknown resources are never adopted into this registry or deleted by it.

Cleanup plans expire after10 minutes and bind actor, IDs, revisions and digest.
Apply rechecks permission, expiry, job state, protection and revision in a
transaction before marking items deleting. Worker deletion is confined to
registered owned resource paths/containers and rechecks ownership and liveness.
For a human cleanup apply, the exact browser session is checked in the same
ledger writer immediately before both the initial cleanup commit and the
dispatching journal commit. Logout is serialized with those commits; expiry is
checked just before commit, not continuously through commit. Once the exact
dispatch intent is committed, a later logout does not cancel that already
admitted operation. Its original outcome is reconciled without another POST.
EH records confirmed deletion only after a matching worker receipt; timeouts
stay uncertain for inspection. No Docker prune, global directory sweep or
automatic history deletion. Compact tombstones and job summaries remain.

## Preview isolation

Preview HTML/JS is never served as normal EH-origin content. The recommended
gateway has ONE separately approved preview origin (no per-demo domain
purchase), opaque sandboxing, and short-lived read-only capability URLs minted
by the current EH human session. Every asset checks session/policy/resource
binding and revocation. No EH cookies, Access assertions, CSRF values or agent
credentials enter preview content or worker requests. Tokens are never logged.

HTTP CSP and the EH iframe both use `sandbox allow-scripts`, without
allow-same-origin/forms/popups/top navigation. Ticket-scoped static assets use
credentialless CORS, not the EH cookie/Access gate; this is required for real
ES-module, dynamic-import and relative JSON/font loads from opaque origins.
No global EH CORS relaxation, arbitrary upstream URLs, redirects, service
workers, dev-server/HMR/backend proxy or privileged ModuleFrame bridge.

Until a preview origin is explicitly configured and its hostname/route approved,
the preview launch endpoint reports unavailable. Development browser tests use
separate local origins. This is a deployment prerequisite, not permission to
change DNS, Cloudflare or the current TrueNAS app.

## Acceptance and deployment

Verify real isolated build/test output, source ZIP rejection, durable idempotency
and restart-held behavior; project denial and revoked grants; cleanup pin/use
races, expiry, foreign-resource denial, symlinks and partial failure; and real
browser ES modules/CSS/fonts/JSON plus parent/session/cross-preview isolation.
Keep main-only hosted CI and run branch development checks locally.

Use a dedicated service location on the staging PC and small bounded fixtures.
Current PA/Viewer, live MCP identity/ports/data, ordinary EH Compose and the old
offline TEST filesystem remain intact. No permanent service activation, secret
rotation or public route change follows merely from a test passing. Publish
tested code, make required operator steps concrete in EH, and distinguish local
integration from the actual NAS-connected pilot/rollback/reboot acceptance.

Failure-protection maintenance uses an index on the Hub audit ledger to verify
committed terminal ordering before removing an older failure's protection. The
index is created if absent when the staging resource store starts. On a large
existing ledger its first creation takes a SQLite writer lock and may delay
startup; plan the migration in an appropriate maintenance window and retain the
normal backup. If creation fails, staging initialization fails closed rather
than dropping history.
A failure after some independently validated older protections were removed
leaves the remaining protections intact and reports a maintenance warning.
