# Container-pilot host adapter source (pending independent review)

This is a separately scoped source candidate. It consumes the shared
`engineering_hub.container_pilot_manifest.PilotManifest` and typed request,
response, and evidence classes directly. It does not fork their schema and does
not enable itself from import, environment, Hub startup, or a listener.

## Implemented source boundary

- `ledger.py`: private local SQLite admission/run/target/phase/operation records,
  stable payload replay, target/fence reservation, exact phase ordering, pause
  checks before each effect intent, a separate private HMAC key for operation
  receipts (outside app mounts), lifetime `flock` ownership before orphan
  recovery, and held recovery after an unclean shutdown. Lock/database/key inodes
  are not replaced or unlinked; Linux mountinfo excludes network filesystems.
- `build_record.py`: closed bounded record parsing using the frozen source
  contract, original metadata bytes and index→platform-manifest→config digest
  chain validation, shared `Platform`, and mandatory explicit enrolled-builder
  verifier injection. The shipped fake verifier exists only in unit tests.
- `adapter.py`: fixed profile derivation and typed no-pull/no-caller-options
  backend boundary; exact image defaults, runtime profile, actual selected image,
  baseline, state-generation and probe identity checks. It persists intents before
  calls and holds uncertain outcomes. Reconcile only asks the backend to inspect
  original operation/container IDs; it never retries, starts or re-probes.
- `state.py`: online SQLite backup into a private exclusive destination with
  integrity/size checks, plus a non-destructive explicit retention planner.
- `asgi.py`: optional bearer-authenticated ASGI factory for the frozen namespace;
  importing it does not construct an app or bind a socket.
- `docker/hub-container-pilot-acceptance.py`: import-safe, hard-disabled gate;
  it intentionally cannot run a Docker acceptance until source review and a
  separately authorized backend implementation.

## Explicit limits / pending gates

There is **no concrete Docker/daemon driver** in this source. No Docker command,
API call, listener, container, model CLI, network call, live state or credential
was used. Unit-test fake engines, fake builders, fake state provisioners and fake
secret resolvers demonstrate protocol and local ledger behavior only. They are
not daemon, provenance issuer, image-layer, health, rollback or application proof.

Still required before any real acceptance:

1. Independent source review and a separately reviewed implementation bound to
   the operator-owned/pinned daemon endpoint; actual effective profile and image
   selection evidence must be correlated to original immutable metadata.
2. Enrolled provenance verifier and protected content-addressed record/blob
   stores; operator-enrolled healthy baseline; versioned test-only secret
   resolver; isolated-runner state provisioner. Missing dependencies fail closed.
3. A private generation provider that uses the tested SQLite copy primitive,
   enforces no migrations/external writes, validates baseline immutability,
   preprovisions uid/gid 10001 access without privileged helper/chown, and applies
   reviewed retention/cleanup limits.
4. Real daemon-specific identity/profile inspection, bounded operations, fixed
   compiled in-container probes and authentic private probe evidence. No fake
   backend may be reported as acceptance.
5. Align the Root-owned client deadline (15s default / 60s ceiling) with the
   manifest's 120s maximum plus bounded transport margin. This branch does not
   edit the client or change shared models.
6. Review and execute the acceptance harness only after the above gates. No
   Docker/harness execution is part of this unit-source phase.

Run the scoped source tests from the repository root with the locked environment:

```sh
PYTHONPATH=.:src /home/bkoltz/.hermes/cache/scratch/internal-MCP-messaging/.venv/bin/python -W default -m pytest -q tests/test_hub_container_executor*.py
```
