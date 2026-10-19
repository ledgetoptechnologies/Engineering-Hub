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

### Protected Unix-socket lifecycle library

`private_browser_socket(path)` in `private_browser_socket.py` supplies an explicit
Linux-only context manager for a caller's existing server, not a service launcher.
It requires an already provisioned, canonical private directory owned by the
current UID with mode0700. Every ancestor must be root/current-UID owned and
not group/world writable, except a root-owned sticky directory such as `/tmp`.
Directory traversal uses no-follow descriptor-relative opens and identity checks.
The path is bounded to107 UTF-8 bytes, with a restricted leaf name. Every existing
leaf, including a stale socket or dangling symlink, is rejected without change.

Binding uses the pinned parent descriptor, creates a nonblocking noninheritable
AF_UNIX listener and sets the new socket to0600 before it is yielded. Cleanup
closes descriptors and removes only the unchanged original socket in the unchanged
namespace. Parent/leaf replacement, permission or ownership changes preserve the
unknown objects and report a sanitized failure. An inherited child never unlinks
the parent's listening endpoint. This does not defend against hostile root or
equally privileged same-UID mutations, and it does not configure container mounts.

`create_private_browser_client(path)` validates an existing protected socket and
returns a dedicated, caller-owned HTTPX client with exact UDS transport, retries0,
no environment proxy/auth, no redirect following and no TCP fallback. Construction
does not connect or adopt a database/owner. Close the client explicitly; stop the
server before leaving the socket context. Use a separate socket directory, not
the private database directory. Actual deployment must keep the public gateway's
mounts free of coordinator data and executor credentials.

The unified Linux tests perform browser signed-assertion login, shared task
creation, agent candidate/review/submission, pause/unpause and native-event queue
execution against one real `PilotOwner`. Both in-process and actual UVicorn/HTTPX
Unix transport cover success and healthy rollback with typed synthetic host
effects. Wrong source IP remains denied even with a forged forwarding header.
These are isolated container integration tests, not an actual Docker deployment,
installed NAS tunnel, independent host health observation or PC reboot acceptance.

Crash residue deliberately blocks a new bind. Never delete an old socket merely
because a PID, lock or lease looks stale; original owner quiescence and a separately
approved recovery procedure are still required. This library does not establish
unattended restart recovery or the machine-wide restored-database admission barrier.

### Explicit split listener runners

`split_server.py` adds two opt-in async library entry points, with no CLI,
environment activation, service registration or Compose changes:

- `serve_private_coordinator(pair_factory, socket_path=..., ...)` binds the
  original agent TCP socket and protected browser Unix socket before invoking
  the synchronous factory. Its exact new `HubFrontends` core owns one lifespan.
  The private browser receiver repeats closed-route and Access checks; the
  original agent listener preserves the real source address. No browser TCP
  socket is opened by this runner.
- `serve_public_browser(client_factory, ...)` binds only literal127.0.0.1 before
  accepting one dedicated HTTPX client from its synchronous factory. It creates
  the restricted public browser gateway, accepts no database/core/agent settings,
  and closes that accepted client after listener requests actually drain. Use
  `create_private_browser_client` for the private Unix transport, not a generic
  environment-configured client.

These APIs do not prove separate process or filesystem namespaces merely because
they run separately as tasks in an integration test. The deployment must give
the public process only its socket connection, without private DB or executor
credential mounts. Factories remain responsible for resources they construct
but do not return as valid accepted capabilities. Caller-owned pilot capability
and client lifetimes still extend beyond private core shutdown.

Shutdown synchronously invokes `HubFrontends.close_admission()` and the exact
queued runtime's `close_admission()` before the first listener-drain await. These
are sticky vetoes, not ownership release or effect cancellation. Already accepted
HTTP mutations and remote phases drain while the owner and feed remain alive.
The core lifespan exits only after listeners drain. The new private runner denies
legacy dispatcher/owned-service combinations lacking this admission contract;
the existing combined runner is unchanged.

The new listeners explicitly disable Uvicorn's forced graceful-shutdown timeout.
Only known read-only GET event subscriptions, including MCP GET streams, receive
a shutdown disconnect; ordinary GETs and every POST keep their original receive
and must finish. Actual pinned h11/ASGI receive-driven behavior is covered by
real global SSE stop tests. A dependency or protocol change needs revalidation;
this is not permission to cancel an accepted tool mutation. The operator's actual
supervisor must also permit terminal drain rather than kill the process on an
unverified deadline. There is no replacement worker or stale-socket adoption.

### Opt-in recovery of a published endpoint generation

`recoverable_browser_socket(path)` is a separate Linux source API. The split
private runner selects it only with the exact boolean `recover_socket=True`;
the default remains the strict helper that rejects every existing endpoint.
No environment setting, migration, configuration change or installed worker is
enabled by adding this API.

The protocol permanently retains `<leaf>.endpoint.lock`, a private zero-byte
regular file whose inode is never intentionally unlinked or recreated. An
exclusive nonblocking kernel flock is held through listener/core drain and final
socket close. A bounded, canonical `<leaf>.endpoint.json` record binds version1,
a fresh generation UUID, stable parent identity, exact lock identity and socket
identity, including socket/lock ctime. The record is written to an exclusive
temporary file, fsynced, atomically replaced and parent-fsynced before listen.
The private parent, ancestor and path requirements remain unchanged.

After acquiring that endpoint lock, recovery removes an existing socket only if
its exact identity matches the fully validated published record and the parent
and lock still match. Unknown legacy objects, missing/changed lock, malformed or
missing record, changed identity/mode/ownership and non-socket replacements are
preserved and denied. A normal close removes only the original socket and keeps
the lock/record, allowing a new generation under the same lock. No PID age or
lease timeout is used as evidence.

Fork callbacks close inherited listener, lock and parent descriptors without
unlocking or unlinking parent state; references are cleared so later cleanup
cannot close a reused descriptor. The protocol forbids arbitrary duplication or
transfer of those descriptors. It does not isolate hostile root or equally
privileged same-UID code. A database flock alone remains insufficient endpoint
ownership proof and is not substituted here.

Crash recovery is deliberately bounded: a crash after durable publication can
rebind that exact stale generation, including a crash before listen. A crash
between bind and publication leaves unknown residue and stays closed. Actual
power-loss storage semantics, installed supervisor restart behavior and a PC
reboot are separate acceptance requirements. Keep endpoint storage private and
durable if this contract is selected; an ephemeral namespace reset is not this
record-based recovery proof.

Endpoint recovery does not adopt a database owner, replay a message/tool/POST,
renew an expired claim, release a machine reservation or reconcile an uncertain
host effect. Existing workflow recovery must still inspect original command
identities with its independent evidence and authorization. The public transport
continues to make at most one upstream attempt per submitted request.

### Separate-container verification scope

The next local acceptance fixture uses two disposable, network-disabled
containers from an explicitly pinned, already-local validation image. Their only
shared storage is a dedicated ephemeral Unix-socket volume, writable in the
private container and read-only in the browser container. The private
container keeps its database, randomly generated synthetic agent credentials and
synthetic executor canary on private-only temporary storage. The browser
container receives only the public assertion and opaque task/job identifiers
needed for its test requests; agent API requests run inside the private fixture.
No host directory, Docker socket, published port or production credential belongs
in this test.

Real signed test assertions, session/CSRF handling, independent agent review and
the native queue should cross that boundary. Executor results remain deliberately
synthetic, not actual Docker application deployment or independently observed
health. Container profiles and inaccessible private file paths are checked as
runtime evidence, not inferred from command-line intent alone.

The browser fixture must additionally observe EROFS when attempting to create a
file in the socket directory while retaining a working Unix connection. Its
read-only mount prevents replacement through that mount; this is not a general
claim of hostile-browser resilience or cryptographic private-peer authentication.
Other privileged writers could still replace the private endpoint. This test
also does not prove installed TrueNAS network policy, Cloudflare
login, physical machine scheduling, power-loss durability or reboot recovery.
Do not activate a host service or broaden access on the strength of this fixture.
