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

## Outstanding shared-loop latency acceptance

The signed observer is currently synchronous on the owned Hub event loop. A
broker snapshot performs one fresh observation, acceptance four, and decline two.
An instrumented queued-runtime fixture counted 25 observations before its first
command POST and 33 before its second; these are fixture counts, not measured live
NAS latency. With the current two-second per-observation budget, legitimate slow
responses can accumulate substantial shared-loop blocking (roughly 50 seconds of
observer time for 25 calls). Other MCP/UI requests cannot progress while an
individual synchronous call occupies that loop. The new test deliberately adds
5ms per synthetic observation and demonstrates both the broker counts and a
scheduled peer waiting until the synchronous decision returns.

**Usable concurrent MCP/UI latency acceptance remains outstanding.** This explicit
selector is unactivated; passing functional/container tests is not a claim that
its current latency is acceptable for a live unified Hub. The next step is a
reviewed asynchronous preflight at semantic admission boundaries with a safe
handover barrier and cancellation/identity revalidation. Blind thread-pool
offloading, caching identity results, dropping checks or arbitrarily tightening
timeouts are not implemented fixes and would require separate evidence/review.
