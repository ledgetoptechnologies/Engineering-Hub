# Retained local TEST pair operator

`python -I -m hub_container_executor.retained_pair_cli --operator-file <absolute-path> --execute`
owns one explicitly enrolled executor, coordinator and GitHub broker in the
foreground. The command installs nothing and has no restart or adoption path.
It requires a reviewed Linux root environment, independently pinned releases,
disjoint daemon and storage, a root-controlled broker cgroup and the existing
protected enrollment files. It is source-level tooling; synthetic tests do not
establish a staging host's custody or readiness.

The operator supplies an already bound, listening Unix stream socket as an
inherited descriptor. A trusted root setup must prepare that socket and the
independent broker enrollment with its exact descriptor and named-socket
identities; the entrypoint does not create or adopt the socket. The inherited
descriptor is transferred to this process and must remain open across exec.
The broker enrollment and coordinator profile independently validate it before
the broker starts. Account, cgroup, listener and key provisioning are separate
host operations and are not performed by this command.

The root-owned mode0600 operator file in a root-owned mode0700 parent is
canonical JSON with exactly these fields:

```json
{
  "schema": "engineering-hub.retained-pair-operator.v1",
  "executor_enrollment": "/root/private/executor-enrollment.json",
  "executor_key": "/root/private/executor-signing.key",
  "executor_journal": "/root/private/executor-journal.sqlite3",
  "held_ranges": "/root/private/held-ranges.json",
  "handoff_file": "/root/private/fresh-handoff.json",
  "broker_listener_fd": 3,
  "report_wait_seconds": 300,
  "run_wait_seconds": 1800,
  "executor_stop_timeout_ms": 120000,
  "broker_stop_seconds": 30
}
```

Paths and values above are examples, not staging approvals. The handoff path
must not exist when the command starts. Without `--execute`, the command only
checks argument syntax and reports `not_executed`; it reads no enrollment.
With `--execute`, it launches one executor and reports `executor_ready` with
its owner PID and original launch/process/container identity. The same process
serves executor observations while a separate operator independently verifies
the current machine reservation, inventory, writer exclusion and original
launch, and prepares the fresh manual session and root-pinned pair policy.

Only then does the operator create the root-owned mode0600 handoff file in its
existing root-owned mode0700 parent and send SIGUSR1 to the reported owner PID.
SIGUSR1 means the file is ready; it is not approval. The handoff is canonical
JSON with exactly these fields:

```json
{
  "schema": "engineering-hub.retained-pair-handoff.v1",
  "pair_policy": "/root/private/pair-policy.json",
  "config_file": "/root/private/coordinator-config-copy.json",
  "session_file": "/root/private/fresh-manual-session.json",
  "admission_file": "/root/private/coordinator-admission-copy.json",
  "coordinator_enrollment": "/root/private/coordinator-enrollment.json",
  "coordinator_key": "/root/private/coordinator-signing.key",
  "coordinator_journal": "/root/private/coordinator-journal.sqlite3",
  "held_ranges": "/root/private/coordinator-held-ranges.json",
  "broker_enrollment": "/root/private/broker-enrollment.json"
}
```

The three input copies are root-owned mode0600 files; their bytes must match
the separately mounted coordinator inputs, which are owned by the enrolled
mapped coordinator account and are checked by the supervisor. The broker enrollment is a
root-owned mode0644 regular file in a protected root-owned mode0700 parent, as
required by the broker. Keys and TEST secret values never appear in either JSON
file. The pair policy, release, signed profiles, actual executor endpoint and
manual report retain their existing independent checks; no signal or file
presence creates those facts.

After validation, the owner starts the broker and coordinator once and reports
`pair_running`. SIGINT, SIGTERM or SIGHUP then requests the authenticated local
coordinator drain, broker drain, executor seal and post-drain observation in
that order. `stopped` means those local processes stopped; it is not a clean or
released-effects claim. The executor supervisor's journal named by
`executor_journal` retains the raw pair outcome for independent inspection.
That observation is not proof that an app or external writer is quiescent, nor
permission to release or reuse state.

Before pair startup, a stop signal or report timeout holds the original
executor rather than initiating a new pair. After startup, timeout, failed
verification, uncertain effects or a lost reply also report `held` where
possible. The owner intentionally remains in the foreground indefinitely,
retaining its process handles and descriptors; further signals do not perform
an automatic retry, kill, adoption or cleanup. An operator must use a separately
reviewed recovery procedure for the exact original held processes and journals.
Killing the owner loses in-memory authority and does not itself establish that
the managed processes or their effects have stopped.
