# Modular staging: operations and deployment gates

Status: source and read-only host assessment, **not installed-host acceptance**.
This document supplements [the v1 contract](modular-staging-v1.md) and
[worker packaging](../services/staging/README.md). Beau authorized building the
modular staging service. The remaining gates concern actual host readiness and
existing ownership; this document does not release held work, authorize
filesystem repair or change production/Cloudflare routes. Existing EH Compose
and its MCP/browser ports remain unchanged.

## Current staging host assessment

Read-only SSH observations on `192.168.50.90`, 24 October 2026, approximately
15:15 UTC. No credential contents, service environment or private key files were
read. Commands inspected filesystem/memory capacity, selected unit state, binary
and socket metadata, and listening process names; no service was started or
stopped and no application protocol was probed.

| Observation | Evidence and consequence |
| --- | --- |
| Root filesystem | `5,004,914,688` available bytes, 95% used: approximately 4.66 GiB free. Only `709,947,392` bytes, approximately 677 MiB, remain above a 4 GiB free-space floor. |
| Memory | `5,730,902,016` bytes available, approximately 5.34 GiB, at this observation. This is not a reservation or permission to consume that memory. |
| Existing rootful Docker | `docker.service` active/running, PID 1216. Existing PA/Viewer workloads must not be adopted, stopped, pruned or used as the new worker's daemon. |
| Existing Viewer | `viewer-staging.service` active/running, PID 1763761. Its unit remains at `/etc/systemd/system/viewer-staging.service`. |
| Existing connector | `cloudflared-staging.service` active/running, PID 51589. It is not a new-worker network or authentication grant. |
| Candidate rootless identity | SSH principal `bkoltz`, UID 1000. `dockerd-rootless.sh` and `rootlesskit` are present as root-owned executable regular files. Presence does not prove runtime readiness. |
| Candidate rootless runtime | User `docker.service` reports not-found/inactive, MainPID 0. `/run/user/1000/docker.sock` is absent. No new rootless daemon is currently available to this worker. |
| Historical TEST runtime | `/run/user/10001/docker.sock` was absent by metadata inspection. Historical offline TEST state remains protected; a missing socket does not resolve its durable held operations. |
| Dedicated worker TLS | No installed NGINX/Caddy/Traefik/dedicated proxy service was found in the selected unit-file inventory. A historical TLS-credential preflight transient exists but is failed, not a serving proxy. No recognized new-worker TLS listener on 443, 8443 or 8780 was observed. Existing application listeners were neither probed nor adopted. |

These observations support **blocked live worker acceptance**, not automatic
repair or permission to create an alternative execution path. Refresh capacity
and ownership immediately before activation; process
IDs and available memory are point-in-time facts.

## Non-negotiable shared-host boundary

The old UID10001 daemon, offline `loop8` TEST filesystem, and campaigns identified
as `5550`/`8a5b` remain outside this module's ownership. Do not mount, repair,
activate, release, adopt, migrate or delete them. Do not create a fresh queue,
database, machine alias or Docker daemon to evade their canonical physical-machine
hold. Offline processes and missing sockets are not proof that an uncertain
operation has been reconciled.

The live PA/Viewer services and rootful Docker are also outside worker ownership.
This is a software-staging machine, not an OpenSplat/ODM/LiDAR processing host.
Use small reproducible source fixtures and fixed, explicitly registered recipes.

## Minimal service layout after prerequisites are met

One EH control plane on TrueNAS owns identity, project grants, tasks, resources,
the existing canonical queue and physical-machine reservations. The optional
worker on `.90` performs one accepted operation at a time and retains its private
idempotency journal. It is not another scheduler or privileged remote shell.

Proposed layout, not created by this assessment:

```text
TrueNAS: existing EH application and local durable ledger
  <existing-state-root>/staging-service.json   optional configuration
  <protected-config>/staging-worker-token     dedicated bearer reference
  <protected-config>/staging-worker-ca.pem    pinned private CA reference

Staging PC: newly enrolled private service root, never historical TEST paths
  releases/<immutable-release-id>/            current and one rollback wheel/runtime
  current                                    operator-managed release selection
  state/journal.sqlite3                      local durable worker journal
  state/sources/                             bounded registered source cache
  state/resources/<EH-issued-UUID>/           owned workspace/build/test/preview
  private/                                   protected service configuration/credentials
```

Choose the actual local path and runtime identity through the existing ownership
process; names above confer no filesystem permission. Do not put Docker state or
the Hub/worker SQLite databases on SMB. NAS storage may hold original datasets
and explicitly published results, but is not a substitute for verified local
runtime and scratch capacity. Build containers receive job-only inputs/outputs,
not service credentials, the host Docker socket or production mounts.

The intended private network route is:

`EH → pinned-CA HTTPS to .90 → dedicated restricted TLS proxy → loopback worker`

The worker binds loopback only; for example, a reviewed proxy may bind the
specific LAN IP while forwarding to a different loopback socket. Exact ports,
certificate files and service names require enrollment; this document opens or
reserves none. Do not route the worker through the public Cloudflare tunnel.
The proxy admits only the measured NAS socket source, preserves the dedicated
bearer, and performs one upstream attempt. The worker checks the real proxy
socket peer; forwarded IPs and EH browser cookies confer no permission. The NAS
client verifies its explicitly configured CA and the literal `.90` certificate
IP SAN, with no environment proxy, default-CA or disabled-TLS fallback.

## Required deployment prerequisites

1. Reconcile or retain the canonical machine hold through its existing owners.
   Until admission is explicitly permitted, do not start a worker, rootless
   daemon, preview proxy or test job on this host.
2. Establish a separate, currently usable rootless Docker endpoint and its
   aggregate host policy without touching the old TEST runtime or rootful Docker.
   Installed binaries alone do not satisfy this gate. Enrollment must identify
   the actual endpoint, service UID, private root and fixed executable.
3. Budget daemon/image layers, two service releases, journal growth, source
   deposits, expanded sources, outputs and logs together. The worker now supports
   `capacity_bytes` and `free_floor_bytes`; the proposed small-host configuration
   uses 128 MiB and 4 GiB respectively. These checks bound worker storage but do
   not bound a separate Docker image store or unrelated application growth.
   Current approximately 677 MiB headroom cannot justify the old default 1 GiB
   worker budget, and even 128 MiB is not acceptance without the remaining budget.
   Additional storage or known-owned cleanup must be separately approved; never
   repair or reclaim `loop8` to make this fit.
4. Provision the dedicated worker credential, private TLS material and source
   allowlist through the protected-file workflow. Do not reuse an agent MCP
   token, paste secrets into chat, commit them or expose them to build jobs.
5. Enroll immutable cached build images, reviewed recipes and explicit project
   grants. Do not pull arbitrary images, accept caller-selected commands or
   enable every repo merely because it exists in EH.
6. Complete the actual source/build/test/restart-held/cleanup acceptance below
   before enabling autonomous work. A configuration parse, source test or Docker
   mock is not live-host acceptance.

## EH configuration and permissions

The existing EH deployment may load the optional sibling `staging-service.json`.
Missing or invalid configuration is nonfatal and does not enable staging. There
is no Compose port change, secret auto-creation, new MCP token or default
first-user administrator. Use the separate reviewed configuration examples;
keep `enabled` and `auto_run` false until the applicable gates have passed.

The TrueNAS EH process runs as UID568. Its dedicated bearer reference must be a
regular, non-symlink private file owned by the process UID (or root where actually
readable), with mode0600. Provisioning as UID568 avoids an unreadable root-owned
0600 file. The pinned CA is read from a protected, non-writable-to-other-users
regular file; verify actual container paths/permissions rather than assuming a
host file is visible. Configuration files should also be protected from other
users' writes. No protected file values belong in review records.

Configure exact authenticated human actor IDs in `admin_actors`, not email-domain
wildcards or the first person who logs in. Administrative status does not itself
grant every project action. The human owner records explicit per-project grants
for `read`, `build`, `test`, `preview`, `retain` and `cleanup` through EH. Agents
retain their existing MCP tokens, cannot grant themselves permissions or pin
forever, and cannot clean up another agent's resources. Project metadata and lead
agent assignment are not execution grants.

## Releases, retention and safe cleanup

Keep the currently installed service release and **one last known-good rollback**,
not an accumulating copy of every build. Identify both by immutable source and
artifact identity. Do not delete the previous release until the replacement has
passed health and controlled restart acceptance. A service rollback does not
replace, erase or rewind the durable worker journal or replay held operations.

EH resource defaults remain previews3 days, terminal workspaces24 hours, build
outputs14 days and test payloads/logs30 days. Explicit keep-until and human pin
override ordinary expiry. Active jobs, current release, rollback target, held
campaigns, decision evidence and the latest diagnostic failure remain protected.
If protected resources exhaust capacity, deny new work instead of deleting them.

Cleanup is plan → inspect IDs/revisions/protections → apply the exact digest →
verify the original worker receipt and absence. Unknown outcomes stay uncertain
for reconciliation; a second plan is not permission to redispatch deletion.
Registered source deposits are collectible only under the implemented terminal,
all-resources-deleted/reference rules. Unknown files, old TEST campaigns, shared
Docker images, other applications and unregistered containers are not sweep
targets. Retain compact tombstones and job/cleanup receipts. Never use broad
Docker prune or a recursive host-directory cleanup as staging maintenance.

## Private preview deployment

EH login, browser/API project checks and MCP endpoints stay where they are. Raw
preview HTML/JavaScript uses one **separately approved** HTTPS hostname and opaque
sandboxed capabilities, not normal EH-origin pages. No per-demo domain purchase
or remote-desktop service is required. Configuring an origin in source is not
approval to alter DNS, Cloudflare Access, Tunnel routes or the TrueNAS application.

The approved route must preserve the canonical Host and allow the capability
gateway's credentialless module/font/JSON requests without forwarding EH cookies
or Access assertions to the worker. Do not apply a generic Access redirect to
every asset and then relax global EH authentication when module loads fail.
Redact capability paths at every server/proxy/access log. Existing Ops policies
and routes remain untouched. Before route approval, leave previews unavailable
and verify isolation with separate local browser-test origins only.

## Acceptance record required before reporting readiness

- Record the real immutable service/image identities, endpoint, UID/mount/profile
  facts, free-space floor and canonical queue/machine admission.
- Prove NAS-to-worker TLS with correct and wrong CA/IP SAN, correct and wrong
  bearer and real source-peer restrictions. Do not publish credential values.
- Execute one tiny registered build/test job through EH, checking the original
  IDs, source ZIP digest, immutable output manifest, health and bounded resources.
  Source commit remains declared provenance unless independently verified.
- Verify identical command IDs do not execute twice; restart-interrupted work
  becomes held and blocks new work until explicit reconciliation. Durable queued
  work is not automatically replayed by installing a new runtime.
- Verify project denial, revoked grants, read-only preview/session revocation,
  retain/pin protections, scoped cleanup and original-operation receipt recovery.
- Execute the real browser module/CSS/font/JSON and parent/session/cross-preview
  isolation tests. A working HTML download is not browser acceptance.
- Only then consider bounded boot startup and separately authorized host reboot
  recovery. No boot persistence or reboot success was tested by this assessment.

No production deployment, DNS/Cloudflare change, host installation, cleanup,
daemon activation, filesystem recovery or held-reservation release was performed
for this document.
