# Container-pilot host adapter source (live acceptance pending)

This is a separately scoped source candidate. It consumes the shared
`engineering_hub.container_pilot_manifest.PilotManifest` and typed request,
response, and evidence classes directly. It does not fork their schema and does
not enable itself from import, environment, Hub startup, or a listener.

## Implemented source boundary

- `ledger.py`: private local SQLite admission/run/target/phase/operation records,
  stable payload replay, target/fence reservation, exact phase ordering, pause
  checks before each effect intent, a separate private HMAC key for operation
  receipts (outside app mounts), creator-PID binding, connection/close
  serialization, lifetime `flock` ownership before orphan recovery, and held
  recovery after an unclean shutdown. Inherited child use is rejected before
  touching SQLite or a possibly inherited mutex; child close only drops its FD
  copy. Lock/database/key inodes are not replaced or unlinked; Linux mountinfo
  excludes network filesystems.
- `build_record.py`: closed bounded record parsing using the frozen source
  contract, original metadata bytes and index→platform-manifest→config digest
  chain validation, shared `Platform`, and mandatory explicit enrolled-builder
  verifier injection. The shipped fake verifier exists only in unit tests.
- `build_enrollment.py`: an immutable exact manifest/role/record/builder/invocation
  allowlist supplied by trusted local composition. Construction is privileged
  operator enrollment, never an API operation. It is not signature verification
  and does not independently establish that any real build has been approved.
- `adapter.py`: fixed profile derivation and typed no-pull/no-caller-options
  backend boundary; exact image defaults, runtime profile, actual selected image,
  baseline, state-generation and probe identity checks. It persists intents before
  calls and holds uncertain outcomes. An independent trusted runtime-identity
  verifier must match the manifest's driver commit before runner inspection or any
  effect; missing providers hold unavailable. The adapter serializes close against
  submit/reconcile and rejects new work after shutdown begins. Reconcile only asks
  the backend to inspect original operation/container IDs; it never retries,
  starts or re-probes. State providers receive one cooperative monotonic command
  deadline; a returned overrun remains held with its intent incomplete.
- `state.py`: online SQLite backup into a private exclusive destination with
  integrity/size checks, cooperative deadline checks through VM/backup progress,
  hashing and surrounding I/O, plus a non-destructive retention planner. Error
  cleanup removes only the newly created matching inode; blocking OS I/O cannot
  be forcibly cancelled by this in-process primitive.
- `baseline_snapshot.py`: explicit first-baseline snapshot preparation in a
  dedicated Linux operator process, not the Hub service. It preserves the
  original database/WAL/SHM inventory, normalizes only its own temporary backup,
  and publishes a frozen artifact without replacing an existing leaf. Process,
  private-storage and source-inventory checks fail closed. This prepares bytes;
  it does not prove baseline health or authorize a rollout.
- `baseline_receipt.py`: read-only `BaselineProvider` implementation over an
  independently pinned private receipt, campaign, verified rollback image and
  private runtime-profile digest. It revalidates the original protected bytes
  and full manifest bindings on every call, exports the bound immutable file
  inventory and rejects future-dated observations. The receipt describes
  historical enrollment, never current health. Actual observed enrollment
  and an independent operator pinning workflow are still required; a caller-authored
  healthy flag plus its hash is not adequate authority.
- `baseline_observer.py`: one-attempt, dedicated-process observation source for
  a prepared isolated baseline. It checks the pinned runner, immutable image,
  effective container profile, frozen backup and unchanged source inventory;
  fixed probes must have an exact terminal private-journal receipt. It returns
  private draft bytes only, never publishes, pins, activates or retries them.
  Tests simulate Engine methods; real daemon observation remains unverified.
- `runtime_profile.py`: pure shared profile/probe derivation used by the adapter
  and observer. Runtime-profile and probe-set digests remain distinct.
  Profile inputs contain synthetic test secrets; never serialize or log them.
- `state_provider.py`: explicit private, independently enrolled frozen-baseline
  generation provider for one bounded pilot. Candidate admission reserves count
  and byte headroom for the same run's rollback before writing intent. Durable
  reservations survive reopening; interrupted/foreign state holds without
  replay, automatic adoption or deletion. Existing baseline bytes are checked,
  and real UID/GID10001 must already own the private storage. Admission accounting
  is not an OS disk quota: runtime application growth can consume space. A
  successful candidate retains its rollback reservation; continuous campaigns
  need a separately reviewed release/retention lifecycle. Plain Hub target state
  is supported, not an enabled OwnedPilotService's inode-bound ownership data.
- `asgi.py`: optional bearer-authenticated ASGI factory for the frozen namespace;
  importing it does not construct an app or bind a socket. GET, reconcile and
  submit body reads share a configurable bounded request-body deadline, separate
  from the manifest's command budget, and bodyless routes consume and reject bytes.
  Synchronous adapter operations run off the request event loop behind one
  non-waiting admission gate. Other valid operations receive `503 command_busy`
  rather than building an unbounded executor queue. This includes inspection:
  the adapter may hold its private ledger transaction during an operation, so
  an inspection must not consume another blocked thread. Authentication and body
  deadlines remain responsive. Repeated request cancellation drains the accepted
  raw executor future before releasing the gate; it is not cancellation of the
  container or proof of quiescence. A forked app cannot inherit this authority.
  Any uncertain response still requires inspection of the original command;
  callers must not interpret a transport failure as permission to replay a POST.
- `daemon_backend.py`: concrete operator-pinned Unix Engine HTTP driver, verified
  only with fake transports and synthetic OCI archives. Its tmpfs/hard-link
  corrections passed independent source review; see `DAEMON_SOURCE.md` for
  unsupported identity paths, explicit private-cgroup profile and remaining
  mutable-generation/live acceptance gates.
- `docker/hub-container-pilot-acceptance.py`: import-safe, hard-disabled gate;
  a concrete acceptance harness and separate live-execution approval are still
  required. This script is not an implemented acceptance runner.

## Explicit limits / pending gates

The concrete daemon driver is present, but **no real daemon was exercised through
this driver**. Isolated Docker validation containers execute the unit suite; that
does not exercise the driver's Engine API, open a daemon socket from the test,
or start an executor service. Unit-test fake engines, fake builders, fake state
provisioners and fake secret resolvers demonstrate protocol and local ledger
behavior only. They are not daemon, provenance issuer, image-layer, health,
rollback or application proof, and use no live credentials or application state.

Still required before any real acceptance:

1. Source corrections were independently reviewed; that does not establish live
   daemon compatibility. Actual effective profile and image selection must be
   correlated to original immutable metadata on the operator-pinned daemon.
   Shared private cgroup namespace policy is required in profile derivation,
   explicit create configuration and effective inspect; API versions below 1.41
   are denied before I/O. These source checks are not live isolation proof.
   Mutable-generation exclusion through bind establishment
   remains an operator/provider gate, not a guarantee from a path walk.
2. Enrolled provenance verifier and protected content-addressed record/blob
   stores; operator-enrolled healthy baseline; versioned test-only secret
   resolver; isolated-runner state provisioner; and an independently trusted
   `TrustedDriverIdentityVerifier` backed by immutable executable/provenance
   identity (not a backend echo or label). The interface is source-only and no
   concrete driver-identity provider is supplied; without one, effects hold.
   Missing dependencies fail closed.
   The receipt reader, snapshot helper and observer source do not supply actual
   daemon evidence, protected receipt publication or independent pinning. The
   observer requires an explicit live owner guard and pre-existing isolated
   baseline; it does not provision these or authenticate an invented receipt.
3. A private generation provider that uses the tested SQLite copy primitive,
   enforces no migrations/external writes, validates baseline immutability,
   preprovisions uid/gid 10001 access without privileged helper/chown, and applies
   reviewed retention/cleanup limits. It must cooperatively honor the absolute
   monotonic command deadline; an in-process blocking Python call cannot be
   safely killed, so a deadline overrun remains held and owner-locked until return.
4. Real daemon-specific identity/profile inspection, bounded operations, fixed
   compiled in-container probes and authentic private probe evidence. No fake
   backend may be reported as acceptance.
5. Preserve the Root-owned client's aligned 125s default/ceiling, covering the
   manifest's 120s maximum plus a 5s transport margin. Real host deadline and
   shutdown behavior still require acceptance. This patch does not edit the
   client or change shared models.
6. Review and execute the acceptance harness only after the above gates. No
   Docker/harness execution is part of this unit-source phase.

Read-only local implementations of content-addressed record/blob storage and
version-pinned synthetic test secrets are available in `local_providers.py`.
They require explicitly provisioned private Linux directories and do not supply
trusted build approval, executable identity, baseline or state enrollment.
Neither is wired into a running executor by importing it.

Run the scoped source tests on Linux from the repository root with the locked
environment (or use the isolated validation image):

```sh
uv run --locked python -W default -m pytest -q tests/test_hub_container_executor*.py
```
