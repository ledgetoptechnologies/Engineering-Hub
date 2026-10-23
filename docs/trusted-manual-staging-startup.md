# Explicit trusted manual staging startup

This is an opt-in installed code path, not automatic activation. Normal Hub
startup, its Compose, ports 8770/8771, agent tokens and live data remain unchanged.
The existing `unified_pilot_startup serve` CLI still refuses without trusted
callbacks. The separate fixed selector is:

```
python -m engineering_hub.trusted_session_startup \
  --config /explicit/protected/settings.json \
  --pilot-config /explicit/protected/pilot.json \
  --session-config /explicit/protected/manual-session.json
```

These are example container paths, **not provisioned files**. To select this
module in an app whose image currently has the normal Hub entrypoint, Beau must
explicitly change that app's entrypoint/arguments after its protected storage and
enrollment are ready. An image pull or unchanged normal Compose does not enable
this workflow. Do not run a second Hub owner against the live database or bind
additional alias ports. No source change here creates enrollment, changes ACLs,
mounts storage, installs services, changes Cloudflare policy or starts an executor.

## Independent setup and retained data

The ordinary protected pilot profile retains its existing manifest, exact allowed
tasks/agents, machine, literal-IP HTTPS executor origin, CA and dedicated executor
credential paths/hashes. The selector first obtains the existing `PilotOwner`,
then captures CA/credential bytes once. Both the command client and synchronous
current-identity transport use that same captured profile/material. Neither
reopens those paths for network requests. The credential must not match any agent
token. TLS trust and the executor signing authority are distinct checks.

The additional manual-session file is protected data: current-UID mode0600,
private mode0700 parent and trusted ancestors, read through the existing stable
no-follow reader. It is never a public Hub note, code/module name, arbitrary
factory, executable file or an executor-supplied enrollment response. Its closed
schema is `engineering-hub.manual-staging-session.v1`:

| Field | Independently retained content |
|---|---|
| `release` | Exact `CurrentExecutorReleasePins` fields; `public_key_hex` replaces `public_key`, platform is its closed platform object |
| `launch` | Exact `CurrentLaunchPins` fields, including original approval expiry, boot/process/Engine identities and launch/container IDs |
| `operator_actor` | Exact independently enrolled `human:cf:<64 lowercase hex>` actor; not the first visitor or the global allowlist |
| `report` | Exact fields below; original observations, not newly generated facts |

The report has `target_id`, `manifest_digest`, `machine_id`, `source` (bounded
source identifier), `observed_at_ms`, `expires_at_ms`, `executor`, `evidence` (1–6000 UTF-8
bytes), `evidence_sha256`, and `attestations`. The attestations contain exactly
`inventory_reconciled`, `writers_excluded`, `unresolved_effects_absent`: each must
be the explicit boolean `true`. Missing/false/integer claims are rejected, never
defaulted. No accepted bit is permitted. `enrollment_bytes` serializes typed data;
it does not discover or authorize its contents.

The report's `executor` retains the exact original `launch` fields plus
`image_config_id` for the executor release. Every field must match independent
enrollment with its exact type. Editing the enrolled launch or release cannot
silently rebind an old report to it; the original report binding is also shown
in the operator evidence alongside its target and manifest.

The report must be observed no more than five minutes before construction, not
in the future, and have an original validity window of at most ten minutes.
Its expiry must be no later than the original launch approval expiry. Independent
manifest/driver/platform/Engine bindings must match the captured pilot profile.
The original source observation time and expiry are displayed in the operator
evidence; binding a new process challenge does not relabel those facts as fresh.
Expiry and backwards wall/monotonic movement veto admission, including acceptance
after an operator leaves a review page open. No automatic report renewal exists.

## What still requires real evidence

Signing a current identity and hashing evidence text do **not** establish complete
inventory, writer exclusion, absence of unknown effects, coherent restoration or
NAS filesystem acceptance. Independently inspect the actual staging Engine,
containers, executor/Hub command and run ledgers, reservations, protected storage,
and every potential legacy writer. Match that evidence to the exact retained
launch, Engine, target and manifest. Missing/uncertain effects mean stop and retain
evidence—not claim `true`, adopt a container, replay a POST or release a fence.
The report is an operator attestation; its native collection/provenance and real
writer-exclusion procedure must be reviewed separately. This source path does not
invent or perform that procedure.

## Fresh manual acceptance and lifecycle

For every new process, fixed code creates one observer and one pending
`StartupReconciliation` bound to the exact owner epoch and target. Before serving
requests, it attaches the same gate to the queued runtime and operator broker,
using `OwnedSessionAcceptanceAudit` on the exact owned Hub SQLite repository.
No audit history or earlier acceptance opens the gate. The exact operator must
read the retained evidence and explicitly accept the new challenge through the
authenticated Hub route with CSRF protection. Other allowed users cannot accept.
Session acceptance remains **not task execution approval**: ordinary task review,
target restrictions, immutable manifests and queue policy still apply.

On stop/cancellation, existing owned service logic closes admission and drains
listeners/runtime/accepted effects. Only after it returns does the composition
close its borrowed observer/audit; partial pre-serving failures close their
already constructed resources too. No periodic monitor, new worker, retry,
autostart, credential or route migration is introduced.

Tests use bounded isolated containers, synthetic host observations and real local
SQLite ownership/auditing where stated. They are not actual NAS enrollment,
installation, host inventory, deployment, rollback, restore or reboot acceptance.

## Explicit asynchronous admission and remaining latency acceptance

The former synchronous installed composition performed one observation for a
broker snapshot, four for acceptance and two for decline. An earlier instrumented
queued-runtime fixture counted 25 observations before its first command POST and
33 before its second. Those were historical source-path counts, not live NAS
measurements; bounded blocking calls still stalled the shared loop. The former
5 ms fixture characterization is superseded by responsiveness requirements, not
silently reclassified as acceptable behavior.

The fixed trusted selector now explicitly constructs an async-admission gate.
Its bounded synchronous verifier runs in a retained executor Future, drained
through cancellation; it returns verified proof rather than a cached identity.
The composition checks original enrollment/report freshness before and after
each observation and clips proof expiry to the original report's monotonic
ceiling. The verified proof also retains its absolute signed expiry; the fixed
composition clips it to the original report and launch expiry. Every consumed
scope checks both clocks again after local ownership/proof reads, including slow
intent/result audits and completion validation. Forward wall steps cannot outlive
the retained absolute ceiling, and permitted issuer skew cannot extend it.
It uses the same captured TLS/credential bytes and independently retained
issuer/release/launch pins. It never renews the original launch or report.

Snapshot and decline use local binding only and claim no fresh remote readiness.
Acceptance awaits one fresh operator-specific preparation outside locks, then
rechecks exact actor/CAS/report/owner and consumes a one-use task/loop-bound scope.
Intent audit, opening and result audit stay on the owned loop without an await;
slow intent audits must not outlive proof freshness. Legacy scope-free effect
calls are denied in this mode. Independently explicit legacy synchronous unit
compositions retain their previous verification contract.

Focused fixtures check health requests below 250 ms while observation I/O is
deliberately blocked, including an actual signed supervisor/UDS/ASGI/TLS round
trip and the real fixed-selector callback wiring. Separate unified Hub/bus tests
measure MCP ping and overview requests during one-second synthetic observations;
actual isolated TCP service tests cover human acceptance, an already-queued job's
native wake, healthy deployment and rollback with simulated executor responses.
The audit result and sanitized outbox invalidation commit atomically. Intent
alone cannot wake work or restore authority in another process.
These measure isolated source integration, not audit/fsync latency guarantees,
network-wide MCP p95, real Engine checks, a NAS route, restart or deployed
concurrency acceptance. **Real-host latency and acceptance remain outstanding.**
This source selector remains unactivated; no cached readiness, blanket
`to_thread(broker.decide)`, automatic launch or reduced timeout is a substitute
for exact semantic admission boundaries and independent deployment acceptance.
