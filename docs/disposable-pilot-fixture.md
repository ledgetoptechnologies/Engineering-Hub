# Disposable pilot: trusted single-executor fixture and full-workflow test

Status: assessment for Codex. Changes no live service. Creates no credential.

## Boundary (what the fixture assumes and does not test)

The immediate pilot runs inside a **documented trusted boundary**:
- **One executor.** It is the only process that holds the command route and the engine socket. It is trusted code built from the reviewed commit. No same-UID attacker, relay or descriptor delegation is in scope; those are the subject of `per-pc-staging-admission.md` §8.x to §8.w and remain open.
- **A disposable Docker engine** used for nothing else, created and destroyed by the test. Not the staging pilot engine, the default engine or the CI engine.
- **Synthetic identities.** Supervisor, issuer and Hub keys are generated per run inside the fixture and discarded. No enrolled or production key, token or certificate is read or created outside the fixture.
- **Legacy agent actors only** (`codex`, `claude`). Per-PC installations stay unsupported, as they are today.
- **No NAS, Cloudflare, GHCR publish, production Compose or registry write.** Images are built locally inside the disposable engine.

Every report from this fixture must carry the label "trusted single-executor fixture: not host, isolation or revocation acceptance".

## What exists and what is missing

Existing tests cover each layer with something simulated:
- `test_hub_queued_executor_service_integration.py`: real TCP and UDS, controller, host adapter, ledgers and queue; build, baseline, state, daemon and health providers synthetic.
- `test_shared_pilot_modular_integration.py`: real owner, ledger, queue and controllers; remote effects simulated.
- `test_hub_container_executor_daemon_backend.py`: a fake Docker transcript.
- `test_hub_executor_image_smoke.py`: synthetic CLI output.
- `test_hub_container_executor_launch_supervisor.py`: real pidfd, `SO_PEERCRED` and HTTP framing; synthetic engine effects.
- `test_hub_pilot_recovery_sigkill.py`: real process death with simulated receipts.

**No test drives the real `daemon_backend` against a real engine, and none runs the whole flow in one piece.**

## Proposed full-workflow fixture test

`tests/test_disposable_pilot_full_workflow.py`. It runs only when explicitly opted in, `EH_DISPOSABLE_ENGINE=1`, and is skipped otherwise. It runs in a disposable Linux container that starts its own rootless dockerd, and never uses a host engine.

1. **Engine.** Start rootless `dockerd` on a temporary socket inside the test container. Record its identity the way `daemon_backend` expects.
2. **Images.** Build two tiny images from fixed contexts: a candidate and a rollback, each a static HTTP health endpoint, the candidate configurable to fail. Record the config IDs computed by `build_record.py`, not taken from labels.
3. **Hub.** `create_app` with temporary settings and real `HubRepository`, `WorkQueue`, `QueuedDevelopmentCI` and `QueuedContainerPilot`. The task is created and claimed by `codex` and reviewed by `claude`.
4. **CI.** A provider that runs the repository's fixed test command inside the disposable engine against the reviewed commit's archive. It records the verified attempt and, after the CI-to-image work lands, the build record binding commit to config ID.
5. **Pilot.** The real `ContainerPilotClient` connects over a private UDS to the real `hub_container_executor` ASGI service, which uses the real `daemon_backend` against the disposable engine and the same pinned manifest roles.
6. **Run three times:**
   - healthy candidate: deploy, smoke and release;
   - unhealthy candidate: deploy, smoke fails, rollback, rollback smoke;
   - the executor process sent `SIGKILL` between deploy and smoke, then restarted: the job is held, `recover` inspects and does not redispatch.
7. **Evidence.** Read `GET /api/hub/v1/container-pilot/runs/{id}/release-evidence` and assert the step states, the reported and expected config IDs, `independently_verified=false`, and that the release-evidence parser in hub-ui accepts the body (the fixture JSON is shared).
8. **Teardown.** Remove containers and images in the disposable engine only, stop that engine and delete its temporary directory. Assert that no host engine socket was ever opened.

The new assertions this adds over today's tests: real container create, start, stop and inspect; real health checks; real config IDs; real crash recovery against a live engine; and one connected path from task review to release evidence.

## Open items

- Rootless dockerd inside a CI container needs `--privileged` or user-namespace support on the runner. Running it on the staging PC would count as host activation and needs Beau. A developer machine's own Docker can host the outer container only with Beau's OK.
- Step 4 depends on the CI-to-image binding, which has not been built. Until then, the test records CI and build as separate facts and asserts the gap (a strict xfail).
- The `bootstrap_container` path for a first deploy is out of scope. The fixture seeds a baseline through the existing enrolled-baseline test helper.
