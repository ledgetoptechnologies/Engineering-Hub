# Work queue source checkpoint — 2026-10-18

This is source/test evidence, not an installed staging worker or production
deployment. The existing message bus on 8765 is unchanged.

## Scope

- Durable reviewed-task admission with immutable task/commit/artifact targets.
- Production-first selection among eligible, approved jobs; FIFO within a class.
- Non-preemptive reservations per physical machine, shared across environments.
- Agent/process-instance ownership and monotonic fencing generations.
- Expired or uncertain claims hold the machine; no automatic replay.
- Completion of executable work requires an explicitly injected evidence verifier.
- Transactional audit/outbox invalidations, observed through the native event feed.
- Closed, authenticated HTTP routes under `/api/hub/v1/work`.
- Explicit opt-in; no worker, host access or production approval is discovered.
- Reject coexistence with legacy dispatch until a shared reservation bridge exists.

## Verified

Frozen Linux validation image:
`sha256:4650bba0f4b3e9ddec5476f81d63486fe5b20b5fe72e789a54f76332ab55cdd6`.
Full suite: **2573 passed, 3 skipped, 4 warnings**, 89.23 seconds. The three
owner-change tests were separately run against that exact image as root with
only `CHOWN`: **3 passed**, 0.30 seconds. Warnings were the known Starlette
dependency deprecation, two deliberate multithreaded-fork diagnostics, and an
unclosed asyncio test event loop. This is not a zero-warning claim.

The actual-HTTP smoke, in the same image, passed real authentication/401,
production priority with a **synthetic approval provider**, non-preemptive machine
exclusion, instance fencing, lease renewal, expiry holds and native SSE
invalidation of the exact queue event. Its actions were explicitly
**coordination-only**: it ran no application tests or deployment, created no
deployment runs and exercised no host, NAS or reboot recovery.

After that snapshot, a pre-effect `live_claim` accessor and regression test were
added: **39 focused queue tests passed** on Windows using an explicit private
test directory. That later accessor is not part of the frozen Linux result above.
It checks current instance/fence, lease, pause, registry, review and approval; it
does not grant host execution capability or eliminate uncertain-effect recovery.

## Remaining before first real execution

The default registry has neither completion nor reconciliation verification.
It can coordinate claims, but it is **not yet a complete executable workflow**.
Do not enable it for unattended real work merely because these tests pass.

Next is one explicitly enrolled immutable validation image, a fixed isolated test
harness, durable pre-effect intent, independently observed container termination,
and a trusted job/fence-bound receipt. No request-supplied shell commands, image
tags, host paths, mounts, secrets or arbitrary executor URLs are accepted.

The operator must provision and verify the actual staging executor boundary and
install the selected release. No agent TrueNAS administration is authorized by
this checkpoint. Default production execution remains disabled. Software
validation is separate from application deployment/rollback, production promotion,
bulk ODM/OpenSplat/TreeDAR processing and actual host-reboot acceptance.
