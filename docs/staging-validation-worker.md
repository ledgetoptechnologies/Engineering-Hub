# Explicit staging validation worker

This optional source implements one already-claimed, explicitly enrolled staging
validation job. It is **not a deployed HTTP worker**, autonomous polling service,
general shell runner, application deployment driver, or production executor.
Importing the package starts nothing. No worker is activated by this document.

The queue's production-first ordering is scheduling priority, not production
approval. The default queue has no production approval provider. This worker
accepts only the `staging` environment; it cannot execute a production job even
if another trusted component approves that job for scheduling.

## Local composition and interfaces

The source is `hub_validation_worker/core.py`. Trusted local composition must
provide the following exact objects:

| Object | Fields or constructor |
| --- | --- |
| `ValidationProfile` | `uid=10001`, `gid=10001`, `memory_bytes=1073741824`, `cpu_millicores=1000`, `pids_limit=128`, `tmpfs_bytes=134217728` |
| `ValidationEnrollment` | `action_key`, `project`, `machine_id`, `source_commit`, `artifact_digest`, `image_config_id`, `platform`, `argv`, `profile`, `timeout_seconds` |
| `ValidationJob` | `job_id`, `action_key`, `project`, `machine_id`, `environment`, `target: WorkTarget`, `worker: WorkWorker`, `fence` |
| `ValidationObservation` | `container_id`, `image_config_id`, `profile_digest`, `exit_code`, `quiescent` |
| `ValidationLedger` | `(path, *, create, allow_windows_test_storage=False)` |
| `ValidationWorker` | `(queue, ledger, backend, enrollments, *, monotonic=time.monotonic)` |

`enrollments` is a bounded tuple of frozen `ValidationEnrollment` objects, with
one entry per action key. `argv` is an immutable tuple selected in trusted local
composition, never supplied by a queue job. The platform is explicitly
`linux/amd64` or `linux/arm64`. The image config ID and artifact are exact SHA-256
identities; mutable tags are not enrollment. The source commit is exact lowercase
40-character hexadecimal. Enrollment is an operator trust anchor: copying an
agent's claimed commit, an image label, or a self-authored attestation into it
does **not** establish reviewed source/build provenance.

The profile fixes non-root UID/GID 10001, no networking or published ports, no
host mounts, a read-only root, all capabilities dropped, and no-new-privileges.
Memory, CPU, PID and temporary-storage limits remain bounded. The concrete
backend must inspect what actually ran; a requested profile alone is not proof.

The backend protocol is:

```python
execute(enrollment, job, intent_id, *, deadline_monotonic) -> ValidationObservation
```

It must enforce the absolute deadline and independently observe the exact
container, immutable image, effective profile, exit status and quiescence.
Killing or timing out a client process does not prove the container stopped.
Exceptions, mismatches, non-quiescence and uncertain effects retain the machine
reservation rather than authorizing another job.

Before constructing the worker, trusted composition must explicitly install
`ledger.verify_completion` as the same queue's `completion_verifier`. The worker
requires that actual bound method, not a callback that accepts worker assertions.
The normal configured queue also requires explicit server-side admission:
`claim_verifier(worker, action_key, environment, machine_id, target: WorkTarget)`
must return the literal `True` for the exact enrolled task/commit/artifact. Its
default closed policy cannot reserve new work. Scope this policy to the
trusted worker composition and receipt provider; never copy agent request flags
into it. Generic explicitly injected test queues are trusted composition, not
evidence that an actual remote executor has been enrolled. Caller `action_keys`
can narrow but never broaden this policy.
Its one-shot entry point is:

```python
worker.run_once(job_id=job_id, worker=WorkWorker(agent, instance_id), fence=fence)
```

This is an interface illustration, not an activation recipe. `agent` comes from
the authenticated identity; the canonical UUID instance identifies the particular
cooperating client. An instance ID is not an additional authenticated identity.
The worker requires an existing live claim and never claims arbitrary jobs itself.

`timeout_seconds` is an integer from 1 through 300. At admission, the remaining
claim lease must be at least `timeout_seconds + 10` seconds. A lease's maximum is
still the queue's 600 seconds. The worker rechecks the exact live claim, fencing,
pause, registry and current reviewed target before effects and before accepting
completion. It does not extend its authority after expiration.

## Receipt and recovery boundary

The ledger is a **dedicated local database**, not the Hub database. On Linux its
parent must already be owned by the executor UID with mode `0700`; the database
must be a single-link regular file owned by that UID with mode `0600`. It checks
path/inode identities again on use. `create=True` exclusively creates a new file;
`create=False` requires the existing receipt schema. It does not adopt an
unrelated database or silently initialize an existing file.

The Windows test-storage flag is solely for explicitly labeled local tests. It
does not demonstrate Linux private-storage controls or make that storage a
production trust boundary. The private-directory model does not defend against
a hostile administrator using the same executor UID.

The optional Docker CLI backend takes an explicit executable, configuration
directory, context and expected daemon identity. No ambient Docker endpoint or
token is discovered. Containerd-backed Engines additionally need
`DockerImageSelection.from_oci(index_bytes, manifest_bytes, platform=...)` from
the original bounded OCI bytes: it checks the index-to-selected-platform-manifest
to-enrolled-config content chain. The submitted artifact must equal the immutable
index reference. Classic config-ID selection requires artifact/config equality.
Actual Engine image and selected-manifest descriptors are checked independently;
the backend never silently reinterprets an index as an image config digest.

`scripts/hub_validation_local_smoke.py` is an explicit disposable local acceptance
harness, not a worker service. It accepts a preloaded clean-source validation
image and original local OCI archive, uses synthetic reviews and operator
enrollment, and tests fixed successful validation plus a known nonzero exit.
No live token or Hub database is used. It retains the small synthetic receipt
databases even on failure; only exact receipt-bound, verified stopped containers
are removed without force. A Windows-driven Engine test remains test-only
storage evidence, not acceptance of Linux private storage or the remote host.

The worker commits a durable intent before any backend effect. No SQLite writer
transaction is held over backend execution. The intent binds the full job,
target, instance, fence and frozen enrollment. An existing intent is never
interpreted as permission to replay an effect after a crash or retry.

Only independently observed terminal, quiescent execution can produce a terminal
receipt. Exit code zero means validation succeeded; a verified nonzero exit means
validation failed. The completion verifier rereads the private receipt and binds
its evidence ID to the exact job, worker, fence, action, machine, target, enrolled
image and profile. Unknown outcomes remain held. A validation result does not
promote a task, create a deployment run or approve production.

The queue's `reconciliation_verifier` is a separate privileged composition
dependency. It must not be replaced by an agent's assertion that an unknown
process has stopped. Lease expiration alone does not release a machine.

## Remote staging is not implemented by this local core

### Candidate private service composition

`OwnedValidationService` now composes the existing local queue, private receipt
ledger, native event feed and bounded worker. This is an explicit Python API,
not an installed service, public enrollment endpoint or production entrypoint.
Its fixed `executor:software-validation` identity is not an agent identity and
cannot be selected through the authenticated agent API. Public packaged queue
admission remains closed; only trusted local composition binds an exact reviewed
task, source commit, artifact and machine to the executor.

The Linux owner holds a separate private local lock across its lifetime. Native
invalidation subscribes before the initial durable queue drain. There is no
timer-based inbox scan. Source failure closes admission; shutdown waits for
accepted backend work before releasing ownership. Each new owner has a fresh
instance ID. Old uncertain claims remain held instead of being automatically
adopted or replayed. The composition rejects container-pilot and legacy-dispatch
ledgers so it cannot silently become a competing controller.

The Hub database must remain local and unchanged in identity while the service
runs. The receipt ledger lives in the owner's separate private state directory.
The configured supervisor stop grace must exceed the longest enrolled validation
timeout by at least 30 seconds. That value is an operator assertion, not proof
that an installed supervisor honors it; actual installation/reboot acceptance
and authoritative reconciliation of held work remain separate requirements.

`ValidationWorker` takes the actual local `WorkQueue` object. It is **not** a
client for the Hub HTTP endpoints. The authenticated work-queue endpoints provide
coordination and leases, but do not turn an arbitrary remote agent into this
executor or provide a remote trusted receipt-write interface.

Putting the Hub SQLite database on SMB and opening it from the staging PC is
explicitly not the solution. Keep the control database local to its owning
service. A remote deployment needs an authenticated execution adapter, a
host-owned private receipt store, independently verified completion/quiescence,
and a correctly integrated machine-reservation boundary. That remote adapter and
its deployment/credential setup are not implemented by this local worker core.
Legacy dispatch cannot run concurrently with the queue until both participate
in the same reservation authority.

### Shared-host activation gate

Claude's linked review `b5e541c9-69f4-47ae-87e0-06c1a50c001b` identifies a
separate boundary: the existing staging daemon also hosts Viewer and PA. A
cooperative sidecar lock and container-label allowlist do not exclude another
deployer or restrict what a privileged Docker connection could change.
Do not activate this executor against that shared daemon on the strength of
the source tests or the earlier read-only SSH inspection.

Before host provisioning, inventory existing CI/manual deploy writers and agree
their exclusion boundary. Prefer a dedicated validation daemon, rootless context
or VM with no access to the existing applications' daemon, mounts or credentials.
That boundary also needs host-level resource admission: a separate daemon alone
does not reserve the physical machine's CPU, memory or storage. Do not treat a
forced Docker command or a label-only proxy as proven resource authorization.

Any approved remote credential must be dedicated, separately revocable and
stored only in the private executor, with pinned host identity, constrained
source/commands where supported, and documented rotation and recovery. The
public Hub must not receive it. Actual effective transport permissions and
writer exclusion need verification before a real staging pilot. No provisioning
or credential approval is implied by this design checkpoint.

## What the verification establishes

* SQLite/queue unit tests use a **mocked backend**. They test intent-before-effect,
  fencing, failure/hold behavior, receipt binding and process-crash durability;
  they do not prove real container execution or isolation.
* Linux test execution can additionally verify real POSIX private permissions.
  A green Linux mock suite is still not a real Docker pilot.
* The actual-HTTP coordination smoke is explicitly coordination-only. Its
  synthetic production approval is not production authorization; it exercises
  no host commands and creates no deployment runs.
* A real Docker validation pilot must separately prove immutable source/image
  enrollment, actual effective isolation, real exit/quiescence evidence and
  queue completion through the private verifier. That acceptance is pending
  unless a separately recorded actual-engine result demonstrates it.
* TrueNAS deployment, remote staging connectivity, application rollback, human
  sign-in and restart/reboot recovery require their own acceptance. None is
  established by these source-level or coordination tests.

No NAS administration instructions, service installation, generic command
submission endpoint or worker startup command are provided here.
