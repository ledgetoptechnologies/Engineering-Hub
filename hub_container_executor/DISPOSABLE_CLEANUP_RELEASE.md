# Disposable staging cleanup: release boundary

The opt-in final stop is not a release authorization. A successful stop leaves
the run and machine reservation held. This document describes requirements for
a future explicit release path; it does not implement or enable that path.

The independently reviewed release must bind one disposable staging run to its
original queue job, worker, fence, target, manifest, exact final cleanup command
and outcome, original create receipt, executor owner epoch, launch identity, and
daemon generation. The saved single-pilot outcome and exact acknowledgment,
MAC-bound operation intent and completed stop receipt, and the retained v3
Engine response record must agree. All earlier accepted operations must have
durable terminal outcomes; a pending or unknown operation remains held and is
never retried or inferred absent from a later empty scope. Inspect the original
IDs only. Failed candidate stop and rollback must use their own original
identities, not the final-cleanup receipt.

Before release, independently establish that the accepted coordinator advance
has unwound, no effect gate is active, the original executor has exited, and the
dedicated Engine and all its effect-capable descendants have been stopped. The
baseline is a separately enrolled task-owned fixture and needs its own verified
teardown; it is not the candidate or rollback create receipt. The closure must
cover the Engine, containerd, shims, application processes and any other worker
that can execute an accepted request. An observed stopped container or an empty
daemon-only scope does not establish that closure.

A release capability must retain an independently provisioned root-controlled,
nondelegable host boundary from before the first effect until the release
transaction, with exact namespace, inode, process identities and writer
exclusion checked. It must prevent or detect later effect-capable entrants and
replacement of the boundary; signed point-in-time observations alone do not
provide that guarantee. The current rootless staging containment has not been
shown to satisfy this requirement. Privileged administrator interference and
external engines/writers remain outside this local boundary unless separately
fenced.

The trusted release operation must revalidate those facts and the exact job,
owner, lease and fence while a live closure guard is held, then compare-and-set
the same reservation in its queue transaction. No unverified JSON, Boolean,
caller assertion, replayed journal record, stale observation or different
target may serve as that guard. Failure, expiry, owner loss, target drift,
unknown operation outcome, or uncertain commit retains the reservation and
permits only evidence inspection. Recovery must not issue the stop again or
promote the run from incomplete evidence.

The existing `daemon_scope_observation` record is expressly marked
`effect_outcome_proven: false`. Even its postcheck does not prove all worker
closure or supply a live reservation-release capability. There is currently
no supported implementation connecting an independently enforced full worker
boundary to queue release; therefore the disposable queue proof remains
fail-closed. Implementing that capability and testing its crash, replacement,
late-writer, owner-loss and compare-and-set races is a prerequisite to release.

`OwnedGenerationDrain` closes local queue admission and drains its owned
accepted phase and event observer. Its acknowledgment explicitly does not
assert that the native executor stopped and is not remotely authenticated.
The capability is tied to its original process, loop and owner and is revoked
on service teardown. It does not provide the full-worker closure guard above.
The separate hierarchical supervisor scope accepts an externally established
worker inventory but does not discover all workers or exclude outside
delegation; the launcher closes its retained scope handle when it finishes.
Neither capability currently remains held and is revalidated across the queue
release compare-and-set.
