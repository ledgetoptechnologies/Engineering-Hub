# Software staging checkpoint — 2026-10-18

## First-use readiness audit

Initial readiness-audit source: `0a40d88`; newer verified source checkpoints
are recorded below. The original goal's GitHub
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

### Split listener lifecycle and immediate admission veto

Tested source `2ec018ae87a1a639a981a0d408f99643755854f5`, development branch
only. Clean Git archive image index
`sha256:054e7dc5d4ca1d884f2847d190fae0ad7175327b5b4c0144129be2727a7b36a3`,
manifest `sha256:c4eae379e56b9e79b6d02fb8dc90e486f50c63e2e48c43aecb3ec054f651411f`,
config `sha256:898db08c828eb25d716bd549f982dd576dde06ce29de8bc0658a00c9d819e491`.
Full exact-image Linux suite: **3286 passed, 6 skipped, 4 known warnings**,
152.42s. Focused split listeners, queued pilot, runtime, frontends, gateway and
contract checks: **104 passed**, 7.20s, ResourceWarning errors enabled and no
warnings. All six ownership cases separately passed **6/6**, 0.59s, on that image
with only CHOWN and synthetic fixture storage. Full warnings were existing
Starlette, an unclosed test event loop and two deliberate fork deprecations.

Two explicit source runners bind before factories: private agent TCP plus browser
UDS over one exact private core, and a separate loopback-only public gateway
accepting only its dedicated HTTPX client. Listener failure/callback failure or
repeated caller cancellation drains accepted requests before client/socket/core
release. Exact read-only event subscriptions receive a graceful disconnect;
ordinary requests and every POST are not force-cancelled by a shutdown timeout.
Actual pinned h11/ASGI stream behavior, not a generic future protocol promise,
is tested with open global SSE streams on both frontends.

Review found that listener drain alone would leave queue admission open while a
slow request finished. Explicit synchronous HTTP/runtime admission vetoes now run
before the first drain await. A real Linux owner/native-queue regression holds
one accepted phase and one accepted HTTP request, stops/cancels repeatedly, and
verifies the second job stays queued while feed/owner/socket remain alive. Only
the accepted phase finishes; its uncertain unfinished chain remains held.
Two additional full HTTP/UDS runner cases cover success and healthy rollback
using signed synthetic Access assertions and typed simulated remote effects.
Independent review accepted the wiring; the original combined runner is unchanged.

Initial working focus: **1 failed, 59 passed**, 7.00s. A new test sent an agent
bearer to human-only task creation and held its403 response, so it was not actually
testing an accepted mutation. The fixture now uses permitted agent decision
creation, asserts201 and persistence before blocking, then tests shutdown. No
authentication rule was relaxed to fix that test.

Claude reply `00bcac3e-8c6b-4717-82b8-b5a27834d880` correctly identifies that
stale socket residue prevents unattended restart. His proposed database flock
alone is not sufficient endpoint ownership proof: `PilotOwner` closes inherited
locks after fork, while a child can retain an inherited listener descriptor.
UID/mode/type are not a generation binding. Endpoint-specific lifecycle ownership,
durable exact identity metadata, inherited-descriptor handling and crash tests are
next source work, not implemented recovery. Per-container tmpfs alone does not
prove shared-socket reachability, namespace reset or reconnect. Unknown residue
still fails closed; no blind unlink or lease-expiry reassignment was added.

These tests do not establish separate container mounts/process credentials,
actual immutable Docker deployment/observed health, installed TrueNAS/tunnel,
independent enrollment, or app/PC reboot acceptance. No live host, MCP endpoint,
token, port, access policy, service or production deployment was changed.

### Unified signed-auth workflow over protected Unix transport

Tested source `b17cd728142d31e9aa0d65cba10b03c43d67ba88`, development branch
only. Clean Git archive image index
`sha256:8e5dd34fe20aad4d903637a9255b11057806304edfbf4aa690d934c0a62169cd`,
manifest `sha256:79735be2064d80f795a1577dcf89b286dfaa5aa7389a3286b8aaedb69a24711b`,
config `sha256:f9b29d04e0b95905efc9ac1e95403df81ae92b444c0cdf18b4e8ca95110fa0f8`.
Full exact-image Linux suite: **3250 passed, 6 skipped, 4 known warnings**,
152.81s. Focused socket, gateway, app, runtime, contract and unified-flow checks:
**90 passed, 3 skipped**, 4.52s with ResourceWarning errors and no warnings.
All six owner-change cases separately passed **6/6**, 0.59s, against that same
image with only CHOWN and synthetic fixture storage. Full warnings were the
existing Starlette deprecation, unclosed test event loop and two deliberate
multithreaded-fork deprecations. Independent source review accepted the changes.

Four unified cases combine real Linux `PilotOwner`, one private core lifespan,
signed synthetic Access assertions, browser sessions/tasks/decisions, original
source-IP agent authentication, independent candidate review, reviewed queue
submission, pause/unpause and native-event execution. Both in-process ASGI and
actual UVicorn/HTTPX Unix transport test success and healthy rollback. Remote
effect/health assertions remain deliberately synthetic; these are not real Docker
deployments or independent observed application health. Browser execution and
actor/executor spoofing, self-review, and a wrong IP with forged forwarding
headers remain denied. No second database writer or scheduler was introduced.

The explicit Linux socket helper requires a provisioned current-UID0700 directory,
safe root/current-UID ancestors and a new leaf. It creates a nonblocking0600 socket,
never adopts stale paths, and preserves replaced/unknown parent or leaf objects.
Tests cover permissions, symlinks, namespace replacement, child cleanup, actual
HTTP transport and foreign UID checks. Review identified that final-parent0700
alone would not protect against renaming through a nonsticky writable ancestor;
the source now rejects that ancestor and has regression coverage. Synthetic
CHOWN fixtures restore their own original directory ownership in teardown;
the library does not repair ownership or permission errors.

Actual protected process/mount composition still needs acceptance. Root or an
equally privileged same-UID process is not isolated by path checks. Crash residue
intentionally prevents a new bind, so no unattended restart acceptance follows.
Real host writer exclusion, independent provenance/baseline/health, remote pilot,
machine-wide restored-database reconciliation and app/PC reboot gates remain.
The existing live MCP, NAS, tokens, ports and production deployments were untouched.

### Private browser gateway and queued-core application lifecycle

Implementation `c9742e20dc2677b47f548abc2e4bcd8a4a59b174`; final tested source
`848667a8a33acac88234515001236f43e4245e7e`, development branch only. Clean Git
archive image index
`sha256:411942044db7d24be131c42f086fde9a9d63eb7a0bcda08eb1ac3e7897879059`,
manifest `sha256:dc61521036f6d244c9156f65a826a6607601789508af92f540b45aed803033ad`,
config `sha256:adfeb197c2c6f69aa0f5627a0158556b0af458e3d4f9f1d5a191f1bdd4c1dd69`.
Full exact-image Linux suite: **3225 passed, 3 skipped, 4 known warnings**,
145.42s. Focused gateway, app, runtime, DTO, queue bootstrap, owned service and
contract checks: **113 passed**, 4.81s, ResourceWarning errors enabled, one
Starlette warning. Three ownership checks separately passed **3/3**, 0.30s,
on the same image with only CHOWN and synthetic fixtures. Full-suite warnings
were Starlette, an existing unclosed test event loop, and two deliberate fork
deprecations. Independent review accepted the source, tests and contract diff.

The explicit app parameter binds one existing queued runtime to its exact
repository, queue and native feed. Startup requires the native source; shutdown
drains accepted effects before feed closure despite repeated cancellation. The
caller retains the owner/client until the lifespan exits. No environment flag,
service installation or replacement scheduler activates this composition.

The browser-only proxy and private receiver expose closed UI/auth/task/decision/
read/SSE routes and reuse core Cloudflare, session and CSRF checks. They do not
proxy MCP, private worker operations, direct dispatch or agent work submission.
Agents keep their original LAN listener and source-IP checks. Request and ordinary
response bounds, cookie isolation, no transparent mutation retry, immediate SSE
and cancellation cleanup have regression coverage. The public process must have
neither the database nor executor credential; actual protected UDS/process
composition is still an operator acceptance gate, not proved by an injected
test client. No host or live configuration was changed.

The 43 gateway tests use the actual core/repository and signed synthetic JWTs;
the eight app cases separately use real Linux `PilotOwner`/native events with
simulated remote effects. At this earlier checkpoint there was not yet a unified
gateway-to-enrolled-owner test. The newer source above adds that test, but not
an installed private listener or real remote pilot/reboot proof.
Machine-wide restore reconciliation and other activation gates below remain.

Verification history: the initial working focus had **1 failed, 104 passed**;
the app correctly denied unsupported startup, but the test expected an unwrapped
HubError rather than MCP/AnyIO's ExceptionGroup. The test now checks the actual
wrapped cause; no product guard was relaxed. Gateway test wrappers were converted
from nested asyncio.run to pytest-managed async execution to avoid losing the
test runner's event-loop ownership. Initial clean implementation suite had
**1 failed, 3224 passed, 3 skipped**, 145.29s: the checked-in OpenAPI snapshot had
not been regenerated for the new queued-runtime DTO. The existing exporter
produced the exact additive 78-line schema change; the final clean suite above
includes that contract check. Windows' initial default pytest temp path was
inaccessible; fresh explicit temporary paths worked without host permission changes.

Peer evidence notices were bus-accepted for Claude
`29264b0e-d4d8-4742-ac2e-3b2285fc8853` and Hermes
`f89ce9c0-69aa-48b1-9fef-1e01a13e794b`. Processing/wake is not verified. No
receipt-only response, deployment, activation or infrastructure work was requested.

### Inert native-event queue runtime

Tested source `c05557fdd00f8120e4a8690203b22785ba501e48`, development branch
only. Clean Git archive image index
`sha256:6a6385ba2e33ca562cd5541be15c608bdf6f3ea8d40d375df514cef155def298`,
manifest `sha256:913a965f58354d43d1886aa1b362530981aa627282fa94f1bd64628b2f76c2b1`,
config `sha256:5b27da3df3150a5ba597d69d4eedb655979b250dc0b0895b5375dd7460d0b0f9`.
Full exact-image Linux suite: **3174 passed, 3 skipped, 4 warnings**, 149.53s.
Warnings were the existing Starlette deprecation, an unclosed test event loop,
and two deliberate multithreaded-fork deprecations. Focused runtime, restore,
bridge, wire, queue and host-ledger checks: **98 passed**, 6.42s, with
ResourceWarning errors. Three ownership checks separately passed **3/3**,
0.30s, on the same image using only CHOWN and synthetic fixtures.

`QueuedPilotRuntime` is an explicitly started library, not an installed worker.
It subscribes before the initial drain, coalesces native events and runs one
eligible job at a time without a timer or inbox polling. Stop, source failure
and manager cancellation immediately veto new effect admission while accepted
work drains. Independent review accepted the runtime and bridge hooks. Thirteen
runtime tests cover lifecycle and cancellation; two additional tests reopen a
real independent host ledger with synthetic effects to check target-fence
regression and busy-target denial. They do not establish real host acceptance.

The initial working-tree focused run had **1 failed, 97 passed**: the idle test
read SQLite while measuring native callbacks and did not await quiescence.
The test now observes actual bridge drain calls and waits for native/runtime
quiescence; production code was not relaxed to resolve the test synchronization.

Claude accepted the single physical-machine reservation invariant and withdrew
the requirement to replace this bridge with an outbound pull worker. The next
source boundary is a restricted public gateway to the private database owner:
ordinary dashboard writes must not bypass `PilotOwner`, and the public process
must not receive the executor credential. This is not implemented or activated.
Machine-wide restore admission also remains closed: retained per-target host
fences alone do not prove a restored Hub can safely release physical capacity.
Independent control-generation reconciliation is still required before such
recovery can be accepted. No lifecycle factory, live service, credential, NAS
change, deployment or reboot acceptance is implied by this checkpoint.

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

At this earlier checkpoint, no installed lifecycle factory/native worker loop, host credential, baseline,
runtime identity issuer, NAS operation, actual remote pilot or reboot acceptance
is added. Default application queue/pilot exclusion stays enforced. The deployment
transport was still being discussed: Claude proposed an outbound worker, requiring
new source APIs/receipt enrollment, while this bridge reuses the existing typed
host endpoint. Private credential isolation and concrete single-reservation
failure cases must be resolved before activation. The later runtime checkpoint
above records Claude's revised position and the remaining isolation gates. See
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
