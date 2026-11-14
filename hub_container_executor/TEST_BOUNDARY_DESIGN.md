# Dedicated TEST worker boundary and retained release guard

This is an implementation proposal, not an installed capability or an
authorization to release a reservation. The existing disposable pilot remains
held. No system service, cgroup, identity, Engine, or staging setting is
created by this document.

## Scope and host assumptions

The boundary protects a single explicitly enrolled staging campaign. It does
not fence a malicious host root, systemd/cgroup administrator, kernel, or an
external service that already has equivalent access. Establish and document
independent exclusion of those writers or explicitly keep them inside the
trusted computing base. Global host disk capacity and unrelated Viewer work are
shared resources; a private cgroup is not a disk quota or host-wide writer
fence. A process listing is a momentary observation, not an exclusion policy.

The previous rootless route is unsuitable for the existing nondelegable scope
reader: task slices were beneath the `ehpilot`-owned delegated user manager.
Use an independently provisioned, root-owned system-level ancestor, with every
ancestor root-controlled and no non-root write access to the boundary or its
membership controls. Permit a bounded delegated child only beneath that pinned
ancestor; cap hierarchy depth and descendants. Use a dedicated TEST identity,
non-overlapping subordinate UID/GID allocations, private archive-backed data
and execution roots, and a private Engine socket. No other non-root process
may share that identity or reach the socket/data roots. The shared system
Docker and containerd sockets must not be used.

The Engine's cgroup driver and all launch paths must place the daemon,
containerd, shims, executor and applications below that root ancestor. Merely
starting a rootless daemon in a system unit does not establish this: a user
systemd bus outside the boundary can create scopes elsewhere. Test the actual
driver and runtime with a disposable process before enrolling a campaign. A
rootful task Engine also needs independent restrictions against escaping the
boundary; root ownership and a private socket alone are not sufficient.

## Implementation sequence

1. Add a root-owned host provisioner that creates only a uniquely named TEST
   system slice, child unit and private paths, pins their cgroup and filesystem
   identities, and records original resource limits. Require an independently
   approved dedicated UID and subordinate ID ranges. Reject pre-existing paths,
   symlinks, overlapping identity ranges, outside user-manager dependencies and
   an Engine configuration pointing at any shared socket or data root. Before
   every bounded operation, enforce the reviewed root-filesystem free-space
   floor plus headroom, archive capacity and actual memory/PID limits; stop
   before starting if these cannot be demonstrated. A failed create with an
   uncertain response is not retried.
2. Extend `EnrolledCgroupScope`/`preflight_worker_scope` with a separately
   reviewed closure capability. Before the first effect, independently establish
   the complete initial worker inventory using pidfds and verify exact recursive
   membership, host cgroup namespace, root-owned ancestor/control inodes and
   finite limits. Every later worker must be confined by the kernel boundary;
   observing initial pidfds alone cannot establish that. Pin the Engine socket
   and data-root identities and ensure no actor outside the boundary has an
   effective route to submit effects. Treat root/privileged actor exclusion as
   an explicit host assumption unless independently enforced.
3. Bind the run, immutable manifest, queue job, worker/fence/owner epoch,
   executor launch/daemon boot/PID/start, and reviewed exact effect enrollments
   before admitting an effect. Persist each original operation and its bound v3
   response projection. `OwnedGenerationDrain` closes only local queue
   admission: use it to drain accepted phases, but do not treat its
   acknowledgment as executor or worker closure. A lost response, pending
   effect, changed target, stale fence or unverified ACK stays held and is
   inspected using the original identity only.
4. After the exact final cleanup and ACK are authenticated, explicitly stop
   the separately owned TEST baseline, drain the executor, and stop the dedicated
   Engine/workers through their original owners. Verify all retained pidfds and
   recursive membership. Establish a **live**, root-owned closure guard that
   prevents new descendants or socket submissions, pins the boundary and
   identities, and remains valid until the release transaction commits. A
   double empty read or a `postcheck_complete` record by itself cannot supply
   this capability. Kernel freezing may be part of the design only after proving
   its semantics for an empty hierarchy and preventing outside writers; it must
   not be treated as a solution to external daemons or privileged movers.
5. Add an exact typed release capability to the trusted composition. The guard
   must be held *around the complete* `WorkQueue.reconcile` transaction,
   including commit, not opened and closed inside its verifier callback.
   `QueuedContainerPilot._verify_completion` must validate the identical
   job/worker/fence/owner and all stop/ACK/response records under that guard;
   the transaction must compare-and-set the original held job and reservation.
   Retain the boundary's no-new-effects restriction after commit until its
   Engine identity, private socket and task resources are safely retired.
   Existing reconciliation subject validation is an additional admission check,
   not the closure guard. If a separate privileged broker holds the capability,
   authenticated exact peer binding, broker death handling, and a fail-closed
   transaction protocol are required; a serialized Boolean or signed stale
   snapshot is not a live capability.
6. Unknown outcome, missing receipt, guard loss, owner/fence change, concurrent
   admission, process re-entry, failed rollback, broker failure or ambiguous
   commit retains the reservation and original evidence. Recovery can inspect
   and correlate those IDs but does not replay an effect or infer non-creation.
   Do not make a post-commit failed recheck the sole protection: by then the
   machine may already have been claimed by a different worker.

## Validation before any real queue use

Use local tests with real SQLite journals, exact signed launch/approval and
synthetic Engine outcomes to cover both final roles, all receipt/ACK bindings,
late/lost responses, wrong UID/daemon/fence, process exit and PID reuse,
callback cancellation, crash at each database boundary, queue CAS competition,
scope replacement, and broker death. A successful stop with absent closure must
stay held. Test old manifests and non-disposable behavior unchanged.

Then, only when the host has sufficient measured capacity, run a bounded
disposable staging experiment with the exact root-managed boundary. Observe
kernel cgroup membership for each daemon, containerd, shim, executor and app,
test attempted escape and outside-UID socket access, close/drain accepted work,
test guard lifetime through a real held-job CAS, and inject a late writer and
interruption. Preserve exact evidence and verify all task-owned processes exit
and all temporary limits are restored. Do not stop shared services or treat
synthetic probes, a stopped container, or empty scope as a real effect outcome.

Existing `daemon_scope_observation` explicitly records
`effect_outcome_proven=false`; its launcher currently closes the scope handle.
The above retained guard, complete independent worker enrollment and safe
queue-release composition do not exist yet.
