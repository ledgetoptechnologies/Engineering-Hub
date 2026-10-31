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
credential paths/hashes. In GitHub mode, the selector first reads and validates
the protected v3 session enrollment and actor against the deployment settings.
It then obtains the existing `PilotOwner` and captures CA/credential bytes once.
Both the command client and synchronous
current-identity transport use that same captured profile/material. Neither
reopens those paths for network requests. The credential must not match any agent
token. TLS trust and the executor signing authority are distinct checks.

The additional manual-session file is protected data: current-UID mode0600,
private mode0700 parent and trusted ancestors, read through the existing stable
no-follow reader. It is never a public Hub note, code/module name, arbitrary
factory, executable file or an executor-supplied enrollment response. The legacy
Cloudflare form uses `engineering-hub.manual-staging-session.v1`; the disjoint
Cloudflare form uses v2. The explicit GitHub form described below uses v3. Their
common retained fields are:

| Field | Independently retained content |
|---|---|
| `release` | Exact `CurrentExecutorReleasePins` fields; `public_key_hex` replaces `public_key`, platform is its closed platform object |
| `launch` | Exact `CurrentLaunchPins` fields, including original approval expiry, boot/process/Engine identities and launch/container IDs |
| `operator_actor` | Exact independently enrolled `human:cf:<64 lowercase hex>` actor for v1/v2, or `human:<numeric GitHub ID>` for v3; never the first visitor |
| `report` | Exact fields below; original observations, not newly generated facts |

For session v2 and v3, the top-level object additionally requires
`profile_mode: "disjoint-v3"` and `profile_json`, a string containing the exact
canonical JSON bytes of the independently enrolled executor launch profile. Its
digest must match the separately retained release pin; the embedded profile is
not itself proof of host isolation.

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

## Explicit GitHub mode for a separate staging deployment

The default deployment configuration remains Cloudflare Access. A GitHub pilot
requires an independently approved deployment settings file with the exact
`schema_version` integer `3`, `base_url`, `github`, `agent_allowed_hosts`,
`agent_allowed_ips`, and `storage` keys. `base_url` must be the exact HTTPS
origin. `github` contains only `client_id` and `allowed_user_id`: the latter is
the owner's positive numeric GitHub ID below 2^63, not a login name. `storage`
must be `{"layout":"protected-ledger-v1"}`. The host and IP lists retain their
existing exact allowlist semantics. Do not include a Cloudflare block, OAuth
secret, tokens, or any other key in this v3 file.

The approved launcher supplies the existing `HUB_GITHUB_CLIENT_SECRET`
environment secret to the Hub process through its protected deployment mechanism;
it is not stored in the settings file, command line, logs, or evidence. The
OAuth application must already be independently configured for the deployment's
exact HTTPS origin and `/api/hub/v1/auth/callback`. Separate agent tokens, the
executor credential and pinned CA remain required. This selector does not create
or authorize those credentials or change an identity-provider policy.

The pilot profile must use `engineering-hub.unified-pilot.v2` with
`profile_mode` set to `disjoint-v3`. Its protected file, executor credential and
CA use the existing protected-ledger paths: `/var/lib/hub-private/pilot.json`,
`/var/lib/hub-private/executor.token` and `/var/lib/hub-private/staging-ca.pem`.
Provision a separately captured `engineering-hub.manual-staging-session.v3`
file with the signed disjoint executor profile and exact retained release,
launch and report evidence. Its `operator_actor` must be `human:` followed by
the same numeric `allowed_user_id`; use neither a Cloudflare actor nor a made-up
attestation. The report's currentness, source and writer-exclusion prerequisites
above still apply.

Use the existing validation entrypoint to check configuration shape and
protected-boundary metadata:

```text
python -I -B -m engineering_hub.unified_pilot_startup validate --config <settings> --pilot-config /var/lib/hub-private/pilot.json
```

This does not read the session enrollment or OAuth
secret, authenticate an operator, contact the executor, or prove readiness. The
activation entrypoint is the fixed `engineering_hub.trusted_session_startup`
command shown above, with `--session-config` naming the protected v3 file. The
plain `unified_pilot_startup serve` CLI cannot supply that file and is not the
v3 activation path. `serve` acquires the existing owner and can perform effects;
it is not a read-only validation command.

This path serves the existing agent and browser front doors from one owned Hub
process; the GitHub browser path uses the existing OAuth state/PKCE, allowlisted
session and CSRF checks. The caller must independently provide the reviewed HTTPS
termination, exact public origin and route to the browser listener; this code does
not install or configure them. The separate private-UDS browser gateway and installed
local coordinator remain Cloudflare-only. Source tests use synthetic OAuth and
executor evidence and do not establish a deployed route, host isolation,
independent signer custody, reboot recovery or permission to use a held Engine.
Changing an existing deployment's provider or entrypoint requires a separately
reviewed operator rollout; preserve the existing production access path.

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
