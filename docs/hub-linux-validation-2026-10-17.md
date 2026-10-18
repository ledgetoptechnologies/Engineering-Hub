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
