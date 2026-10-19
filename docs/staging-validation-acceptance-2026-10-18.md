# Software staging checkpoint — 2026-10-18

## First-use readiness audit

Current inspected development source: `0a40d88`. The original goal's GitHub
human sign-in wording was superseded by Beau's separate Hub Cloudflare Access
choice. Do not add a second GitHub sign-in flow to satisfy the stale wording.
No current live installation or authenticated browser session was inspected in
this audit; earlier source/test evidence is not fresh deployment evidence.

| Requirement | Current evidence | Remaining first-use gate |
| --- | --- | --- |
| Shared task, review and decision records | Repository/API/dashboard source and earlier isolated HTTP tests | Verify installed release and authorized human UI against the intended database |
| Browser-only tunnel, LAN agent interface | `server.py` binds browser loopback and separate agent socket; split tests recorded below | Beau-owned NAS/tunnel deployment and actual allowed/denied browser checks |
| Software queue and production-first priority | `work_queue.py` and reviewed-target policy in `work_queue_config.py` | Default admission is deliberately closed; a queue flag alone installs no executor or production authority |
| Remote container execution | `ContainerPilotClient`, `OwnedPilotService`, `PilotASGI` and `HostAdapter` exist as explicit injected APIs | Concrete private host composition, protected build/baseline enrollment, trusted driver identity, installation and effective network/credential boundary |
| Local validation executor | `OwnedValidationService` composes a local `WorkQueue`, private receipts and native feed | It is not a remote queue client; do not mount Hub SQLite over SMB to make it remote |
| Health, rollback and recovery | Frozen state, observer draft, fixed probes, held outcomes and recovery source tests | Real isolated daemon/image/profile/health/deploy/rollback and installed restart acceptance; drafts are not approved enrollments |
| Agent topic availability | Registry client/bridge and evidence-aware preflight source | Canonical registry injection and actual participant session/runtime evidence; transport subscription is not an awake agent |
| Preserve existing MCP data and compatibility | Old 8765 service left untouched; offline archive validator integrated with frozen legacy fixtures | Operator-produced coherent read-only bundle, actual validation and separately authorized retrieval/copy; no replay of archived messages |

The next real pilot requires an approved isolated Linux executor boundary.
Existing shared staging Viewer/PA containers are not a disposable test boundary.
Read-only local inventory found only Docker Desktop's internal WSL distribution,
not an existing general-purpose Linux operator environment. No internal WSL
administration, socket mount, VM provisioning or shared-host service change was
performed to bypass this prerequisite. Beau chose continued source-only work
and clarified that the design must use the existing equipment. No additional
PC or dedicated VM is a prerequisite. Future activation should assess an
appropriately constrained test setup on the existing staging PC, separate from
production data and processing workloads; a Compose project name alone is not
a security or physical-resource reservation boundary. No host provisioning,
private credential/context change or live service activation follows from this
hardware clarification. Existing local Docker remains suitable for bounded
synthetic tests. Host selection is not evidence that an executor exists.

## Latest verified source

### Queue-to-remote-pilot source integration

Implementation `33e0206c84a28c598eefbb53bdac6b05094b6220`; final tested source
`12f15aef8dbf414626968b857422b93c5e86401b`, development branch only. Clean Git
archive image index
`sha256:36d56fb30aee076bf112f9e858c83d4c84739505451427023caa41626dc36912`,
manifest `sha256:39380532be7fc788e5343eacb90fd3d0e42b29beb10aad2315bb56cad98e6600`,
config `sha256:50e7e3afd3beb69fe4d4bc7e1e6702a8b777d38340652c988dccb8fad1dbaa72`.
Full exact-image Linux suite: **3159 passed, 3 skipped, 4 known warnings**, 150.39s.
Focused bridge/wire/API/queue/recovery/capability/topic checks: **173 passed**,
8.06s with ResourceWarning errors and one existing Starlette warning. Three
ownership checks separately passed **3/3**, 0.31s on the same image.

Independent review accepted the explicit `QueuedContainerPilot` API, persistent
late-object/repository guards, disabled direct routes and per-phase admission.
Twenty-five new Linux-owner integration cases plus two wire/API cases verify
original intent/request/run/fence bindings, held machine exclusion through
restart, healthy rollback mapped to queue failure, policy revocation, private
worker admission, malformed proof chains and no effect replay. One wire case
uses the real client and `PilotASGI` with deliberately synthetic host assertions:
it loses an accepted smoke response, then GET-only recovery completes the same
run without another POST. These assertions are not real Engine/health evidence.

The first clean implementation run reported **2 failed, 3157 passed, 3 skipped**,
157.55s. Both failures were pre-existing topic tests whose module-collection
timestamp expired under the real 120-second evidence TTL before execution.
The test-only correction creates evidence when requested and compares one stable
snapshot for REST/MCP parity. The explicit one-day-old evidence test remains;
production freshness logic was not changed. Review accepted that narrow fix.

No installed lifecycle factory/native worker loop, host credential, baseline,
runtime identity issuer, NAS operation, actual remote pilot or reboot acceptance
is added. Default application queue/pilot exclusion stays enforced. The deployment
transport is still being discussed: Claude proposes an outbound worker, requiring
new source APIs/receipt enrollment, while this bridge reuses the existing typed
host endpoint. Private credential isolation and concrete single-reservation
failure cases must be resolved before activation. See
`remote-software-staging-bridge.md`; no new hardware or mandatory VM is required.

### Remote executor request concurrency

Implementation checkpoint: `e2997979529bb8db1182f1fbfe1f3e3328d210b6`, development
branch only. A clean Git archive built image index
`sha256:ab17ca58417b3ab097d29cf764e31cae7b3b4f44832bfde620a3965ed20767d7`,
manifest `sha256:41dd9f262aaec101bcc246624d6015b811cd96a9b2fb7a1ecf4fb89ef6dae1e7`,
config `sha256:82830924854acc2f09038a27a3483f099040dbc08cd936e5b59f27962127f785`.
The full exact-image Linux suite passed **3132 tests, 3 skipped, 4 known warnings**
in 148.82s. Focused ASGI/client/adapter/daemon tests passed **210/210**, 1.58s,
with ResourceWarning errors; three ownership checks separately passed **3/3**,
0.31s, on the same image. The earlier working-tree run passed 3132/3 skipped in
147.64s; the clean archive result is the authoritative committed-source result.
An initial ownership-check invocation omitted the test-data tmpfs and failed
during collection; the corrected isolated invocation passed. No test was
silently counted from that failed run.

Independent review accepted the bounded adapter offload. Eleven new real-thread
tests cover submit/get/reconcile responsiveness, busy admission without waiters,
body deadlines while work is active, repeated cancellation drain, context
propagation, scheduling failure, exception release, PID and cross-loop behavior.
The synchronous adapter no longer blocks the ASGI loop, but a cancelled client
does not stop accepted host work or release its gate early. Busy inspection is
unavailable rather than queued behind the same private ledger. The wire client
still treats a non-success response as unknown and never automatically resends.

This is isolated source/HTTPX/SQLite/thread evidence, not remote host or Docker
driver acceptance. No listener, private credential, Docker context, host mount,
NAS operation, old MCP endpoint/token/port, production deployment or reboot
configuration changed. The queue-to-pilot reservation bridge remains unimplemented;
see `remote-software-staging-bridge.md` for its concrete invariants and tests.
Current queue/pilot exclusion remains in force. Existing equipment and continued
source-only work supersede any requirement to provision a new PC or mandatory VM.

### Earlier archive HTTP checkpoint

Opt-in archive HTTP checkpoint: `2ff8f6d`. Clean Git archive image index
`sha256:804574caae4e73a3ee6902d16c570f587ad959b415aa5c181525409164e2c370`,
manifest `sha256:5fda5fa8d5a9b1106b7fa92aa444a2825ff71ef6496b05d52e5f6e3ec4484642`,
config `sha256:f4ba03c77fb6f9498ef2cbd25a2717949a85f20b407da2bf28b015ca2e08c682`.
Full exact-image Linux suite: **3073 passed, 3 skipped, 4 known warnings**,
145.33s; the three owner-change checks separately passed **3/3**, 0.31s.
Focused archive/auth/ingress/default-contract tests passed **261/261**, 6.51s,
with ResourceWarning errors and one existing Starlette warning. Independent
review accepted the HTTP projection and explicit factory boundary. Default
startup still adds no archive routes or schemas; no environment variable adopts
a bundle. Existing authentication supplies principals; selectors, duplicate
bearers, write verbs and private response fields are denied. Caller retains
reader shutdown ownership. This is ASGI/SQLite fixture evidence, not live data,
NAS setup, production sign-in or installed archive retrieval acceptance.

### Earlier archive reader checkpoint

Archive reader checkpoint: `fb2694c`, following reviewed archive integration
and synthetic kernel-mount acceptance. Clean Git archive image index
`sha256:c444022da7d69346a20e8fc329fd47ac6d900006ad8cf1c03a7108614c78c0a7`,
manifest `sha256:83f36dc9281d4ede8d8cbde759c006dfe99b36c99af373c580d50fe4e33da51e`,
config `sha256:16050420978709e98c910f6785aa78e002a408392c9c37e5c1eaca46002f1577`.
Full exact-image Linux suite: **3037 passed, 3 skipped, 4 known warnings**,
144.50s. The three owner-change checks separately passed **3/3**, 0.31s.
The source-only reader has 46 focused passing tests with ResourceWarning errors.
Independent review accepted its sender/recipient ACL and immutable read boundary.
Separate synthetic kernel checks verified read-only access, denied writable
mounts, and preserved private-message visibility (see `hub_archive/README.md`).
This adds no installed REST/MCP/UI route, actual archive adoption or replay;
actual coherent export, authenticated read-surface integration and original
data acceptance remain required. The full staging pilot gates above also remain.

### Earlier baseline observer checkpoint

Baseline observer checkpoint:
`5fa2e8494ad6f4c7fc9bb40319105d8650013892`, development branch only.
A clean Git archive produced image index
`sha256:259c0153e8d5074b1253185e1dd8efa84267ffde781725cea39b664fd5de7fec`,
manifest `sha256:1c441cfbfa0dcce7d7a863bbc6311697a270214410a3b5c4514d83555a425104`,
config `sha256:f925239eb1737188c2e3d42cb0ddd43399665e112679b1befb386faefb4ab81b`.
Full exact-image Linux suite: **2961 passed, 3 skipped, 4 known warnings**,
138.33s. The three owner-change checks separately passed **3/3**, 0.28s,
on the same image. Focused observer/snapshot/receipt/profile tests passed
**100/100**, 47.65s, with ResourceWarning errors enabled in the working image.

Independent source review accepted the one-attempt observer after mock-transport
and frozen-inode descriptor safeguards were added. Observation checks runner,
image, profile, unchanged original inventory, frozen SQLite integrity and exact
terminal private probe receipts before returning draft bytes. Tests use actual
private SQLite journals and separate Linux processes with a quiet WAL writer,
but **simulate Engine methods**. They do not establish real daemon/container
health. Receipt publication, independent operator pinning, trusted driver-runtime
identity and actual deployment/rollback/reboot acceptance remain incomplete.
No live host, NAS, endpoint, credential or installed worker changed.

## Earlier baseline preparation checkpoint

Baseline preparation checkpoint:
`372f38b96a81970188c770869afb02aa2a18ff38`, pushed to the development branch,
not main or an installed image. Clean Git archive image index
`sha256:84de0f6abaa98e85859af7485fc4e624eadc8c3c6152062e50ac6d1b483eb02a`,
manifest `sha256:5d35c83aafa0a16631760bd98081cfd8c66f357278028ab2783fffd4486a328f`,
config `sha256:2a98b268773e477d3e21f8e6200eb210fb19edbaf84bfde940e272b3af2306b2`.
Full exact-image Linux verification: **2936 passed, 3 skipped, 4 known warnings**,
102.63s. The three owner-change checks passed separately **3/3**, 0.29s.

This adds a protected historical baseline receipt reader, bound file inventory,
shared pure runtime-profile derivation and an explicit first-baseline snapshot
helper. Actual Linux SQLite/WAL snapshot tests use a separate operator process
while the fixture writer remains running; database/WAL/SHM bytes and identity
remain unchanged. The helper rejects an already-open source descriptor or a
multithreaded operator and never normalizes the live source. An initial test
measurement itself released the writer's POSIX locks; moving that measurement
into another process fixed the test without relaxing production guards.

Receipt-reader integration exercises real private storage and candidate/rollback
state copies with **synthetic health authority**. It is not actual application
health, observed baseline enrollment, driver-runtime identity, real container
deployment/rollback or reboot acceptance. The actual health enroller and
independent pinning workflow are still missing; the acceptance gate stays
disabled. No live host, NAS, MCP endpoint, token or production state changed.

## Earlier private executor lifecycle checkpoint

Private lifecycle implementation checkpoint:
`dc9d1dffd71a4cc658aa56d242613aaa44fe500b`, pushed to
`work/hub-pilot-integration-codex`, not main or an installed image.
A clean Git archive produced image index
`sha256:28ce50baeb55f71be8b594cd2b7e1c19cb799f0bdcab7508de48b121852146bb`,
manifest `sha256:e7690bdeba9715375de84c46f134d0e8e7faf90260e2372becd1bba56ea5d265`,
config `sha256:5d0c4ac47f5b0c85739ea538cd22ef2a0ed8b5cd39017b76e272528e01db5cfa`.
The exact image passed **2821 tests, 3 skipped, 4 known warnings**, 90.31s.
The three owner-change checks passed separately **3/3**, 0.29s, with only CHOWN.
The final focused identity, authentication, ownership, runtime and composition
set passed **92/92**, 2.06s, with ResourceWarning errors enabled. Independent
source review found no blocker.

This adds actual Linux lock/native-feed lifecycle verification with a **mocked
execution backend**, not an installed remote executor or a new real Docker pilot.
It proves startup draining, event-driven invalidation, accepted-work shutdown,
fresh restart identity, exact private admission and agent-token impersonation
denial. No entrypoint, credential or worker service was activated. Existing
shared staging Viewer/PA resources remain outside the allowed pilot boundary;
see the shared-host activation gate in `staging-validation-worker.md`.

An earlier working-tree image passed 2860 tests; that count includes unrelated
untracked tests and is not the clean-commit release evidence above. The exact
clean commit is the authoritative checkpoint.

## Earlier queue and actual local Engine checkpoint

Final implementation checkpoint:
`2132e8727e9eeb3688a59075ddd3eb11f20ea282` on the same development branch.
A fresh clean Git archive was built as image index
`sha256:a4bfe1acfcc881ffe45be3357a73f3c18fc1e122a7eda3f86a2295e18bc1d6aa`,
selected manifest
`sha256:73c6ff2e63858f887af18fea834f2560dbb2bf87a549aa0fb7122ba312543c8c`,
config `sha256:79bd865ef8e3da2eb248a5b4990d03354ff4b6e5c9efa7f80e60b4244253bf16`.

The exact final image passed **2701 Linux tests**, with 3 owner-change skips
separately verified **3/3** (0.31s), and 3 known warnings, in 93.12s. The full
suite includes opt-in MCP transport, closed bootstrap admission, exact worker/
target authorization and action-filter tests. The actual-HTTP/native-SSE smoke
also passed against this image. The actual local Engine acceptance was repeated
successfully against this exact image with explicit test-only worker admission:
fixed tests succeeded, the separate exit-7 command failed correctly, forged
evidence was denied, receipts reopened, reservations released, and only the two
receipt-bound stopped containers were removed. Production remained blocked.

The older a377 checkpoint below preserves the initial acceptance/fix history;
it is not the final source. All remote, deployment, credential and reboot gaps
below remain. Existing approved SSH access to staging `192.168.50.90` was
verified read-only, including Docker availability; no host configuration or
service was changed. Staging root reported 19 GiB free of 98 GiB, so image/scratch
retention must be bounded before any installed worker is activated.

Beau selected software workflows first. Bulk ODM/OpenSplat/TreeDAR processing
workers remain separate future work. This is **not** a report that the remote
staging installation is ready.

## Exact source/artifact verification

Reviewed source checkpoint: `a377e26e68140bf9abc5dd598914721bd535e3ba`, on
`work/hub-pilot-integration-codex`, not main. A clean `git archive` of that commit
was used as the complete Docker build context, excluding unrelated untracked
work and local datasets. No new production image was published or installed.

- Local validation image index:
  `sha256:402c3aa41620d043232e46d197e10f2f79b39c912408b63beb26ccfe5420890b`.
- Selected linux/amd64 manifest:
  `sha256:4feb828b8d7ee53232dffd5829742a47eb89a9f253422b105fac65d0a8cbdf8d`.
- Image config:
  `sha256:8d60fc69c9537cf92954d428dbf26bb7cd34aa2b0f3e3591e35520ae4f6aa914`.
- Full exact-image Linux suite: **2648 passed, 3 skipped, 3 warnings**, 93.48s.
  The skipped owner-change tests separately passed **3/3**, 0.31s, against the
  same image with only CHOWN and synthetic fixtures. Known warnings: Starlette,
  an unclosed asyncio test event loop, and deliberate multithreaded fork.
- Actual HTTP coordination smoke on that image passed real 401 handling,
  non-preemptive production priority with synthetic approval, machine exclusion,
  instance fencing, expiry hold and exact native SSE queue invalidation. This
  test's coordination-only actions did not execute anything.

## Actual local Engine validation

An explicit Windows driver connected to the existing local Docker Desktop Linux
Engine, using its pinned daemon identity and the preloaded exact image above.
The original local OCI archive bytes established the index/selected manifest/
config chain. Task reviews and operator enrollment were **synthetic fixtures**,
not a verified CI attestation or human production approval. No real token or
live Hub database was used. No host Docker socket was mounted into a container.

The first actual test invocation exited 4 because the test conftest creates a
data directory next to the read-only test tree. The backend correctly recorded a
verified failed receipt and released that job; it did not claim success. The
exact original stopped container was inspected and removed without force. Its
small synthetic intent/receipt databases were retained.

The driver was corrected to copy the fixed test tree into disposable `/tmp`
before invoking the fixed tests. Runtime isolation was not relaxed. The corrected
actual local acceptance passed:

- Fixed queue/API/bootstrap tests executed and exited zero.
- A separate fixed command exited 7 and produced a failed result, not success.
- Original image, effective sandbox profile, exit and quiescence were inspected.
- Trusted receipts survived reopening; a fabricated evidence ID was rejected.
- Both verified terminal jobs released their machine reservations.
- Unapproved production stayed queued; no deployment run or task promotion.
- Only the two exact receipt-bound, independently verified stopped containers
  were removed without force. A final local listing found none remaining.

The worker used **Windows test-only receipt storage**. Linux permissions were
tested separately in the exact-image suite; the Windows run does not establish
Linux private storage, remote staging, trusted launcher identity, reboot recovery,
application deployment/rollback or production execution acceptance.

## Remaining first-use gap

The new HTTP queue and local validation worker are not yet an authenticated
Hub-to-staging-PC execution service. Do not place the Hub database on SMB or let
agent assertions create trusted receipts. The remote adapter, its bounded
execution/receipt contract, private provisioning and installed lifecycle still
need implementation and actual-host acceptance. The default production verifier
and executable completion/reconciliation provider remain disabled.

After peer review, the configured bootstrap was further hardened to deny claims
entirely until a trusted server-side exact-worker/action/machine/target policy
is installed. Merely enabling the queue cannot wedge the machine by reserving
an unexecutable job. Optional action filters only narrow that policy. The local
acceptance harness explicitly enrolls its precise synthetic target and bound
receipt provider; this is not remote executor credential provisioning.

The staged agent API base remains `http://192.168.50.80:8770/api/hub/v1/work`,
and the Hub MCP listener is `http://192.168.50.80:8770/mcp/`; these new queue
capabilities are **candidate source**, not live installed endpoints. The old MCP
on 8765 is unchanged. Beau retains TrueNAS management/installation ownership.
