# Live staging: one future operator handoff

This is an inert preparation checklist, not a deployment or execution instruction.
Beau applies NAS changes. Keep the current ordinary Compose unchanged until the
gates below are accepted. Updating the ordinary UI/MCP image does not activate
staging. No production, Cloudflare, token, boot-autostart or subscription changes
are included.

## Release prerequisite

### Later release checkpoint (2026-10-22, America/Chicago)

The original release discussion below is historical: PR53 and PR54 were merged
and published together in `5e53b43ca4bea10cf647d5e7550f2f46b3347a39`.
PR55 was subsequently published in
`11d045a1062b7b2bc200a96842a3992f08c52368`, verified by workflow
`39871121323`; both saved registry/latest manifests hash to
`sha256:3494a48a927e43b07fb7e62fe1f348a9700ebcc83d5d3bdbf7f9d2318f3fd240`.
PR56 is merged at `055cecb306f4041a28d1c520da99ad553d7b6436`, but its
publication workflow `39901194959` was still running at this checkpoint.

A fresh NAS health read still identified ordinary `49dcbcd`, not these later
releases. Publication resolves the source/image prerequisite only; it does not
establish NAS enrollment, protected storage, executor adoption, restore or reboot
acceptance. All operator gates below remain in force. Consult the Hub document
`runbooks/current-staging-readiness-20261022.md` for subsequent observations before
selecting an artifact; do not treat this dated checkpoint as current deployment.

### Original 49d checkpoint

Published ordinary release `49dcbcd3da6254739c85299a581a0d34d3b9a4ea` is suitable
for its separately documented ordinary update. It does **not** include PR53's
current-ledger enrollment compatibility or PR54's ordinary idle-stream drain fix.

Before preparing a protected cutover, accept and publish the current-ledger
enrollment fix from PR53, independently identify the resulting immutable image,
and verify its installed enrollment/inactive-startup profile against a ledger
constructed by the current application. Do not use an older minimal-schema
fixture as proof that the real current ledger is supported. PR53 supports
initial cold enrollment, not relocation of an already enrolled ledger.

Review PR54 separately for the ordinary shutdown follow-up. Its measured idle
stream improvement does not establish the cause of the earlier NAS exit137 or
prove NAS supervisor drain. An independently reviewed source branch or passing
container tests are not proof that either follow-up is deployed.

## Beau's NAS preparation — acceptance still required

1. Accept a protected child inside the existing Engineering-Hub root. Verify
   actual UID568 access, effective ACLs, source-parent rename/delete protection
   and backup coverage, including any new child dataset. Do not recursively
   change the existing shared root or bus permissions. A metadata listing or
   a successful ZFS snapshot alone does not prove these boundaries or recovery.
2. Agree on a maintenance window and verify all writers to the ledger and
   settings are stopped. Preserve complete coherent state, including SQLite
   sidecars when present. Do not copy just a live SQLite main file or discard
   sidecars. Retain the original authority and its configuration separately.
3. Classify the ledger before any move. A pre-existing owner marker/lock,
   unresolved work, unsupported schema or uncertain writer is a stop condition,
   not permission to delete records or retry enrollment. For an accepted cold,
   unenrolled source, prepare an independently reviewed copy/parity plan; this
   checklist supplies no executable copier.
4. Only an accepted destination may use the existing `pilot_enrollment inspect`
   operation. Preserve its complete `plan` and `digest` output privately.
   `pilot_enrollment apply` is a separate mutating operation requiring the
   reviewed digest and explicit writer-stopped/backup attestations; those flags
   do not establish the facts. Retain failure artifacts; do not auto-retry.
5. Review one final configuration change and the abort boundary before applying
   it. Initial startup is **owned-inactive**, not the queued-pilot selector.
   Confirm existing Access, agent identities, ports8770/8771, notes, decisions,
   tasks, messages and retained delivery history; execution must remain disabled.

### Future selective mapping template — not a replacement Compose

The following documented paths must already exist and pass acceptance. Replace
the whole-parent bind; do not leave it alongside the private child, which would
expose a second alias to protected state. All three bindings require
`create_host_path: false`; settings is read-only inside the container.

| Host source | Container target |
| --- | --- |
| `/mnt/Plugins/App_Data/Engineering-Hub/settings.json` | `/var/lib/hub/settings.json` (read-only file) |
| `/mnt/Plugins/App_Data/Engineering-Hub/agent-message-bus/` | `/var/lib/hub/agent-message-bus` |
| `/mnt/Plugins/App_Data/Engineering-Hub/private/` | `/var/lib/hub-private` |

Retain validated existing non-secret settings and select documented version-two
`protected-ledger-v1` storage only as part of that reviewed change. The fixed
private ledger is `/var/lib/hub-private/hub.sqlite3`; its existing enrollment and
lock must match their accepted identities. Private root0700 and ledger/lock0600
must be owned by the runtime UID, with the required filesystem/ancestor checks.

The future process argument list is
`python -I -B -m engineering_hub.owned_inactive_hub --config /var/lib/hub/settings.json`.
It **replaces** the ordinary image entrypoint; it is not an argument appended to
`python -m engineering_hub`. This reference is not an instruction to run it now.
Ordinary/version-one startup is not a fallback for the selective layout.

Owner acquisition and authentication/schema initialization can write even in
inactive mode. Once the new authority accepts mutations, the retained old ledger
is stale: reverting image/mappings or an old snapshot is not lossless rollback.
Freeze and preserve state for reviewed recovery rather than silently reopening
the old authority. See the [offline cutover runbook](offline-ledger-cutover-runbook.md).

## Staging-host preparation — separate from NAS changes

The rootless Engine identified during read-only inventory is not an accepted
executor, a new campaign or permission to adopt its workloads. The earlier8ec
executor/TLS window is closed; do not reuse it. Engine identity does not establish
idle capacity, writer exclusion, an issuer receipt or a working NAS route.

Before any bounded activation, independently accept one fresh generation with:

- Exact immutable executor/application artifacts, current Engine peer/process
  identity, driver/issuer enrollment and a healthy frozen disposable baseline.
- Protected disjoint state, TEST credentials distinct from live agent tokens,
  observed UID/GID/socket mappings, aggregate resource limits and writer exclusion.
- A current signed launch receipt, owned supervisor/drain lifecycle and explicit
  endpoint-generation recovery. No boot autostart or automatic uncertain replay.
- A dedicated standard HTTPS-to-UDS proxy, restricted to NAS192.168.50.80, using
  verified certificate trust/name and caller bearer, exact routes and no retry.
  The existing renderer is inert and requires actual sandbox/mount acceptance;
  rendering alone does not install a permanent owner or renew receipt lifetime.

All these are **unaccepted live gates** until actual evidence says otherwise.
No activation commands, credentials or new service installation are supplied here.

### First-release recovery model: trusted operator, not unattended replay

A third continuously running monotonic-authority service is not a prerequisite
for a bounded, manually accepted first release. The existing storage/launcher
trust model already trusts Beau and host administrators. What remains mandatory
is an explicit recovery boundary: all relevant writers and prior executor
generations stopped, evidence preserved, intended authorities selected, and
actual current Engine effects reconciled with both operation histories.

Every new queued session must start admission-closed. A fresh process/owner-bound
challenge, current immutable executor identity, complete target inventory and
effective writer exclusion support the operator's bounded session acceptance.
Unknown work or unresolved differences remain held; do not reset fences,
manufacture receipts or replay commands to make histories agree. An in-memory
accepted capability must expire or close with its process/generation, and be
checked again before later effects. Restart/reboot requires new reconciliation.

This is an implementation and acceptance requirement, not a claim that current
startup already enforces it. No new editable approval file, settings boolean or
allowlist callback establishes reconciliation. The launch receipt alone proves
bounded launch identity, not complete current host state. Operator attestations
record responsibility; they do not automatically establish the underlying facts.

Restoring both histories can be handled by deliberate trusted-operator inspection
and authority selection, but not advertised as automatically detected rollback.
Undisclosed rollback of all trusted state by a malicious administrator is outside
this first-release model. Restoring databases under an active writer remains
unsupported. See the source-only reconciliation contract in the Hub; no NAS
restore or service installation is authorized by this section.

## End-to-end acceptance after both boundaries are accepted

First run the existing container-shell `executor_connection_check` with privately
provisioned explicit CA/credential files and their verified hashes. Its two GETs
can establish transport/auth only (401 unsigned,404 authenticated unknown command),
not readiness or permission to submit work. Never disable TLS verification.

Then, under the separately accepted bounded campaign, require a fresh task and
different-agent review of its exact manifest/artifacts. Prove actual NAS-owned
queued deployment, independent health checks, deliberate failed-candidate
immutable rollback, interrupted-work reconciliation and retained state. Confirm
ordinary MCP and other applications remain unchanged. Treat app recreation,
protected restoration and PC reboot as separate acceptance checks; the earlier
isolated pilot and synthetic integration tests do not prove them.

References: [protected layout](protected-ledger-layout.md),
[owned-inactive service](owned-inactive-hub.md),
[queued selector](unified-queued-pilot.md),
[proxy renderer limits](executor-proxy-bundle.md),
[connection check](executor-connection-check.md).
