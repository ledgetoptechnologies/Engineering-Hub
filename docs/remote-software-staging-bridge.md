# Remote software staging: next integration boundary

Status: source design, not an enabled service or host installation. Beau selected
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

## Missing queue-to-pilot bridge

The next implementation should bind a privately claimed queue job to exactly one
durable pilot run before sending any remote command. This is not implemented by
the ASGI concurrency correction. Keep the current queue/pilot mutual-exclusion
guard until every executable pilot admission participates in the same reservation
authority; do not just remove that guard to connect the modules.

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
3. Run creation changes the task's status/revision. The existing queue target
   verifier requires `verified`; the bridge needs a narrowly bound run-aware
   check, not a blanket exception accepting all running tasks or stale reviews.
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

Then, only after separately approved activation, verify the existing staging
host's effective transport, writer exclusion and resource limits, immutable
image, health/rollback and restart recovery. TrueNAS deployment remains Beau's
operation; no agent NAS management or production change is implied.
