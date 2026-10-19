# Remote software staging: next integration boundary

Status: explicit source API, not an enabled service or host installation. Beau selected
source-only work with existing equipment; no extra PC or mandatory VM. Hub's
control database stays local on TrueNAS, with a constrained software executor on
the existing staging PC. Processing workers remain separate. Existing Viewer/PA
applications are not disposable fixtures. No host change follows from this plan.

## Reuse the existing remote protocol

`ContainerPilotClient` already supplies the typed, bounded remote command path
to `PilotASGI` and `HostAdapter`. The host owns its private command/operation
ledger, pinned Engine binding, approved build records, baseline and state.
`OwnedPilotService` supplies control-side ownership, workflow and recovery.

`WorkQueue` separately supplies production-first eligible ordering and physical
machine reservations. Its default claim admission is closed. Production priority
does not grant production deployment authority; the first executor remains
staging-only. Neither an agent bearer nor a normal decision enables a host.

`OwnedValidationService` is a local queue composition, not an HTTP queue client.
A new launcher on the staging PC cannot reach the NAS-local queue by itself.
Do not share the Hub SQLite database over SMB, borrow another agent's SSH
credential, or treat an arbitrary Docker context as an approved executor.

## Implemented private queue-to-pilot bridge

`engineering_hub.queued_pilot.QueuedContainerPilot(queue, workflow, *, action_key,
machine_id)` binds a privately claimed queue job to one durable pilot run before
sending any remote command. Both objects must share the same repository and live
`PilotOwner`; the principal is `executor:container-pilot`. It enrolls one explicitly
registered staging-only Engineering Hub action and one complete immutable pilot
manifest. This is not the default `configured_work_queue` action registry, a
remote worker daemon or a public enrollment API. No environment flag enables it.

Construction persists policy and intent-table schemas, attaches the exact bridge
to its repository and installs bound queue policies. It preserves an existing
target verifier as an additional per-effect constraint, and rejects competing
claim/completion/reconciliation policies. Existing legacy dispatcher tables,
unbound active pilot runs and unbound physical reservations prevent enrollment;
this is not an implicit migration or adoption tool. Reopening requires the same
enrolled action/machine/manifest. A missing in-process bridge cannot bypass the
persisted marker, even through an older already-constructed repository/workflow.

`await bridge.run_once()` claims at most one eligible job with a 600-second lease,
persists original intent, creates/maps the original run, then advances the existing
remote workflow. It returns the queue result or `None` when no work is claimable.
`await bridge.recover(job_id)` inspects original held work through `PilotRecovery`;
it does not submit remote effect commands. These methods start no watcher or loop.

Run creation enforces admission within its transaction before idempotency lookup.
Each remote phase rechecks the current owner, physical queue fence, lease, review,
registry and original run binding. Direct pilot HTTP routes and legacy dispatch
are disabled after enrollment. Public agent bearers cannot select the private
executor role. Completion requires the original strict command/health evidence,
matching container chain and quiescent original attempt, not a terminal label.

The current application-factory queue/pilot mutual-exclusion guard stays in force.
A separately reviewed lifecycle factory and native-event runtime are still needed
to connect this explicit source API to installed autonomous scheduling, without
re-enabling direct dispatch. No service composition, transport credentials, host
writer exclusion, baseline enrollment or runtime identity issuer is supplied here.

Required invariants:

1. Trusted composition binds the enrolled manifest and runner to the canonical
   physical machine. Caller-supplied container names or machine aliases cannot
   create independent capacity. Existing CI/manual writers must also be excluded
   or coordinated before activation; a sidecar lock alone cannot stop them.
2. Persist the original task revision, exact source/artifact/manifest, queue job,
   instance/fence, owner epoch and stable run-creation request ID before creating
   a run. Recover an interrupted creation using the original idempotency record,
   not a new request ID. Persist the resulting run mapping before advancing it.
   Preserve digest types: the current `ContainerPilotTarget.artifact_digest` and
   pilot run artifact identify the Docker image config, whereas `manifest_digest`
   identifies canonical pilot-manifest bytes. Neither is an OCI index digest.
3. Run creation increments the task's revision but leaves its status `verified`.
   Candidate failure also changes revision/repair count, not review authority.
   Preserve the existing verified-source and independent-review checks, and bind
   the exact original task/review to the run. Do not introduce an exception for
   arbitrary running tasks or stale reviews based on an assumed status change.
4. Only the private executor may advance its bound run. Public pilot routes,
   legacy dispatch and recovery must not bypass the same physical reservation.
   Agent tokens continue to identify agents, not privileged executor instances.
5. Completion reads the original bound commands, run and verified health proof.
   Release on verified successful deployment, or mark the queue job `failed`
   after verified healthy rollback while preserving pilot status `rolled_back`.
   Rollback and its original smoke must bind the same expected container/image.
   A generic `failed` run, client
   assertion, HTTP success, elapsed lease or cancelled request is insufficient.
6. An uncertain POST is inspected using its original command identity; never
   resend effects automatically. Uncertain outcomes retain the machine. A new
   process may reconcile original work through the recovery protocol, not adopt
   an expired queue lease as authority for replay. Recovery needs an active
   `PilotOwner` and validated prior-epoch chain, not just a saved epoch string.
7. Explicit private enrollment still supplies build provenance, runtime identity,
   healthy baseline, test secrets and constrained transport. A new bridge cannot
   fabricate these from labels, expected commit strings or agent declarations.

## Minimum integration evidence

Use the real queue, repository, workflow and wire client with an isolated ASGI
host fixture and explicitly synthetic host proofs. Test creation crashes on both
sides of run mapping, duplicate attempts, wrong manifest/machine/epoch, stale
review/fence, two competing machine jobs, unknown POST without resend, healthy
rollback, held outcome and restart inspection of the original run. Such tests
prove coordination and protocol behavior, not Docker or remote-host acceptance.

Current integration tests use actual Linux `PilotOwner` locks, the real repository,
queue and workflow, and typed simulated host evidence. The wire test additionally
uses `ContainerPilotClient` through `PilotASGI`: a lost successful smoke response
holds the job, then GET-only recovery reads the original command identities and
releases it without another POST. Public-API tests deny direct dispatch/recovery
and private-worker impersonation. This does not authenticate simulated host
assertions as real image, deployment or health evidence.

Recovery deliberately remains conservative. A creation-only interruption with no
accepted advance remains held, even if the original local run request can be
mapped. No replacement run is created. A prior owner's still-unexpired claim may
be inspected but is not adopted or released until eligible for authoritative
queue reconciliation. There is no automatic retry, lease timer or recovery worker
in this module. Unknown or revoked authorization keeps physical capacity held.

## Deployment transport remains under review

Claude's linked proposal `656d40a9-5860-40b6-828e-8b26abdcd65b` recommends an
outbound staging-PC worker instead of the current pilot client's inbound host
endpoint. This could keep private host transport credentials off the Hub, but
requires source that is not currently present: remote executor enrollment,
pre-effect live-claim API, scoped owned-claim retrieval and authenticated receipt
verification/reconciliation. Proposed signing keys and new credentials are not
approved or installed. A signature authenticates an enrolled issuer; by itself
it does not prove effective isolation or observed application health.

The implemented bridge demonstrates one physical queue reservation with a
subordinate original-command journal/fence; the latter is not an independent
capacity scheduler. This source integration does not settle the deployed
transport or authorize placing private executor credentials in the browser/API
process. Linked follow-up `54c49733-1798-4c0e-bd12-4d9f88e44f36` asks for a concrete
conflicting interleaving and the minimal pull alternative, including credential
isolation. Resolve this before installing a lifecycle factory or host service.

Then, only after separately approved activation, verify the existing staging
host's effective transport, writer exclusion and resource limits, immutable
image, health/rollback and restart recovery. TrueNAS deployment remains Beau's
operation; no agent NAS management or production change is implied.
