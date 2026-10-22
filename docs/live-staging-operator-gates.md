# Live staging: one future operator handoff

This is an inert preparation checklist, not a deployment or execution instruction.
Beau applies NAS changes. Keep the current ordinary Compose unchanged until the
gates below are accepted. Updating the ordinary UI/MCP image does not activate
staging. No production, Cloudflare, token, boot-autostart or subscription changes
are included.

## Release prerequisite

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
