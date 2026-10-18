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
  integrity/size checks, plus a non-destructive explicit retention planner.
- `asgi.py`: optional bearer-authenticated ASGI factory for the frozen namespace;
  importing it does not construct an app or bind a socket. GET, reconcile and
  submit body reads share a configurable bounded request-body deadline, separate
  from the manifest's command budget, and bodyless routes consume and reject bytes.
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
