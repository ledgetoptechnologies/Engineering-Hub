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
through the source broker. In the offline fixture the synthetic Engine creates
the initial database in an empty generation. The child verifies fixed
preflight probes, stops and checks the exited container has no PID, freezes its
SQLite state, and restarts it once before running the actual BaselineObserver.
The observer still requires the
source inventory to remain unchanged; actual application restart behavior has
not been validated. The coordinator pins that observed draft, seals the source
broker and transfers the exact baseline ID to the successor broker. Native
events, queue, workflow, HostAdapter, SQLite generation copying and the actual
DockerEngineBackend then exercise candidate failure and healthy rollback over
broker Unix sockets against one **synthetic** Engine. This test uses legacy
local reconciliation and does not execute an application container or prove
daemon, image provenance, protected-owner recovery, or staging custody.

`hub_local_baseline_child.py` is the fixed bounded IPC for that child; its
listener is selected by a trusted parent and its path checks do not authenticate
the listener. `hub_local_native_coordinator.py` and
`hub_local_acceptance_components.py` own the connected offline queue and broker
lifecycles. Their cleanup association is an in-process fixture boundary, not a
portable certificate. The optional quiescent baseline path admits no startup
grace retries: failed or uncertain operations remain held. The initializer and
cached-image discovery have offline tests only; neither has been exercised
against an actual daemon in this fixture.

The fixed-FD coordinator and launcher are **test-only candidates**, not yet
accepted for real-daemon use. They require independently supplied exact OCI
manifest digests already resolvable on the pinned local daemon, a bounded
512MiB/4096-inode tmpfs task volume, a separately bounded private scratch mount,
and a task-owned keeper that remains mounted until receipt authentication.
The explicit v2 launcher can pin an already-authorized socket supplementary
group for its task containers; it does not change host socket permissions or
account group membership. This grants those containers daemon capability and
is suitable only under the stated trusted-local-operator assumption.
The Windows parent request must also include the independently observed
`operator_socket_source` for its Desktop socket proxy. The fixed operator
requests private propagation and checks the created container's requested and
effective bind metadata; a mismatch remains held. Before launching the inner
fixture, its bootstrap checks the daemon ID through the mounted socket. This
does not establish exclusive daemon access or host custody.
The coordinator writes an authenticated post-exit cleanup handoff only after
its native work and exact container cleanup have drained; the launcher must
independently verify coordinator removal and exact container absence before it
removes the volume. A held or ambiguous operation retains the keeper and
resources for inspection. Docker Desktop path mapping, socket access, actual
Engine formatting and the application's post-restart source inventory remain
unverified for the full workflow. The shared daemon and image store are not
subject to the task volume cap, so their aggregate resource headroom must be independently
established before any real run.
The coordinator shares a 4GiB archive-response budget between its serial source
and successor brokers; this does not bound daemon read-ahead or CPU. The broker
requires a local log driver capped at two 1MiB files per application container.
The holder has bounded resources but deliberately remains alive on an uncertain
outcome until an operator inspects the original task and resolves cleanup.
The existing archive helper requires an independently supplied exact manifest
digest; a local Docker image ID is not that digest. A lost effect or any
unverified cleanup retains the task resources for explicit inspection. Do not
point these components at a shared daemon until the complete fixture and its
ownership/cleanup handling have been independently reviewed.

`hub_local_operator_storage.prepare_source` prepares a source tree from an
uncompressed Git archive whose SHA-256 and commit association are independently
established by the caller. It requires exclusive control of the archive and
output parent, creates a new bounded tree with read-only modes, and returns the
tree digest consumed by the launcher. Its focused tests use synthetic archives.
On Windows, create the archive with `git -c core.autocrlf=false -c core.eol=lf archive` and
verify that its fixed seed member matches the initializer's SHA-256 pin. The
default `git archive` can otherwise apply the workstation's CRLF conversion.
The Windows parent instead seeds task-owned named volumes from independently
pinned archive and Docker CLI bytes. A fixed helper extracts the archive, then
a separate read-only helper measures the tree before the operator can start.
The parent journals one-shot effects, retains uncertain resources, and checks
the exact operator and inner task have gone before its storage cleanup. It
retains the journal volume as evidence. File fsync provides process-crash
evidence; Windows directory-entry durability after power loss is not proven.

Local probes have confirmed current read-only aliases for empty ordinary named
volumes and writable aliases for a bounded tmpfs while its keeper is live.
Those probes do not establish the full operator path, nested socket binding,
source provenance, post-restart database inventory, or native rollback. The
Windows parent and cleanup have only synthetic transport tests; do not treat
them as acceptance of a shared-daemon run. The CLI digest pins observed local
bytes and is not an independently verified publisher checksum. All of these
tools assume an exclusive, trusted local operator and daemon.
