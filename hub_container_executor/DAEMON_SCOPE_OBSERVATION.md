# Daemon scope observation

This opt-in supervisor API retains a cgroup-v2 observation; it is not a daemon
shutdown controller or a held-operation recovery mechanism. Before stopping the
supervised executor, an authorized root operator can call
`supervisor.enroll_daemon_scope(relative_scope, timeout_ms=...)` with an
independently provisioned, nondelegable scope containing exactly the already
pinned daemon process. The caller owns and must close the returned handle.

After the existing `graceful_stop()` has durably recorded the supervised
consumer's exit, an independent operator may stop the dedicated TEST daemon.
Once the retained daemon pidfd and enrolled scope show empty, call
`supervisor.record_daemon_scope_emptiness(scope)`. The existing root-private
launch journal first retains a `provisional` observation, then marks it
`postcheck_complete` only after a second scope check succeeds. Both states are
preserved by later hold updates. A provisional record does not establish that
the postcheck succeeded. Neither state alters a held operation or
start, stop, kill, adopt, or replay a daemon effect. Failure, including an
already recorded observation, is closed and requires evidence review. Scope
observations are snapshots and are not an atomic barrier against a privileged
administrator or later processes.

A daemon's process scope does not necessarily include containerd, shims,
containers, or other delegated workers. Empty scope and a stopped launcher do
not prove an interrupted Docker request completed, that all writers were fenced,
or that any effect outcome is correct. An independently enforced closure and
exact host outcome remain necessary for any future held-effect recovery. This
API must not be used to clear a hold or claim staging acceptance.

The first scope observation and the journal's `user_version` change from 1 to 2
commit in the same SQLite transaction. The previous v1 supervisor rejects v2
while acquiring the same exclusive owner lock, before it can write state. The
new supervisor reads both versions and preserves v2 observations across later
state changes; the table schema is unchanged. A v1 journal already containing
an observation from an earlier candidate is ambiguous and is rejected without
automatic migration or deletion. Preserve that journal for explicit review.
This version check is not protection against an administrator or other code
that bypasses the journal's ownership protocol and writes SQLite directly.

## Explicit launcher option

The optional `--daemon-scope-policy /absolute/root-private/policy.json` enables
this observation in the existing launcher. The operator independently creates
the canonical JSON file mode 0600 in a root-owned mode 0700 directory. It has
exactly these fields: `schema` is
`engineering-hub.daemon-scope-policy.v1`; `scope` is the relative cgroup-v2
path; `scope_device` and `scope_inode` pin its actual `stat` identity;
`daemon_pid`, `daemon_start_ticks`, `daemon_id`, and `boot_id` must exactly
match the separately enrolled live daemon; and `wait_seconds` is 1..3600.
None of these values is discovered or enrolled by the option.

The launcher checks the file, daemon, scope inode, cgroup controls and exact
live membership before launching. It pins that scope, then rechecks the same
scope and live membership after launch. It preserves the existing explicit
consumer stop.
After printing `consumer_stopped_waiting_daemon_exit`, it waits only on the
retained daemon pidfd for the specified bound. An operator must independently
stop the dedicated daemon; the launcher never signals it. A second stop signal
cancels the wait. On exact exit, the launcher rechecks the policy and scope,
records the bounded observation, and prints
`daemon_scope_observed_no_effect_outcome`. Timeout, changed policy, signal,
missing scope or uncertainty returns failure and preserves evidence. This
option proves neither all effect workers quiesced nor an operation outcome.

### Optional executor cgroup parent

An independently reviewed executor launch profile may supply
`cgroup_parent` (the exact systemd slice name passed as Docker `CgroupParent`)
and `cgroup_host_parent` (its pre-existing absolute cgroup-v2 path as seen in
host `/proc/<pid>/cgroup`), plus `cgroup_host_ns_device` and
`cgroup_host_ns_inode` identifying the independently verified host cgroup
namespace. The parent fields must name the same slice. All four fields bind
the profile digest; omitting all four preserves the earlier profile digest and
behavior. The supervisor and daemon must both be in the enrolled namespace,
and the cgroup-v2 mount must expose its hierarchy root. The supervisor holds
the parent directory FD until close, checks the named path and retained inode,
checks Docker's returned `CgroupParent`, and checks the executor PID's
momentary kernel membership is below that parent. A missing, replaced or
mismatched parent or namespace fails closed. The operator must independently
verify that the enrolled namespace is the intended host namespace; matching
an inode by itself does not establish this fact.

This does not enroll, configure, or prove an aggregate resource limit, contain
containerd/shims, or establish application-container membership. In particular,
the app backend runs in a separate PID namespace and needs an independently
trusted host-side membership observer before application containment can be
claimed. These checks provide no effect-outcome or recovery authority.

### Opt-in app membership observation

A v3 bound runner profile may independently pin `app_cgroup` with exactly
`parent`, `device`, `inode`, `namespace_device`, and `namespace_inode`. These
values must match the root launcher's separately enrolled parent and namespace.
The backend sets Docker `CgroupParent` on create. The root supervisor accepts
an app-container ID and fresh nonce only from its exact retained runner peer;
it inspects the daemon's exact container, obtains a host PID and pidfd, checks
its actual cgroup membership and parent inode, rechecks the daemon's PID, and
signs a short-lived observation bound to that container, daemon generation,
launcher generation, parent, namespace, and nonce. The runner verifies all
pins and the signature before using that observation.

The initial application start is necessarily issued before a process PID exists.
The backend checks host membership immediately after start and holds uncertain
results. It obtains fresh membership evidence before stop and each probe exec
POST, checks that evidence remains live after the independent effect guard,
and fails closed on mismatch or expiry. These are point-in-time checks, not an
atomic kernel fence. They do not prove uninterrupted containment, all worker
closure, or recovery of a held effect. The profile is opt-in; legacy profiles
retain their prior behavior. No installation or staging acceptance is implied.

### Opt-in hierarchical worker scope

`preflight_worker_scope` is a root-supervisor API for an independently
provisioned scope that includes the daemon and every explicitly inventoried
worker process. It accepts retained pidfds for the exact initial inventory and
rejects a missing, extra, or outside member. The root scope and every ancestor
remain root-controlled, while descendants may be delegated within the scope.
Kernel descendant depth and count must be finite and bounded. The reader
rechecks the exact initial inventory and uses the root's recursive
`cgroup.events` plus retained pidfds when observing emptiness. The journal marks
such an observation as `hierarchy`; it is still only a point-in-time observation.

This does not discover all relevant processes. An independent operator must
establish that user-systemd, containerd, shims, application processes and all
other effect workers are actually inside that boundary and cannot use an
outside delegation. A pre-existing outside user manager or privileged actor can
create workers the scope cannot see. The current installed launcher CLI does
not supply that complete enrollment, and this source API never clears a hold or
asserts an exact operation outcome. Do not infer a full recovery barrier from a
successful hierarchical observation alone.

### Installed hierarchical scope policy

For the hierarchical observation path, the root-private launcher policy uses
schema `engineering-hub.daemon-scope-policy.v2` with all v1 fields plus a
`members` array. Each entry has exactly `pid` and `start_ticks`; it must include
the exact daemon and every independently inventoried initial process, with no
duplicates. The launcher retains pidfds for the additional members, verifies
the complete live inventory is inside the root-controlled hierarchical scope
before launch, and rechecks it after launch. After the explicit consumer stop,
it waits for every enrolled pidfd to exit, then performs the recursive scope
observation. Any remaining descendant, changed policy, timeout, mismatch, or
second stop signal fails closed and preserves the hold. Additional pidfds are
closed before the daemon owner is released. The launcher never stops a daemon
or worker on this path.

This policy does not discover the user manager, containerd, shims or other
workers and cannot prove that a process outside the scope cannot start work.
The operator must independently enroll the complete worker set and establish
the placement and non-delegable boundary. An empty observation still proves no
individual Docker effect outcome and grants no recovery or retry authority.
