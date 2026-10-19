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
A separately reviewed lifecycle factory is still needed to connect this source
to installed autonomous scheduling without re-enabling direct dispatch. No service
composition, transport credentials, host writer exclusion, baseline enrollment
or runtime identity issuer is supplied here.

## Explicit native-event runtime

`QueuedPilotRuntime(bridge, feed)` in `queued_pilot_runtime.py` binds one exact
bridge to the same-database `NativeHubEventFeed`. Construction starts nothing.
The caller owns and starts the native feed, then explicitly awaits `start()`.
The runtime registers its subscription before its initial durable queue drain,
processes one eligible new job at a time and coalesces subsequent invalidations.
There is no timer, inbox polling, model invocation or alternate reservation queue.

Once attached, the runtime is an additional veto on claims and every subsequent
create/advance/remote-command phase. Stop, source failure, wrong process/loop or a
cancelled manager close that veto immediately. Already accepted phases drain;
they are not force-cancelled, counted as successful because of cancellation, or
given permission for another phase. A fully verified terminal phase may still
record proof-based completion; otherwise the original reservation stays held.
Held work is never automatically replayed or inspected merely because unrelated
database activity arrives. `recover(job_id)` remains a separate explicit API.

Await `stop()` before stopping the feed, closing the private host client or
releasing `PilotOwner`. Repeated cancellation of stop waits for accepted work and
observer cleanup before propagating. The runtime does not close those resources
itself, detach its sticky veto, install hooks or provide reboot recovery. This
library does not add an app factory, environment enable flag or installed worker.

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
in this module or its native runtime. Unknown or revoked authorization keeps
physical capacity held.

## Peer transport review and remaining activation gates

Claude's linked proposal `656d40a9-5860-40b6-828e-8b26abdcd65b` recommends an
outbound staging-PC worker instead of the current pilot client's inbound host
endpoint. This could keep private host transport credentials off the Hub, but
requires source that is not currently present: remote executor enrollment,
pre-effect live-claim API, scoped owned-claim retrieval and authenticated receipt
verification/reconciliation. Proposed signing keys and new credentials are not
approved or installed. A signature authenticates an enrolled issuer; by itself
it does not prove effective isolation or observed application health.

In follow-up `36945d03-a641-4eaa-bb1c-45de59a99385`, Claude accepted the invariant
and withdrew the pull-required preference: reuse of the existing push bridge is
the shorter source path. The host's original-command journal/fence is subordinate
effect-replay protection, not an independent physical-capacity scheduler. This is
peer architecture review, not authority to provision an endpoint or credentials.

Three activation requirements remain explicit:

- Release only after verified terminal host-operation evidence. Healthy deployed
  application containers intentionally remain running; that differs from an
  unfinished create/stop/probe operation. No operator note, `cancelled` outcome or
  queue lease expiry substitutes for the original terminal evidence chain.
- A retained host ledger rejects lower/reused fences for a new run and higher
  fences while its target is busy. Tests retain the real private ledger across
  two disposable Hub histories to verify that narrow behavior. This is **not**
  machine-wide restore reconciliation: historical terminal receipts after a Hub
  restore do not prove that no newer host work exists. Restore must keep admission
  closed until independent current-host reconciliation. No persistent anti-restore
  anchor, generation-floor synchronization API or approved restore procedure is
  supplied here; do not automatically reset or bump host fences to resume work.
- Existing CI/manual deployers must be coordinated or excluded. Labels, a sidecar
  lock and a shared Docker daemon alone do not establish writer isolation.

Private adapter credentials must stay out of the browser process. `PilotOwner`
also prevents an ordinary separate public process from writing its enrolled DB.
The explicit browser gateway source below supplies a restricted HTTP seam, not
shared-file access or SMB SQLite. Credential cohabitation is not an accepted risk
on Beau's behalf. Actual protected transport and process composition must still
be verified before installing a factory or host service.

Then, only after separately approved activation, verify the existing staging
host's effective transport, writer exclusion and resource limits, immutable
image, health/rollback and restart recovery. TrueNAS deployment remains Beau's
operation; no agent NAS management or production change is implied.

## Explicit private core and browser gateway source

`create_app(..., repository=repo, work_queue=queue, event_feed=feed,
queued_pilot_runtime=runtime)` now accepts an exact `QueuedPilotRuntime` bound
to that same queue, repository and native feed. The caller supplies an already
approved bridge and retains ownership of its private client and `PilotOwner`.
No setting constructs or activates this runtime automatically. Legacy dispatcher
and workflow injection remain mutually exclusive with the queue; no second
reservation authority is introduced. Runtime reuse across app instances and a
foreign/already-started feed are rejected. An unsupported native source prevents
this explicit application's startup rather than silently enabling polling.

During the existing core lifespan, the feed starts before the queued runtime.
Shutdown closes runtime admission and drains accepted work before stopping the
feed, including repeated caller cancellation. The owner/client must remain alive
until that lifespan has actually exited; the caller closes them afterward. This
does not configure or verify a supervisor's stop grace. `/autonomy` and overview
project sanitized queued-runtime state, not host readiness or deployment proof.

`private_gateway.py` contains inert `PublicBrowserGateway` and
`PrivateBrowserGateway` APIs. The public process receives no Hub database,
agent-token configuration or executor credential. A caller-owned, dedicated
`httpx.AsyncClient` connects to a protected private Unix HTTP socket; the fixed
internal origin is `http://hub-private.invalid`. Its transport must have no
proxies or retries. These constructors do not create or verify that socket,
credentials, process isolation, filesystem permissions or installed lifecycle.

Only an explicit browser route table is exposed: Cloudflare session login/me/
logout, shared task and decision operations, pause and bounded reads, static UI
assets and native SSE streams. MCP, worker claims/completion/renewal, direct run
creation/dispatch/recovery and agent-only work submission are not proxied. Agents
continue on the private core's original LAN listener, retaining their original
source-IP and bearer checks. No forwarded client/actor header supplies authority.
This avoids changing allowlists to trust loopback or a proxy's address.

Original browser Host/Origin, JWT, cookie and CSRF headers reach the private
receiver, which repeats route admission and independently verifies Cloudflare
Access before existing core authentication/authorization. Client default auth
and cookies cannot fill in missing browser credentials. Duplicate Set-Cookie
headers are preserved. Requests and ordinary responses are bounded, ordinary
upstream attempts have deadlines, and SSE forwards small events immediately
without buffering the complete stream. Accepted mutations are never
automatically retried after lost responses; read the original request/revision
records before deciding on another action. HTTPX manual streaming requires
explicit response closure; the gateway drains that cleanup on cancellation.
See the [HTTPX async streaming contract](https://www.python-httpx.org/async/).

Future operator acceptance must prove that only the loopback tunnel frontend
is public-facing, the private socket directory/socket admit only intended
process identities, no public process has the DB or adapter credential, original
agent source-IP denial still works, and listeners close before owner release.
The source seam is not a deployment manifest or proof of those properties.
