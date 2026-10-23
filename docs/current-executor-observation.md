# Bounded current executor observation (source only)

This standalone protocol/verifier is **not integrated or activated**. It creates
no service, transport, key, enrollment, process or task. Tests use synthetic
in-memory Ed25519 signers, not real staging identity or inventory evidence.

`engineering_hub.current_executor_observation.CurrentExecutorObserver` is a
synchronous callable suitable for a future manual reconciliation observer. Core
Hub can import it without the optional `hub_container_executor` package. Trusted
composition must explicitly convert the SAME independently enrolled existing
issuer/release into `CurrentExecutorReleasePins` (eight fields, including the
public key), and supply independently retained exact `CurrentLaunchPins`.
Neither set of pins may be selected from an observation response, candidate Hub
release, proxy generation or generic JSON configuration acceptance flag.

The future public `LaunchSupervisor.observe_current_launch(nonce, timeout_ms)`
must use its existing retained authority and fully revalidate the pinned Engine,
socket/namespace, pidfd/process start/boot, container, mounts/profile and release
before signing. Engine ID, daemon PID and start ticks share the pinned host boot.
This verifier does not perform those remote kernel checks itself. Signatures
prove possession of the enrolled issuer key, not the correctness of issuer claims.
There is no new signer authority or reuse of one-shot launch receipt evidence.

## Closed protocol

Protocol `engineering-hub.executor-current-observation.v1`, audience
`engineering-hub.staging-session-observer`, Ed25519 domain
`engineering-hub-current-observation-v1` followed by one NUL byte. Envelope is
exactly `payload` plus unpadded canonical URL-safe base64 `signature`. Signature
covers domain plus canonical UTF-8 JSON payload (sorted keys, compact separators,
no nonfinite or floating-point numbers, duplicate fields or extra fields).
Entire envelope is at most 8192 bytes. Exact payload fields:

- `protocol`, `audience`, fresh internal 64-hex `nonce`, UUID4 `observation_id`;
- `issuer_id`, `implementation_commit`, `runnable_digest`,
  `platform_manifest_digest`, `image_config_id`, `platform`, `launch_profile_digest`;
- `launch_id`, `boot_id`, `authenticated_host_pid`, `process_start_ticks`,
  `container_id`, `engine_id`, `engine_pid`, `engine_start_ticks`;
- integer `observed_at_ms`, `expires_at_ms`, `approval_expires_at_ms`.

`approval_expires_at_ms` is the original independently pinned approval wall
ceiling. The retained supervisor captures it ONCE at original launch and clips all
observations to both it and its original local monotonic launch deadline. It must
never recompute a renewed approval expiry for a new request. Hub derives a fixed
LOCAL monotonic ceiling once from the remaining pinned wall interval; it never
compares monotonic timestamps from different hosts. Every call checks both wall
and local monotonic ceilings and rejects observed clock reversal. A reconstructed
observer/process still requires fresh independent manual reconciliation. This is
not global anti-rollback against trusted administrator clock rollback or rollback
of all trusted enrollment/state. Beau/host-root and issuer authority remain trusted.

Each call performs one synchronous exchange with its fresh nonce, no retry or
cached identity. Whole elapsed deadline includes bounded mutex acquisition,
transport, parsing and signature verification. Issuer wall timestamps may differ
by at most 5000 ms in either direction; nonce freshness, whole-call monotonic
budget and original approval ceilings still apply, with no cached validity grant.
Default exchange budget is 2000 ms; observation validity 5000 ms,
both configurable only within 100..10000 ms; approval remaining at construction
must be positive and at most one hour. Expired response, replay, mismatch, clock
reversal, PID change, callback reentry, transport/crypto failure or budget overrun
denies. A failure (including cancellation/process-control exceptions, which are
re-raised without conversion) permanently seals this observer; concurrent ordinary calls
serialize. At most 4096 observations are allowed per instance (bounded replay
tracking); exceeding that bound requires a new independently reconciled session,
not automatic renewal. `close()` revokes local use, not caller-owned supervisor
or transport. PID checks precede mutex acquisition after fork.

Timeouts are cooperative: synchronous transport must actually enforce its I/O
deadline. The verifier can reject an overrun after it returns but cannot interrupt
a blocked call. No asynchronous transport task is spawned/adopted/cancelled;
only an incorrectly returned unstarted coroutine is closed.

Returned gate identity is exactly `generation_id=launch_id` and
`artifact_digest=executor image_config_id`. It does **not** prove inventory,
writer exclusion, restored-state correctness, absence of unknown effects,
ongoing process integrity, session acceptance, task execution approval or
renewal of original launch approval. The supervisor, executor relay and core
HTTPS transport are described in `current-observation-supervisor.md` and
`current-executor-https-transport.md`. Their synthetic integration tests do not
replace trusted live composition, independent pins or manual reconciliation;
this file alone is not a completed usable installed operator path.
