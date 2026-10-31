# Explicit trusted local coordinator

This entrypoint is for a separately approved, disposable Linux staging VM. It is
launched independently as PID 1; it is not accepted by the current closed
coordinator supervisor profile and does not provide supervisor identity for the
coordinator. It does not install a service or create operator evidence.

The operator must exclusively control the VM and every account able to mutate
the private executor socket directory, and must prevent executor/socket
replacement until the coordinator drains. The observation and command clients
use separate connections; inode checks do not atomically bind them. These are
host prerequisites, not properties established by this code.

Provision the existing immutable coordinator config and an independently
captured, fresh disjoint-v3 manual-session enrollment. The enrollment is operator
attestation data, not a signature or automated proof of inventory and writer
exclusion. It must be a current-UID mode-0600 regular file under a trusted,
current-UID mode-0700 parent outside the database and secret trees. The process
must run as UID/GID 10001 and PID 1; the config must satisfy the existing
read-only mount and identity checks. The executor must already be independently
enrolled and running. Human acceptance and current signed executor observations
remain required by the existing reconciliation gate.

Use manual-session schema v2 with the default Cloudflare coordinator config;
the opt-in GitHub coordinator schema v2 requires manual-session schema v3 and
the matching numeric GitHub operator. Both the signed observation and command
factory use the same configured Unix socket and captured executor credential,
subject to the no-replacement host prerequisite above.

The signed profile parser recognizes the separate
`trusted-local-coordinator-v1` role, with a fixed entrypoint, mapped UID/GID,
and closed, separately mounted config, enrollment, database, secret and socket
directories. The pure configuration commitment checks the v2 GitHub config,
v3 enrollment, paths and file digests together. **The supervisor still rejects
this role before creating its journal or contacting the Engine.** This profile
is not an installed launch path: the supervisor has a role-specific protected
config/enrollment capture and cross-check, but complete mount custody,
pre-admission handshake and coordinator/executor lifecycle binding still
need to be implemented and verified before that guard can be reconsidered.

Run the following as the independently launched container's PID 1, using paths
that already exist inside its approved mounts:

```text
/opt/venv/bin/python -I -m hub_container_executor.trusted_local_session --config /operator/coordinator.json --enrollment /operator-evidence/session.json
```

Use a freshly built and independently approved core/executor artifact containing
these modules. This source entrypoint does not modify a host, provide reboot
recovery, or authorize use of any retained held execution.
