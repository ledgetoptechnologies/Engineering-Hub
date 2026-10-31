# Local native Docker test components

These are **offline, test-only components**, not a runnable real-Docker pilot or
staging isolation. The scoped broker assumes an exclusive trusted local test
operator and daemon; it does not protect against another daemon client or
prove that a host pathname remains the same inode when Docker mounts it.

`hub_local_test_volume.py` reserves a durable intent before creating a unique
task-labeled volume and verifies its exact daemon-reported mountpoint. Unknown
creation or deletion outcomes remain held and are never retried. The broker
accepts exact pre-enrolled image identities, container profiles, names and
compiled probe requests. It journals each mutation before forwarding, records
only verified returned IDs, and refuses new effects after ambiguity. Its
explicit seal drains its serial request boundary and closes further admission;
cleanup then requires the joined listener and exact owned identities. Volume
removal separately rechecks the absence of those identities. The cleanup
observations are trusted in-process fixture facts, not host-wide quiescence
certificates. Created-but-not-started containers are conservatively retained.

`hub_local_native_binding.py` derives exact task names and state-generation
identities from an already admitted native queue run. Its read-only snapshot is
not a grant for host effects. The connected offline test prepares exact OCI
fixture graphs with explicitly synthetic build assertions. A dedicated UID/GID
10001 child holds its own PilotOwner while it creates and starts the baseline
through the source broker, freezes its SQLite state and runs the actual
BaselineObserver. The coordinator pins that observed draft, seals the source
broker and transfers the exact baseline ID to the successor broker. Native
events, queue, workflow, HostAdapter, SQLite generation copying and the actual
DockerEngineBackend then exercise candidate failure and healthy rollback over
broker Unix sockets against one **synthetic** Engine. This test uses legacy
local reconciliation and does not execute an application container or prove
daemon, image provenance, protected-owner recovery, or staging custody.

There is **no real-daemon opt-in runner yet**. Before one can be reviewed, it
still needs verified real OCI archives and independently supplied exact manifest
digests for both test images, an actual healthy task-owned application baseline
and frozen backup, and an operator-owned lifecycle connecting those real
resources to native admission. Aggregate budgets for the daemon, broker,
controller, application containers and storage also need verification.
The existing archive helper requires an independently supplied exact manifest
digest; a local Docker image ID is not that digest. A lost effect or any
unverified cleanup retains the task resources for explicit inspection. Do not
point these components at a shared daemon until the complete fixture and its
ownership/cleanup handling have been independently reviewed.
