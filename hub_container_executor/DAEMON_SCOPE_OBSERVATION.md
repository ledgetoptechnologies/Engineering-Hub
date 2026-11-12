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
