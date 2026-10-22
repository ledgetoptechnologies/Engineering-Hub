# Generation-bound local queue phase drain

Explicit source API, not an activated or remotely authenticated service. This
change does not deploy a service, access live secrets, activate an executor or
renew an execution identity.

## Deliverables and real integration

`src/engineering_hub/queued_pilot_runtime.py` is the existing runtime plus
`bind_generation_drain(owner, generation_id)` and a local capability. It actually
invokes the existing sticky close_admission and cancellation-safe runtime.stop;
it is not a stand-alone request/ack data model or a timer extension.

`src/engineering_hub/unified_queued_pilot_service.py` adds paired optional arguments
`generation_id` and synchronous `on_generation_ready(capability)`. During the
existing runtime construction it binds exactly the existing bridge/repository
PilotOwner and its epoch. Only after existing listener startup does the trusted
local callback receive the capability. Existing callers supplying neither option
retain their behavior. Service teardown awaits a pending owned drain, then revokes
the capability before client/owner cleanup. No public HTTP/MCP route is added.

The integrator can retain that in-process capability and later call:

```python
ack = await capability.drain(generation_id=exact_generation_uuid,
                             request_id=fresh_canonical_request_uuid)
```

The capability is bound to process, runtime object/bridge binding, owning loop,
exact owner object, owner epoch, database ownership and generation. A request
binds once before synchronous admission close. Concurrent/repeated calls with
the same request share one owned drain task and identical frozen acknowledgement;
different/stale generation or request IDs fail. A cancelled caller is not given
an acknowledgement: it receives cancellation only after actual drain termination,
and can explicitly retrieve the existing result with the same request if still
authorized. No retry or new request is created automatically.

Successful acknowledgement is created only after the runtime task completes
normally, its accepted phase terminates, its observer is gathered and source
iterator closes, active work is absent, admission remains closed, no native
source failure is recorded, and the exact current owner remains valid. Recheck
the owner again when retrieving an existing acknowledgement. Cleanup, ownership
loss, source failure or a cancelled/failed runtime denies acknowledgement.

## Exact scope, not a remote stop authorization

The frozen acknowledgement explicitly says:

- scope: `local_queue_accepted_phase_and_observer_only`;
- `whole_workflow_completed: false`;
- `native_executor_stopped: false`;
- `remote_authenticated: false`.

It is an in-memory local possession capability, not a signed transferable proof.
Copying its JSON fields, knowing the generation UUID or receiving an agent
message grants nothing. Same-process/root code is trusted, not sandboxed.

The queue closes admission to subsequent phases as the existing implementation
already does. An incomplete workflow can remain HELD with durable reservation;
that is not a failure to drain the accepted phase and must not be described as a
completed rollout. This acknowledgement also does NOT prove every direct Hub
workflow HTTP call, executor request, proxy connection or native process drained.
Only the explicitly named runtime is covered. Existing service teardown still
must drain its other owned tasks/listeners before releasing ownership.

## Remaining full-goal gaps

1. NAS-to-root-native-owner transport must authenticate a specifically enrolled
   lifecycle requester and bind request+generation. No safe remote transport
   exists here, so this implementation intentionally exposes none and does not let a
   bearer/agent message substitute for authentication.
2. Aggregate service/executor/proxy drain must compose their own actual lifetimes
   before the native owner accepts stop. Do not use this queue-only ack alone to
   kill a busy executor. A native stop call is not added by this implementation.
3. v1 one-shot receipt expiry and bounded launcher wait remain unchanged. The
   later continuous-owner/live command-identity protocol remains separate work;
   this capability does not renew authority or make the owner continuous yet.
4. No durable ack reconstruction, crash takeover, journal adoption, boot
   autostart, TLS/Cloudflare change, production authority or deployment is added.

## Validation scope

The portable tests execute the actual runtime with mocked providers: accepted
phase/source-close barriers, repeated caller cancellation, shared/concurrent
request, identity/epoch loss, source/cleanup failure and revocation. The Linux
acceptance tests additionally use real PilotOwner kernel locking, native SQLite
feeds and unified local HTTP service startup/shutdown with synthetic executor
responses. They verify capability handoff, phase-only acknowledgement, readiness,
post-shutdown revocation and failed callback cleanup. These tests do not use a
Docker daemon, NAS, remote transport, production credential or native executor/
LaunchSupervisor.

Windows runs skip the Linux cases; a skip is not installed acceptance. The
mocked tests and Linux suites must pass in an explicitly isolated source-test
runtime before treating this integration as reviewed lifecycle evidence.

The final focused Linux validation run passed 62 tests with ResourceWarning and
PytestUnraisableExceptionWarning treated as errors, including the existing runtime
and unified-service regression suites and raw-ASGI duplicate-Authorization tests.
It ran in a network-disabled, non-root, read-only test container with disposable
tmpfs test data. Executor responses in these tests remain synthetic; this result
does not establish a live NAS connection, native executor drain or reboot safety.

## Repeatable isolated Linux validation

From the repository root, build the test-only image with
`docker build -f docker/Dockerfile.hub-validation -t eh-drain-validation .`.
Use the resulting immutable local image ID when recording release evidence.
The validation image includes the public unified Compose and settings example;
neither contains a live credential.

A full-suite run needs disposable writable test-data directories even when the
image filesystem is read-only. Its temporary directory must allow executing
synthetic test scripts used by the worker protocol tests. These are test-only
mounts, not a proposal to change production container mounts or hardening:

```sh
docker run --rm --network none --cpus 1 --memory 1g --memory-swap 1g \
  --pids-limit 128 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=512m \
  --tmpfs /validation/data:rw,nosuid,nodev,noexec,size=128m,uid=10001,gid=10001,mode=0700 \
  --tmpfs /validation/tests/.test-data:rw,nosuid,nodev,noexec,size=64m,uid=10001,gid=10001,mode=0700 \
  eh-drain-validation python -m pytest -q -W default \
  --basetemp /tmp/hub-validation -p no:cacheprovider
```

No host filesystem, Docker socket, live credentials, NAS or production endpoint
is mounted. Network-disabled tests may use only their own container loopback.
Full-suite skips and warnings must be reported separately from passing tests.
