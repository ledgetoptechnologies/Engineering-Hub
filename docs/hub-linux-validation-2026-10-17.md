# Isolated Linux validation — 2026-10-17

## Verified result and scope

Full normal Linux suite: **672 passed in 44.44 seconds**, with no ResourceWarning.
The existing Starlette/httpx deprecation warning remains; dependencies and warning
filters were not changed. Windows Hub-only suite: 410 passed, two platform skips.

The frozen Linux validation image was
`sha256:f1d2c5efb3f3423530f76fb9c1ff454885ebd667d5f6406c0e446ab9f221fe79`
(local tag `engineering-hub-validation:cli-loop-fixed`). It contains the working
tree overlay based on `9e08838`, plus the publisher sender-pin tests and the
test-only fixes below. It is not falsely labelled an exact earlier Git commit.
QA reported 101 copied source/test/script/contract files with aggregate SHA-256
`82ad662b12232601d82440f486ff3f9f20b4adaba92db5b1356bb7cd4c97c7c9`.

Root independently read the immutable image in a network-disabled, read-only
disposable container and compared SHA-256 hashes against the current worktree.
All eight selected changed/contract files were byte-identical: outbox source and
tests, presence/semantic/push-worker tests, Hub app/DTOs and OpenAPI. No token,
environment dump or private runtime database was read by that comparison.

This is software-test evidence, not TrueNAS/ZFS, host reboot, real GitHub OAuth,
canonical topic bridge, actual application deployment or staging-pilot acceptance.
Hermes's separate fixture executor is not included in this 672-test result.

## Root cause and minimal test-only fix

The unclosed loop was allocated by pytest-asyncio's cleanup-loop factory. Direct
`asyncio.run()` calls in synchronous tests replaced its main-thread policy slot
without closing that pre-existing pytest-owned loop. Later garbage collection
reported the warning in an unrelated presence/transport test.

Allocation traces plus an opt-in read-only policy observer established the lost
loop transitions. The repairs preserve assertions and production behavior:

- Presence's disabled-publisher test now awaits on the pytest-owned async loop.
- Semantic upload tests await their uploads on the pytest-owned loop.
- Seven push-worker scenario tests await their unchanged scenarios.
- The real CLI entrypoint test still invokes `main()`, but in a context-managed
  worker thread so the CLI owns a fresh loop context as it would in a new process.

The final focused allocation/ownership probe passed seven tests with no resource
warning and preserved pytest's cleanup loop across the real CLI entrypoint call.
No production bus source, native source, runtime listener, credential, dependency,
or live configuration was changed. No warning was suppressed or force-GC used.

## Reproduction environment

`docker/Dockerfile.hub-validation` is a separate test image, not the deployed bus
or Hub image. It installs locked test dependencies and copies required test inputs.
Its Dockerfile-specific ignore file admits only the tracked public `.env.example`
template while continuing to exclude real environment files and runtime data.
The normal default command uses `-W default` without global tracemalloc.

Run it only with no network, no host mounts/ports/credentials, a read-only root,
dropped capabilities, no-new-privileges, bounded resources and private tmpfs at
`/tmp` and `/validation/data`. The latter is necessary because a legacy test-module
import initializes its fixture data directory. All validation containers were
removed after their bounded tests; the local image remains available for inspection.

`tests/_hub_loop_diagnostics.py` is explicitly opt-in, not an auto-loaded plugin.
Its focused invocation is documented in its header. Do not enable global tracing
for the full suite: the streaming-memory test manages its own trace window, and
instrumentation distorts both memory peaks and short multiprocess deadlines.

Earlier instrumented runs with fixture omissions, timing failures or distorted
memory peaks were diagnostic failures, not acceptance results or demonstrated
production regressions. They were not counted as green gates.

For the integrated executor validation, also supply private writable tmpfs at
`/validation/tests/.test-data` (UID/GID 10001, mode 0700). The legacy conftest
sets its import-time bus database there. Generated `.test-data` directories are
excluded from new validation images. Without this tmpfs, a read-only image can
fail collection before any test runs; that is not a passing validation result.
The test-only `/tmp` mount must permit execution: two existing tests deliberately
launch tiny generated fake CLIs there. A first integrated run with `noexec` passed
714 tests and failed those two CLI cases; no assertion was relaxed. Permit `exec`
only for this bounded, network-disabled trusted-test scratch. Application and
deployment profiles are unchanged and keep their own restrictions.

## Current integrated executor result

The corrected bounded test invocation passed **716 tests in 55.72 seconds**,
with one existing Starlette deprecation and no ResourceWarning or skips. The
frozen image ID was
`sha256:08abdb2f5d0700c15a6ebc6073efce5304b284e4405a468350ca4bfbd9016861`.
It contains root `ce50f67` plus the selectively imported executor slice from
`53f9bb188cf92de227d730e0aad129dadf19064e`, test platform gates and packaging
changes. It is a frozen working-tree overlay, not an exact pre-existing commit.
Root independently verified all six executor Python runtime Git blobs match
that upstream commit exactly. Only its contract wording and Compose health
check were clarified; no Docker capability was added to fixture actions.

The run used no network, host mounts, ports or real credentials; read-only root,
dropped capabilities, no-new-privileges, 2 GiB memory, two CPUs and 256 PIDs.
Private tmpfs held `/tmp` (exec, 256 MiB), `/validation/data` and
`/validation/tests/.test-data` (both noexec, UID/GID10001, mode0700, 64 MiB).
The uniquely named test container was automatically removed after completion.

Windows Hub plus executor collection passed 410 tests, with eight explicit
platform skips: six Linux executor modules plus existing native/symlink skips.
The Linux run, not the Windows skips, establishes executor test coverage.
`uv build --quiet` also succeeded; the source archive includes the separate
executor source, contract and Docker harness. Its historical evidence JSON
remains labelled against the older exact peer-selected snapshots.

This integrated suite includes real loopback HTTP and actual SIGKILL recovery,
but does not start nested Docker or establish an actual application rollout.
The separate four-scenario Docker acceptance was peer-run on Hub `1b50667` plus
executor `da4cf84`; revalidation on the integrated revision remains distinct.

## Pilot controller source validation

After the separate pilot client/workflow/API slice and functional-review fixes,
the normal isolated Linux suite passed **858 tests in 56.87 seconds**, with only
the existing Starlette deprecation. Image ID:
`sha256:d692f0304cbfa318a86f16925b00ab87b2b92ab8b3c19b77edbe1fcd69f831d6`.
This frozen overlay is based on `aed6cb8`, with the new pilot controller/client,
142 added tests, separate API routes/capability, dispatcher support and regenerated
OpenAPI. It is not labelled an earlier exact Git commit. Isolation and private
tmpfs bounds match the integrated executor run above; no Docker socket is mounted.
The test container was removed after exit. Browser validation also passed eight
unit tests, TypeScript/Vite build and all 20 real Edge tests. Current TypeScript
fixtures and the published OpenAPI match the server response models.

Independent functional review found a dispatcher restart-path incompatibility:
the pilot workflow needed the original manifest-digest recovery method. It was
fixed and tested across local receipt/completion-record crash gaps, with exact
run/fence preservation, explicit quiescence and no repeated command submissions.
Typed adapter responses are retained durably as assertions; unknown outcomes
remain null. Migration preserves legacy unknown reservations without replay.
These new pilot tests use typed fakes/MockTransport, not actual application Docker
effects. The host adapter/full manifest/real app campaign remain incomplete.

Hermes separately ran the existing four real Docker FILE-fixture scenarios once
against exact `aed6cb8ea3eec2bce16be6d63136d3da63ef2ff8`: success two POSTs,
corruption/rollback three, lost post-commit response one and persistent pause one.
Every scenario restarted its actual container with no repeated POST. Root
retrieved the canonical report from shared knowledge at
`agent-bus/engineering-hub/acceptance/aed6cb8-fixture-container.md`, version 1,
SHA-256 `0e4b86bee2395a3f8ddaf0f21e6cedd001367a0cd8edac4b5929f62fef14f9fc`.
This is peer-run exact-source container evidence, not a root-run application pilot.

## Complete manifest and image-identity source validation

The next frozen source overlay, based on `eaffa9a`, passed **1,051 Linux tests in
57.46 seconds**, with only the existing Starlette deprecation and no resource
warnings. The validation image selection was
`sha256:d7e1dd2858f0f5935c915b13669c4cff3cf1f83e97dff5d3f3b286d317ef891b`
(OCI index, not an image-config digest). It includes the full shared manifest
schema, mandatory canonical-manifest controller binding and read-only identity
helper/tests. Isolation and private tmpfs match the preceding full suite; the
test container was removed after exit. The source archive build also passed,
and current OpenAPI/TypeScript fixture checks remain green.

Root separately reran 298 Windows pilot/manifest/client tests and all 37 image-
identity unit tests. Independent functional review found noncanonical direct
binding text could poison a formatting-only restart comparison; the controller
now rejects it before binding and a canonical restart regression passes. No
default service, provider, watcher, credential or daemon configuration changed.

Root's actual read-only local-image probe returned zero and verified the three
distinct metadata digests documented in the pilot contract. The helper streams
bounded archive metadata, extracts no files and skips layer authentication. That
result is not build provenance, daemon admission, a deployed container or actual
pilot evidence. The full manifest schema similarly proves policy syntax and
digest binding—not backup consistency, secret resolution or host enforcement.
Actual application rollout/rollback, OAuth and live topic integration remain
unfulfilled acceptance gates.

## Canonical registry request/stream bridge

The frozen registry-bridge source overlay based on `52d534c` passed **1,283
Linux tests in 58.76 seconds**, with only the existing Starlette deprecation.
The validation image was OCI index
`sha256:e17d728357f9629721caf1be184dfad1b7dc842c35f1906a0505df77c62227cc`.
It ran with no network, host mounts, exposed ports, credentials or Docker socket;
read-only root, dropped capabilities, resource bounds and private temporary
filesystems match the preceding campaign. The container auto-removed on exit.

The first full run found a real REST/MCP metadata mismatch in the legacy topic
projection (1 failure, 1,282 passing); the shared projection now supplies the same
registry epoch/event-source defaults, and the full rerun above passed. Focused
review separately found malformed event cursors triggered an upstream snapshot
before local validation. That ordering is fixed, with regression assertions that
neither snapshot nor subscription can run for malformed cursor/header requests.

Request-scoped synthetic transports cover original-agent bearer preservation,
fixed-topic human reader isolation, denial/default-unconfigured behavior, expired
and future evidence, independent registry cursors, bounded stream parsing and
stream cleanup/session revocation. OpenAPI matches source, actual TypeScript
fixtures match public DTOs, and wheel/source builds pass. This proves neither
live registry availability nor trusted provider enrollment, actual same-chat
readiness, offline delivery or reboot recovery. No live bus or connector changes
were made in this validation campaign.

The dashboard counterpart passed **15 unit tests, TypeScript/Vite build and 24
Edge browser tests**. It opens a separate canonical topic event stream only when
the snapshot advertises it, uses explicit epoch/head snapshot recovery, closes
on source errors instead of automatic retry, and invalidates readiness without
disabling healthy workspace actions. Review found a stale rejected topic fetch
could erase a newer accepted snapshot (or clear workspace state on a late 403).
Both read-generation guards and deterministic 503/403 browser regressions now
cover that race. Unit cases also cover successful epoch changes and callbacks
from closed streams. Browser tests use synthetic routes/events: they do not
establish deployed registry, trusted-session, private OAuth or reboot acceptance.

## Actual bundled control-image packaging smoke

Root built the independent control Dockerfile from clean source
`292a648464471b65013637bfba13ea70b6a9dba1` with that exact revision label.
Immutable local OCI index:
`sha256:74a7c1b0cb66ff7839e91756adf3d6bc058cce6917bcf4ca3e63a87b689505a9`.
The read-only metadata helper independently verified its original-byte chain to
amd64 platform manifest
`sha256:a9f6a4c992c02665879c9f4b6c534fe8cee2f43c595304c122cb4e742e4fe59a`
and config
`sha256:bf729811d05fca793d4f57160f655ed711698ae9b06aaed1c2442c4c7d3abc14`.
This is metadata identity, not authenticated source/build provenance or layer proof.

The hardened host-side smoke harness ran that immutable local image with pull
disabled, network none, no ports or host mounts, explicit UID/GID10001, read-only
root, dropped capabilities, no-new-privileges and bounded resources. Its in-container
probe confirmed actual app health, both compiled JS/CSS assets and content types,
security headers, anonymous private-API rejection and human login disabled. Each
of three synthetic agent identities initialized MCP and listed exactly the nine
Hub tools; unauthenticated and invalid-token discovery returned401. No real token
or OAuth credential was retrieved. The uniquely owned container was removed, and
a label-filtered container read confirmed no remaining smoke containers.

Independent review hardened missing-image/no-pull behavior and cleanup when a
startup response is interrupted. Twenty-four fake-subprocess tests cover owned-ID
cleanup, foreign identity refusal, absence and ambiguous daemon failures. Probe
readiness/whole-operation time and response sizes are bounded. The harness is
outside the tested image and does not change its packaged source revision.

This is an actual application **packaging/startup smoke**, not the required
controller-driven deployment/health/rollback pilot. Private state was disposable
tmpfs; no state durability, host reboot, OAuth, trusted registry/provider, actual
host adapter, immutable build approval or TrueNAS acceptance is established.

Full isolated Linux validation of frozen source `fc56198` (including the deadline
compatibility correction and smoke-harness tests) subsequently passed **1,323
tests in 59.13 seconds**, with only the existing Starlette deprecation. The
validation image was OCI index
`sha256:43bb59ad6c636350a917a0157062966463f784ae0a3ada4eb3c0e9a05ffb7309`.
It used the same network-none/no-host-mount/private-tmpfs validation profile and
auto-removed after exit. This does not cover later peer-review corrections.

## Rollback holds and baked health identity

Source `1b9e5dd71ccb8a3e115904a1b7fd87b4f564a022` adds visible held rollback
outcomes, transactionally revalidated independent reviews, and a fixed package
resource for the build commit. Root ran400 affected controller/client/API/repo/
dispatcher/public-contract tests; all passed. Build-identity and hardened smoke
tests separately passed63 cases (overlapping, not additional to that count).
UI unit18/build and all29 Edge E2E tests passed, including the held reason display.

Root built the exact clean committed source with `VCS_REF` equal to that commit.
The actual local image's original-byte metadata chain was independently verified:

- OCI index `sha256:76b8b4e4d2f28d9bfe4761dad0cc504dff2f086a5fab0d6e6936bcdd488ae4fe`.
- amd64 manifest `sha256:e32299ecbbbdb90231d52a2f35b6964de0f7719e120b933c37630e3b899f53cc`.
- image config `sha256:121f1bcc631c20cb29c40923a8f405e462c4e03ad5fa7fc1a253cf67328daed9`.

The offline image smoke passed with `--expected-commit` equal to the exact source.
The actual `/health` response reported that baked commit; packaged assets, private
API rejection and three synthetic MCP identities/exact nine tools also passed.
A second actual invocation with a deliberately wrong expected commit was rejected.
Both owned disposable containers were removed, and the label-filtered daemon read
confirmed none remain. No runtime environment or label supplied the health value.

This proves the build recipe and probe discriminate an actual build identity,
not that the identity claim authenticates a builder, layers or source provenance.
It remains application packaging smoke, not controller-driven rollout/rollback,
coherent durable state, dead-owner recovery, OAuth, TrueNAS or reboot acceptance.
Claude's independent source review was requested as727d5228-a4d8-495c-928c-513a5443b6a6;
no review result is inferred from these local passes.

## Recovery ownership and phase-event prerequisites

The frozen source overlay based on `7e266d9` passed **1,528 Linux tests in61.01
seconds**, with only the existing Starlette deprecation and no resource warnings.
Validation OCI index:
`sha256:d574c0a593df8b5409a56885fe2ae6444316aa3985c1c808b20e8a48a12c4e67`.
It used the same network-none/read-only/no-host-mount/private-tmpfs profile and
auto-removed. The run includes35 actual Linux ownership cases: process exclusion,
SIGSTOP refusal, SIGKILL with a surviving fork child, lock replacement, foreign
or copied state, epoch changes, garbage collection and descriptor cleanup.

Review found two primitive defects before this accepted snapshot: enrollment
initially pinned only the database inode, not the lock inode; and dropped Python
owner objects leaked raw descriptors. Both have regressions. An earlier full
run passed with test subprocess pipe warnings; a cleanup change then failed on
entering an already-closed stream. Registering idempotent close callbacks fixed
the test helper; the final full result above is the clean rerun, not a suppressed
warning. Production ownership code did not change for that test-only correction.

The overlay also includes phase-specific event UUID receipts, legacy dedupe
preservation, and strict revalidation of injected typed responses before storage.
Windows focused integration passed273 tests with the Linux cases explicitly
skipped. UI18 unit/build, current OpenAPI/actual TypeScript fixture checks, and
wheel/source builds pass. No visual behavior changed in this slice.

These are prerequisites only. `PilotOwner` is not wired into service startup or
any workflow/dispatcher writer. There is no new recovery endpoint, held-run
continuation, stale-owner adoption, automatic accepted-pause recovery, default
worker, credential or permissions change. Remote effects remain the separate
host adapter's responsibility. No actual controller-driven deployment/rollback,
private OAuth, native enrollment, TrueNAS migration or PC reboot is proved here.
# SQLite ownership descriptor correction

Frozen validation image
`sha256:3b924dc8204af59a99423ac1cf1d5837bcfefd1b394ad0f44d374068968a15c6`
contains the no-raw-existing-database-FD correction and cross-process lock test.
Focused actual Linux ownership tests: **36 passed**. Full suite: **1,529 passed
in 61.48 seconds**, with the existing Starlette deprecation warning only.
Tests ran as UID 10001, network disabled, read-only root, no host mounts, ports,
Docker socket or credentials, and private temporary test directories.

A separate disposable container monkeypatched close to raw-open/close the DB
before normal owner cleanup. The exact new regression failed as expected with
`SQLite transaction lock was lost`; the unmodified image passes. This confirms
the test detects the reviewed POSIX-lock hazard rather than merely exercising
the success path. Validation does not prove controller ownership integration,
durable production storage, real application deployment or reboot recovery.
Documentation-only updates following the image build are not part of its layers.

## Explicit ownership integration and read-only inspection

Frozen combined image
`sha256:7dd769354665d5b92378448d8463b9eb3b021ac34d0d716c22853ca24748bc3d`
passed **1,561 Linux tests in 61.43 seconds**, with only the existing Starlette
deprecation. Same isolated UID 10001/read-only/no-network/no-host-mount harness
as above. It includes explicit capability guards in the pilot and dispatcher,
legacy fixture enrollment refusal, test-only ephemeral storage, and inspection
identity/response validation without ledger mutations. Earlier focused image
`sha256:80c1b48a20dd27a3de53600c6c399b2e1cc432fdcf061ce873bbb6b663604540`
passed 58 ownership/capability/dispatcher tests before the later fixture and
inspection corrections; final combined full-suite evidence supersedes it.

Independent subagent source review found the direct fixture bypass, which was
corrected and re-reviewed with no further concrete defect reported. That review
did not execute tests. No service entrypoint is activated, and no epoch-tagged
reservation adoption, real host rollout, OAuth login or reboot acceptance is
claimed. The final documentation updates postdate the frozen image build.

## Owner ancestry, repository barrier and explicit crash recovery

Frozen image
`sha256:19e02061561fd0d00213d86f067733b7840f78cb40cb3c71a0a3a35d81c3ee93`
passed **1,634 Linux tests in 64.27 seconds**, with the existing Starlette
deprecation only. Same UID 10001, isolated network-none/read-only/private-tmpfs
harness; no host mounts, Docker socket, ports or credentials. A separate focused
run of the same image passed **73 tests in 2.84 seconds** for recovery, ancestry,
epoch reservations and repository barriers.

The new subprocess test SIGKILLs the actual controller after its durable original
deploy reservation, proves a second owner cannot acquire the live process lock,
then acquires the successor and GET-recovers the original command. Only the
never-reserved smoke command is subsequently submitted. Host responses are
typed simulated evidence: this is not a Docker application effect or reboot test.

An earlier image's full run had 1,632 passes and two failures: the new SIGKILL
harness omitted the newly required repository capability (corrected in both
child and successor), and unchanged `test_push_worker` expected one publication
but observed two. That latter test passed in the final standalone full run; its
intermittent publication-count behavior is not diagnosed or fixed by this patch.
Resource warnings were emitted after that failed test's teardown, not in the
final accepted run. Intermediate images are not substituted for final evidence.

Default service startup is not activated. Accepted-pause dispatcher continuation,
cleaned-up held runs lacking a stale advance, actual host adapter acceptance,
real OAuth and reboot/durable-volume acceptance are still required. Documentation
updates following the final frozen build do not change runtime/test sources.

## Quiescent continuation and event-driven runtime integration

Final frozen image
`sha256:6e94213059c082068628d825edaf5de1075bf77974e1e37efcdbd30ea177689b`
passed **1,700 Linux tests in 71.36 seconds**, with only the existing Starlette
deprecation. The same image passed **18 focused runtime/dispatcher tests in
2.20 seconds**. Isolation remains UID 10001, network none, read-only root,
private disposable tmpfs, no host mounts, ports, Docker socket or credentials.

Durable quiescent attempts permit normally cleaned-up held/paused runs to be
inspected safely. Dispatcher UUID/epoch proof and original accepted request
binding preserve run/fence/manifest identities. Tests include concurrent unpause
before receipt persistence, original-command GET only, no unchanged-head retry
loop, active-epoch exclusion, malformed-prefix fairness with limit=1, and receipt
completion from another executor's terminal run event. Enrolled read contexts
reject writes; schema and outbox writes use guarded transactions, with pre/post
bus-I/O owner checks.

Two tests run the real StagingRuntime and dispatcher rather than manual drains:
an injected EventBroker invalidation continues a paused run, and a new runtime's
startup snapshot recovers after simulated owner loss. Receipts remain simulated.
These tests do not establish installed native-watcher, supervisor, host completion
push, real app, TrueNAS volume or reboot acceptance.

An earlier frozen image `sha256:0957a278ef9e4222eea152a13d1619c1d950b4bc221fcc779764f7186051af92`
passed 1,697 tests before the runtime integration cases and same-run terminal
event handling were added; final combined evidence supersedes it. All subsequent
changes in this slice are documentation only, not frozen runtime/test changes.

## Owned service lifecycle, auth barrier and scoped retry

Final frozen image
`sha256:2eaa8357b1ac79d9fc4f159fa5a86f783444917f0bee9014ead9a47f55e2b001`
passed **1,754 Linux tests in 70.28 seconds**, with only the existing Starlette
deprecation. Same UID 10001, network-none/read-only harness, bounded private
tmpfs, no host mounts, Docker socket, exposed ports or real credentials.
OpenAPI source/snapshot and actual TypeScript DTO fixture checks also passed.
Production bus source, root image/Compose and workflows have no diff from the
preserved `2001ee7cf0b24cce12b7340904476d06c36593dc` baseline.

Eleven Linux owned-service tests exercise actual local ownership with an
injected event feed and simulated executor: detached dispatch/recovery, repeated
shutdown cancellation, admission stop, runtime/API drain, feed/client failure
cleanup, original-creator recovery access, failed health on owner loss, duplicate
capability transfer rejection and unchanged default activation. The owner stays
held until accepted work is terminal. Twenty-two portable owned-task tests cover
bounded concurrency, concurrent deduplication and cancellation behavior.

A real child process commits a task, spills a different uncommitted transaction
into WAL, then is SIGKILLed. The first repository read before successor ownership
writes sees only committed state and rejects mutation. This covers existing
writable SHM; absent/unwritable SHM permission variants are not asserted.
Authentication mutation paths now share the repository transaction barrier.
Held-receipt tests prove unrelated task/decision and wrong-run events cause no
executor inspection; exact-run invalidation, unpause and explicit recovery can
inspect the original command and continue only never-reserved phases.

The initial image's focused run passed 65 tests and failed three new fixture
cases that incorrectly created a human-only task as Hermes. The fixture actor
was corrected; authorization was not relaxed. Duplicate-transfer regression
cases were then added before the final image/full run. Subsequent documentation
updates postdate the freeze but do not alter runtime or tests.

This factory is explicit source wiring, not deployed startup configuration.
Actual host adapter/provenance/secret-provider acceptance, authenticated host
completion events, OAuth setup, real application success/rollback, TrueNAS
durable volume and reboot/installed connector acceptance remain separate gates.

## Supervisor grace and asserted lifecycle status

Frozen image
`sha256:0ee8ddb85088224e8dca7e205feae9efbb49b08baacf194d1c724917fe9151eb`
passed **1,778 Linux tests in 72.79 seconds**, with the existing Starlette
deprecation only. The isolated UID10001/network-none/read-only/private-tmpfs
harness still has no host mounts, socket, ports or real credentials. Portable
Compose/task/DTO tests separately passed 37 tests on Windows. OpenAPI and actual
TypeScript DTO fixture checks pass.

The owned factory now rejects missing/invalid/undersized supervisor grace before
ownership transfer. The 1120-second minimum derives from eight possible bounded
125-second inspection/submission operations plus a 120-second cleanup allowance.
The standalone profile declares 1200 seconds without activating pilot wiring.
Autonomy exposes configured/required values explicitly as an operator assertion,
not verified supervisor evidence. A simulated too-short observer deadline proves
the existing attempt/owner remain active until the blocked accepted work ends;
it is not a real Docker kill-timer or reboot test.

Independent subagent source review found no concrete defect in this grace patch;
it did not rerun tests. Production bus source/config/ports remain unchanged from
the preserved baseline. Subsequent documentation updates do not alter the frozen
runtime/test sources. Actual installed supervisor and workload restart acceptance
remain outstanding.

## Actual packaged app: named-volume restart and isolated backup restore

Built the actual control/dashboard image from runtime source
`80583b2219ce0f886af0aa882c87b26b4c46eb5a`, not the pytest validation image.
The metadata-only archive verifier independently resolved this Linux/amd64 chain:

- OCI index `sha256:e2dd35f2c151261eeb1668f5826b1c6fdc6ec413c14cea95d9cd8e272ad91567`
- Platform manifest `sha256:10c12a5ed821e26298ae19fdbec1c61d88189e6cc878bdc3ff4cf48e3571d359`
- Configuration `sha256:628e38091df71fc7b4855d820a13d59a8f336eac0f3a81c5fd2687af63d8485f`

This is a local build/identity record, not enrolled trusted host provenance or a
registry publication. The build initially rejected an abbreviated source SHA;
the successful image was rebuilt with the exact forty-character source identity.

`hub_dashboard_container_smoke.py` passed against that immutable index: actual
health identifies the exact commit, compiled JS/CSS and dashboard headers load,
all three synthetic agent identities initialize MCP and discover exactly nine
Hub tools, anonymous/invalid private access is denied, and human OAuth remains
disabled. No real bearer or network exposure was used.

The final `hub_durable_container_smoke.py` run also passed. It starts the actual
default app with a unique named local Docker volume, injects one clearly marked
synthetic task through the local repository fixture, claims it through the real
HTTP API, restarts the exact same container and verifies task identity, revision
2, claimed state and owner. The actual online SQLite backup utility produces a
0600, quick-checked backup. A helper with the source volume read-only copies only
that completed backup into a second fresh named volume, verifies identical bytes
and full integrity, then another actual app reads the same claimed task and
continues to deny anonymous/invalid access.

Actual filtered Docker metadata verifies nonroot UID/GID10001, network none,
read-only root, no extra capabilities/privilege/host bind/ports/devices, exact
volume mounts, 512 MiB memory, one CPU and 64 PIDs. All three uniquely labelled
containers and both labelled local volumes were removed after the final run.
No user data was removed. The immutable local image remains available.

Independent source review caught omitted resource-limit inspection and overly
permissive backup-report comparisons. Both were corrected before the final real
run; closed reports require valid SHA-256 and positive exact-integer sizes.
The final focused Windows set passed **84 tests**, including 46 harness tests,
with the existing Starlette warning only. No new full Linux suite is claimed for
these harness/documentation additions; packaged application runtime remains the
already full-suite-tested `80583b2` source.

This verifies default **unenrolled** application storage across a container
restart and isolated restore. It does not prove an owner-enrolled ledger can be
copied/re-enrolled, a Docker daemon/host reboot, real OAuth, TrueNAS durability,
autonomous controller-to-host deployment/rollback, or topic readiness delivery.
Those gates remain open; a manual local app restart is not the autonomous pilot.

## Required private cgroup namespace contract

Frozen validation image
`sha256:1f831cd9ffa83752e9dfed521a564eecef80e380475bb631325031992858d344`
passed **1,830 Linux tests in 71.54 seconds**, exit 0, with only the existing
Starlette deprecation. Same isolated nonroot/network-none/read-only/private-tmpfs
harness, no host mounts/socket/ports/real credentials. The full run now includes
the durable harness unit tests as well as the new required namespace contract.
A separate Windows manifest/client/workflow/API set passed 350 tests, and
OpenAPI/UI DTO checks passed. Independent source review found no canonicalization
bypass or silent legacy-profile upgrade.

`runtime.cgroupns_mode` is explicitly required and accepts only `private`.
Missing, host, empty, null, Boolean and uppercase values reject. Copied model
objects cannot bypass canonical revalidation. The positive canonical vector now
hashes to `sha256:e984626ae49fb99716d41cbab2e2cafcf356ef27bcc81a48f720731c5c752bc7`.
This is a coordinated pre-activation draft tightening; saved old accepted
manifests/commands must not be rewritten or assigned an inferred field.

The shared minimum `(1, 41)` records Docker's documented feature introduction,
not actual negotiated backend support or a version downgrade instruction.
Host EffectiveProfile, API admission and effective create/inspect checks still
need the matching adapter implementation and real daemon acceptance. This run
does not prove those host properties, nor update the actual app-image smoke's
previously recorded source identity. Production bus baseline remains unchanged.

## Local real-container private-cgroup profile check

The durability harness now passes `--cgroupns private` explicitly and verifies
the actual filtered `HostConfig.CgroupnsMode` is exactly `private` before starting
each original/helper/restored container. Host/empty/null/Boolean/uppercase values
are rejected by profile regressions. The final focused harness/Compose/dashboard
set passed **89 Windows tests**, with the existing Starlette warning only.

The actual local Docker run against the previously recorded immutable app index
`e2dd35f2c151261eeb1668f5826b1c6fdc6ec413c14cea95d9cd8e272ad91567`
and baked app commit `80583b2219ce0f886af0aa882c87b26b4c46eb5a` passed again:
explicit effective private cgroup configuration, same-container restart, online
backup, isolated byte-identical/integrity-checked restore, matching claimed task
state and anonymous/invalid-token denials. All three unique test containers and
both test volumes were removed; no user data was removed.

This changes only the reusable harness and tests, not the packaged app's source
identity or a deployed profile. It proves local Docker CLI-created effective
configuration, not a direct kernel namespace inspection, remote host/Engine HTTP
adapter behavior, negotiated API admission, trusted provenance/state providers,
owner-enrolled restore, OAuth, TrueNAS or a host reboot. No fresh full Linux suite
is claimed for this five-case harness extension; the preceding 1,830-test frozen
run remains the full controller/manifest source evidence.

## Independent host/controller source overlay

The published host `70df47c` owned-file commits were replayed onto controller
`1e9768b` in the separate detached review checkout
`../engineering-hub-host-overlay-70df-ca5`, resulting in `bed88c7`. The only
uncommitted review wiring adds `COPY hub_container_executor` to that checkout's
test Dockerfile. The primary controller branch was not merged or activated.

Frozen combined validation image
`sha256:cba0ebcaf4f7377ad1fdf6b1063a5be4128dcdf60ed751ee97cadc7486db1f12`
passed **1,919 Linux tests in 74.41 seconds**, exit 0. Same UID10001,
network-none/read-only/private-tmpfs harness, no host mounts, sockets, ports or
real credentials. This reruns the actual combined source instead of promoting
Hermes's branch-only report to controller integration evidence.

Two warnings were observed: existing Starlette/httpx deprecation and an
unclosed-event-loop ResourceWarning emitted during
`test_hub_dispatcher_continuation.py::test_matching_malformed_owner_fields_are_not_quiescence_proof`.
The allocation owner is not established; the warning was not suppressed, fixed
or attributed merely to the test where collection reported it.

A separate effect-free probe on that image confirms required shared cgroup mode
is **not** projected into the old host EffectiveProfile, and its binding accepts
API `1.40`. These known source-contract gaps remain despite green existing tests.
No daemon socket was opened and no new API support was inferred. Matching
cgroup field/create/readback and numerical API-floor rejection are required
before accepting the host against `ca5d0a0`. Concrete providers, generation
exclusion and actual autonomous app/rollback/SIGKILL acceptance are still gates.
The review checkout remains isolated for follow-up; it is not a deployed service.

## Cloudflare human authentication and canonical alias

Beau selected Cloudflare Access human authentication as an alternative to the
previous GitHub-only dashboard setup. Source commit
`68adb7333a5819c98a109832f5fb2646951e8b78` adds explicit provider configuration,
RS256/team issuer/EH-specific audience/expiry/exact-email validation, bounded
on-demand JWKS handling, a hashed local session bound to the verified identity,
and the existing exact-Origin/CSRF human-write protection. No fallback to GitHub
or agent bearers exists in Cloudflare mode. New human actors propagate through
repository authorization, decision DTOs and explicitly scoped registry readers.

Canonical browser origin is `https://eh.ledgetopdroneservices.com`; configured
navigation alias `https://eh.ledgetoptechnologies.com` redirects to the fixed
canonical root without query strings. Alternate-host API/MCP and non-GET/HEAD
requests fail 421 before authentication/route effects. These addresses and the
two user-approved exact emails are documented deployment inputs, not activated
DNS, TLS, Access application or policy evidence.

Frozen isolated validation index:
`sha256:a5a4cca1ead2b7caa1d83ca147c50fe2a045d13181a508d63bf6317244e0088c`.
The full Linux suite completed **1,942 passed**, one existing Starlette/httpx
deprecation warning, **72.48 seconds**, exit 0, in the network-none/read-only,
non-root, cap-drop/private-tmpfs harness with no host socket/mount/port or live
credential. This includes real RSA signature verification against synthetic
JWKS transport, real ASGI human/agent API integration, and Linux ownership-loss
and additive-session-schema rollback cases. An earlier frozen run was 1,941
passed/one failed because the checked-in OpenAPI human-actor regex was stale;
the existing exporter produced exactly that one-line schema update, and the
entire suite was rebuilt and rerun successfully rather than ignoring the check.

Additional evidence: **250 passed/four expected Linux-only skips** in the focused
Windows auth/API/repository/DTO/registry/Compose suite before the generated-schema
refresh; generated-contract check separately passed. Dashboard Vitest **18
passed**, TypeScript/Vite production build passed, and Playwright/Edge **30
passed**, including the new verified-email/Cloudflare profile view. Independent
review found no remaining auth/alias issue in the reviewed delta. Documentation
and reviewed source are not a live security-policy acceptance result.

Read-only public HTTPS observation of Ops returned 302 to
`ledgetoptechnologies.cloudflareaccess.com`; no cookies or query values were
recorded. Verify the real Zero Trust account, separate EH application audience,
allowed/denied identities, origin topology, both hostname certificates, and SSO
behavior before enabling. Existing Ops policy and agent bus remain unchanged;
baseline `2001ee7` has no diff in bus source/root Dockerfile/root bus Compose or
GitHub workflows. No TrueNAS activation, actual Access login, autonomous host
pilot, host reboot, or missing trusted host/registry provider acceptance is
claimed. Overall release remains incomplete.

Coordination INFOs were accepted by the bus as
Hermes `a0641515-7c52-44ed-9942-a371358097c6` and
Claude `b985188d-f31d-4f9b-8fd3-01e8c9b492d1`, on the existing build correlation.
Acceptance is not evidence of peer wake, processing or deployment permission.

## Access peer follow-ups and matching host cgroup source

Claude review `77691ea9-edb0-488e-af61-d13c576bd4cc` was claimed, evaluated,
answered with linked reply `4628142e-9041-4907-8789-73a5d0a00fa2`, and acknowledged.
Most points matched the already implemented boundaries. Two concrete source
follow-ups are in `4f3e15d6cbd2aa64d6d907d72ec2fcdf1645779d`:

- Durable first-verified Access subject pins per issuer/application/email,
  committed atomically with session creation. Pins survive logout, expiry and
  reopening; conflicting subjects cannot repin or delete a prior valid session.
  Concurrent enrollment and owner-loss schema rollback are tested. Account
  reprovisioning requires explicit operator review, not an automatic reset.
- Fifteen-second transport-only SSE comments on both Hub and topic routes,
  retaining one pending source read and the original subscription. No task,
  message or readiness polling/model wake occurs. Authentication is rechecked
  on idle sends; expiry/revocation closes the stream and pending readers are
  cancelled, awaited and closed. Comments do not extend evidence freshness.

Frozen source validation index
`sha256:0a44f19b9d029a84c4bac15d2ff0eb6b9be27ab816a4b3a6bba69f417f9d5ecb`
completed **1,974 passed**, one existing Starlette/httpx warning, **72.60 seconds**,
exit 0, with the same isolated Linux constraints. Focused Windows run was
220 passed/five platform skips; the separate actual-endpoint iterator/helper
suite was 25 passed. Endpoint tests seed synthetic verified identities and
typed registry fixtures; they do not claim real Cloudflare or registry acceptance.
Independent source review and seven concurrent/persistent-pin regressions
supplement the cryptographic verification cases from the prior section.

A newly built real packaged Hub image identifies source `4f3e15d`:

- OCI index: `sha256:c90bf9c574d338baa1f034b691274203cef6fb49c4eb02ab2079b2e034425654`
- Platform manifest: `sha256:0026cc65862bed049a29e7280d3883d51044582f8ce6ad358f37230b00b97a4c`
- Config: `sha256:80f127a3100af33f3e055932ddf13a9c261af18651e5f60d6144bc870191aedf`

The existing offline dashboard smoke passed against that exact index and source:
two compiled assets served, human sign-in disabled, anonymous private requests
denied, three synthetic authenticated agents, and nine Hub MCP tools. It used
no network, host mounts, published ports or live credentials, and cleaned up its
owned disposable container. This is packaging/auth-disabled startup evidence,
not identity-provider, durable-state, trusted provenance enrollment, or deployment
acceptance. The image is local; no registry push or activation is implied.

### Host shared-contract alignment artifact

The independent combined host review checkout is now published as
`work/hub-host-cgroup-alignment-codex`, commit
`e5ed4d32dc467a3cb628d38e992b10acab3dbb63`. Its eight-path source patch adds the
required EffectiveProfile field, manifest derivation, explicit create field,
strict effective readback, canonical numeric API floor, regression coverage and
corrected source-limit documentation. The previously local validation Dockerfile
COPY is included so the host tests are reproducible. Hermes's existing branch
and primary control branch are not changed by publishing this separate artifact.

Frozen combined validation index
`sha256:24fb01467f0381f9e4314ae139a4cf4ccec5bad01d919e4720b9f682aa4639b0`
completed **1,944 passed**, **73.47 seconds**, exit 0. Warnings were the existing
Starlette deprecation and an unclosed-event-loop ResourceWarning, this time
emitted during `test_hub_owned_service.py::test_invalid_stop_grace_rejected_before_owner_transfer`.
The allocation owner remains unestablished; neither the warning nor its cause
was suppressed, fixed or assigned to that test merely from its emission site.

New fake-transport regressions reject API 1.40 and noncanonical versions before
I/O, preserve higher pins including 1.56/1.100, require explicit private mode
with no default, check private create body, reject missing/host/empty/null
readback, and deny unsafe plans before daemon requests. Expected-denial test
paths close their synthetic HTTP clients. The source-contract gap is addressed
in this artifact; **real daemon execution is still unproven**. Concrete trusted
providers, generation exclusion, registry implementation, coherent enrollment,
real autonomous app/rollback/SIGKILL and TrueNAS/reboot acceptance remain required.

Coordination INFO `3b3a0568-2b3a-4ba3-845d-0d3857c66110` told Hermes the patch was
confined to this review checkout; no replacement daemon worker or activation was
requested. Live bus/ports/data, Ops, DNS, Access policies and credentials remain
unchanged. Overall release is still incomplete.

## October 18: browser/agent ingress boundary candidate

Starting from main `112a60e629a0555b2a9de2f2812f5c8eeda1e0d1`, the source adds
opt-in `combined` (unchanged default), `agents`, and `browser` ingress roles.
The pure-ASGI boundary executes before redirects, static serving and human auth.
Agent mode retains existing bearer/source-IP authorization but refuses UI and
human-auth paths; browser mode requires configured Access assertions even for
index/assets/sign-in, rejects bearer/MCP, and retains local session/CSRF checks.
Explicit exact agent MCP hosts preserve DNS-rebinding checks. The entry point
now disables implicit forwarded-header trust. Browser execution injection fails
closed; no PilotOwner rule is relaxed for a second frontend.

Independent read-only review accepted this bounded source slice and independently
ran the new ingress plus entrypoint tests: **91 passed**. Implementation-focused
API/auth/MCP coverage: **218 passed**, one existing Starlette warning. Tests use
local RSA signatures and mocked JWKS (not live Cloudflare credentials), including
wrong audience/key/algorithm rejection, all three agents, source-IP/header/path
spoofing, session/CSRF, multi-body streaming and cancellation propagation.

Frozen working-tree validation image index
`sha256:3154c9d370cd31d99696b078c8f77ecbf7deb93349cb018628f3670e34ae4eda`
passed the full Linux suite: **2,083 passed**, **76.08 seconds**, exit 0, one
existing Starlette/httpx deprecation warning. It contains the candidate runtime
and tests; this evidence/deployment-document update followed the image build.
The run used network none, read-only root, UID10001, private cgroup namespace,
cap-drop ALL, no-new-privileges, two CPUs, 2GiB RAM, 256 PIDs, no host mounts,
ports, Docker socket or real credentials. Only disposable private tmpfs was
writable: executable test scratch `/tmp`, and noexec `/validation/data` plus
`/validation/tests/.test-data`. The named container removed itself after exit.

The first Windows full-suite attempt failed collection because existing bus file
code imports Linux `fcntl`; it was not counted as acceptance. Two initial Linux
harness attempts omitted required fixture tmpfs and likewise failed collection.
The final invocation supplied the already-documented isolated mounts; no test
assertions or application behavior were weakened to obtain the passing result.

Separately authorized Cloudflare work created a Hub-only Access application and
exact-two-email Allow policy, without modifying Ops. An unauthenticated public
request changed from HTML200 to Access-login302. Direct NAS8770 still returned
HTML200. Origin-side Access configuration, physical browser/agent separation,
signed-in browser acceptance, cross-frontend shared-state/events, actual TrueNAS
rollout/reboot, trusted host providers and an owned autonomous pilot remain
unproven. Two app processes on an unenrolled SQLite database are not a substitute
for deliberate single-owner integration once the pilot ledger is enrolled.

A post-app-restart native listener recovery attempt also exposed a separate live
bus fault: version0.6.0 health reports native push `failed`, `OperationalError`,
subscriptions disabled; independent initialize advertises `resources.subscribe`
false while ordinary authenticated receive works. No polling fallback, bus
restart or server configuration change was made. A linked read-only diagnostic
request and evidence were sent to Hermes; acceptance is pending, not assumed.

## October 18: one-owner split listener and packaged-image verification

Source commit `6531c74214253c4c853e209eacff16ee6addf51e` adds a single shared
core/lifespan with agent and browser ASGI adapters. Both sockets bind before core
construction; browser bind is hard-coded IPv4 loopback. Listener configuration
executes inside the owner lifespan, with explicit single-worker settings.
Startup errors unwind ownership; listener loss drains its peer, and repeated
caller cancellation cannot release the core before listeners drain. Private
in-process JWT verification proof avoids double verification without trusting
public headers or arbitrary request state. No existing bus source changed.

Independent read-only review accepted lifecycle, JWT proof, Compose, smoke and
publication gate changes; independently executed server/proof/smoke tests:
**36 passed**. Root's final focused owner/proof/frontend/entrypoint check:
**32 passed, 1 Windows skip** (Linux owner test). Earlier final focused set
before the two owner regressions: **66 passed, 1 Windows skip**.

Frozen Linux validation image index
`sha256:49870470dc8e725245f6dfb0461aab18b3c0c4bb9781e69bd62be5e2d9b1c863`
contains the final runtime/tests and passed **2,131 tests in 81.08 seconds**,
exit 0. Same isolated profile/tmpfs as the previous entry. It includes the Linux
actual PilotOwner frontend test. Warnings: existing Starlette deprecation and an
unclosed-event-loop ResourceWarning attributed during an executor-client test.
The latter is not declared fixed: rerunning frontend/server/proof/executor-client
tests with ResourceWarning promoted to error passed **114 tests in 2.62 seconds**;
its full-suite origin remains unconfirmed. Workflow and documentation changes
followed this validation-image snapshot; runtime/tests were unchanged.

Locally built packaged control image, baked source `6531c742...`, index
`sha256:de9783f80f07a56917e8a5048c2e5e37c201050cc1ca5f4e937016cba626831b`,
passed the new split smoke using its normal entrypoint plus `--split-ingress`.
Actual kernel TCP tables show only `127.0.0.1:8771` for browser and `0.0.0.0:8770`
for agents, no IPv6 Hub listener. Both health endpoints report the exact source;
all three synthetic agent tokens initialize MCP and list exactly nine tools.
Missing/invalid tokens fail; agent UI/auth/static routes and browser bearer/MCP
routes fail; missing/forged Access JWTs fail. The container runs UID/GID568,
network none, read-only root, no capabilities, no-new-privileges, private cgroup,
1 CPU/512MiB/128PIDs, private tmpfs and one owned disposable named volume only.
Recreation preserved a fixture marker, then ownership-verified cleanup removed
the disposable containers and volume. This proves marker persistence, not task
durability, an autonomous pilot or host recovery. No registry upload was done.

The Hub-only publication workflow now runs split smoke for PR builds, the exact
publication candidate before upload, and the authenticated registry digest before
latest promotion. Existing combined smoke remains. This does not authorize or
prove live rollout. Beau confirmed same-NAS host-network cloudflared and reserved
all NAS administration for himself; no agent NAS inspection/change is allowed
under the current scope. Operator instructions remain gated on a published image.
Actual Cloudflare login, NAS/LAN isolation, app/reboot acceptance, trusted host
providers and an owned autonomous pilot remain required. Overall goal incomplete.

### Stronger shared-record recreation probe

The subsequent smoke-helper enhancement was run against that same packaged
`6531c742...` image and passed. It seeds one synthetic human-owned fixture task
through the repository (not a fabricated browser session), claims it through the
Codex bearer API and creates an open non-authorizing decision through Hermes's
bearer API. All three agent identities read exact resulting records. After
container replacement, only GETs verify the original task/decision IDs, full
fields, status, owner and revisions against private saved expectations. Report:
`durable_task_decision_recreation: true`. No task/decision is recreated or replayed
in the replacement. Unit evidence: **31 passed**, one existing warning; includes
fresh-app transport and deliberately mutated-record failure. This strengthens
record persistence evidence, not browser authentication or autonomous execution.

The enhanced helper/tests full Linux snapshot index
`sha256:da8928790b1fd5252341a6e7aa3b99bb5757e28b99876cc88e4fb940c286e347`
passed **2,133 tests in 80.98 seconds**, exit0, with the same Starlette and
unclosed-event-loop warning classes. Independent helper review ran its scoped
18 tests and accepted exact-record and negative mutation coverage.

Claude's review identified a valid legacy topic containing a colon being encoded
as `%3A`, rejected by strict ingress and treated by UI as global session loss.
The UI now validates the exact legacy alphabet before HTTP and preserves literal
colons; topic-specific ingress denial stays local while real auth/global ingress
denials still clear private state. Canonical registry streaming retains its
separate narrower alphabet, not a new colon-support claim. UI unit19, full
Playwright32 and production build passed; independent API review10 passed.
A real two-socket regression also proved literal-colon peers200 while encoded
colon remains403 (10 server tests passed). That server-test assertion and the UI
changes followed the Linux snapshot above; do not imply it included them.
Both GitHub workflows for prior head7935ef4 succeeded (39166678653/39166678655),
including actual packaged split smoke; updated source still needs fresh CI.

### Actual application pilot: concrete remaining host composition

Read-only comparison with host overlay `e5ed4d3` confirms the concrete Engine
backend exists but is not integrated into the control checkout. Actual acceptance
still needs protected content-addressed/build enrollment, installed-driver
measurement, immutable private test-secret versions, healthy baseline enrollment,
exclusive bounded candidate/rollback state generations, and a durable private
operation journal. Existing mocks do not supply those trusted implementations.
The explicit application-pilot acceptance entrypoint is still disabled.

Two specific compatibility gates were identified: the host profile rejects the
control image's fixed `HUB_DB` default, and the pilot requires UID/GID10001 rather
than the control deployment's568. Resolve these with exact profile/default and
private-state permission evidence; do not broadly waive environment checks or
change the working control image just to pass. The isolated pilot expects the
combined internal8770 target, not the production split-listener layout. Mid-effect
unknown successes deliberately remain held; durable completed-command recovery
must not be reported as arbitrary successful crash continuation. Hermes request
`861ce3b6-44ab-4ad9-a4a2-7774e1be9c73` asks for the bounded source/provider
composition and isolated evidence only, with all NAS administration excluded.

### Approved 30-day sign-in preference

Following Beau's direct request and action-time confirmation, only the Hub Access
application duration was saved from24hours to1month and independently read back
in the browser. Existing Hub policy inherits application duration; Ops/global
settings, email selectors, agent tokens and TrueNAS were unchanged. The local
Access-session ceiling now becomes2,592,000seconds, while GitHub and standalone
SessionStore defaults remain28,800seconds. Verified JWT expiry still caps the
local session and every browser request still requires a signed assertion.
No leeway, sliding renewal, identity repinning or retroactive cookie extension.

Implementation auth/ingress/proof tests142passed; independent auth review45passed;
root reran the broader set including real sockettests: **152passed**, existing
Starlette warning. Tests use actualRSA/mockedJWKS and a controlled session clock
for day29/day30, earlier JWT expiry, non-renewal, missing/forged assertions, CSRF
and logout replay. These prove source behavior, not thirty elapsed real days or
installation of this updated release. A fresh sign-in after deployment is needed.

## October 18: split-ingress release published from main

PR2 merged as `c720f7a82c70230e267a369077211749c30e9360`. Main Hub workflow
`39167860307` completed successfully, including verify and publish. Verification:
**2,137 Python tests**, two warnings, **102.27seconds**; **19 UI unit tests**,
**32 Playwright tests**, production UI build, actual combined and split image
smokes. Legacy workflow `39167860273` also passed; old-bus publication skipped.

Published private package:
`ghcr.io/ledgetoptechnologies/engineering-hub-control:latest`
is recorded and read back by CI as
`sha256:fdfa41b8c0a511c50eeb80a9a1e4c780b4d9b30a82b2cc0a54e57c10824f1bab`.
The pipeline verified package privacy before upload, tested the exact build,
authenticated registry pull and same-artifact startup, denied anonymous pull,
checked current main, promoted the tested digest, compared manifests and tested
startup again. Registry split smoke reports baked source `c720f7a...`, three
authenticated identities/nine tools, actual agent/browser binds, successful
task/decision recreation and no real human sign-in or TrueNAS deployment claim.

Root downloaded `hub-control-main-publication-evidence`, independently compared
registry/latest manifest bytes and computed their SHA256: it exactly equals the
release digest above. The workflow artifact does not include the intermediate
descriptor JSON; an initial local check for that missing file was a harness
mistake, not a failed publication. Local Docker registry inspection returned403
under this PC's existing Docker credential context. CI authenticated pull success
is not proof that this PC or NAS has equivalent credentials. Keep the package
private and use the operator's already approved registry configuration.

The operator-ready source Compose is at main `c720f7a...`; all real tokens and
dedicated state remain private and must be preserved. Only after installing this
image should the operator point the Hub tunnel route to `http://127.0.0.1:8771`
on the confirmed same-NAS host-network connector. No agent changed NAS, tunnel,
Ops, old8765 endpoints, clients or stored data. Actual LAN isolation, fresh
Access/Hub sign-in, container/app/reboot acceptance and owned autonomous pilot
remain outstanding. This is publication acceptance, not overall goal completion.

### Isolated host-provider journal source

Separate host-overlay commit `12abc7c2c8d9e22d9dac3c9ce5cc8b6930b34625` adds an
explicit private SQLite operation journal, not runtime wiring. Every existing
operation ID denies a second `begin`; exact completed receipt recording alone is
idempotent. Pending uncertainty survives process exit; no health/retry/terminal
failure is inferred. Dedicated schema, Linux owner/private-path and inode guards
prevent ordinary unrelated-database adoption. Malicious same-UID administrative
replacement races remain outside this private-directory trust boundary.

Independent review found and verified correction of inherited child descriptor
cleanup; the regression holds a forked child alive while proving parent authority
and later reopen. Root's fixed isolated Linux image index
`sha256:f601fee8a3961c8cb3910ec1088074028a13c00b534c7072dbef50b6bddcb14c`
passed journal plus existing daemon/hardening tests: **96passed,1skipped,1.77s**.
No network, host mounts/ports/socket, credentials or Docker effects were exposed.
Tests cover committed intents/receipts across real process exit; hot rollback
journal recovery during a transaction remains unproven/fail-closed. The journal
is not integrated or enabled, and does not complete the actual application pilot.
