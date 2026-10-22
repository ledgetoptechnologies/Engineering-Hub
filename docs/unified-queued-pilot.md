# Explicit unified queued-pilot composition

Status: independently reviewed source integration, not an activated NAS workflow.
Normal `--config` startup remains unchanged and does not select this service.
See [the current release checkpoint](release-readiness-2026-10-22.md) before any
operator handoff; older evidence below is scoped, not proof of installed readiness.

Isolated non-root Linux validation passed 96 focused tests, including 19 new
composition cases. A separate root-agent rerun of the 19 cases and API contract
test passed 20 tests. They use real local HTTP/MCP listeners and native feeds,
but synthetic executor responses and test-only identity material. They cover
preserved bus records, one shared repository, owner/reviewer separation,
successful and rollback paths, failed construction, duplicate transfer and
repeated cancellation while accepted work drains. These are source-integration
results, not live host execution or an installed TLS route.

`engineering_hub.unified_queued_pilot_service.serve_owned_unified_queued_pilot`
composes the existing embedded message bus and queued staging runtime under one
Hub lifespan, repository and pair of authenticated front doors. It is not a
second Hub, worker protocol or queue. The caller supplies an exact pre-enrolled
`PilotOwner`, manifest, dedicated `ContainerPilotClient` factory, additional
target veto, explicit bus paths, machine/action identity and sufficient external
supervisor grace. The veto cannot grant task ownership, independent review,
manifest enrollment or permission to execute.

Both ports bind before owner/client transfer. A bind failure leaves the caller's
owner untouched. Once accepted, the owner is single-use. Shutdown closes new
runtime and HTTP admission before draining accepted requests and the one shared
core/bus lifespan; only afterward may the accepted client and owner close.
The listener must not force-cancel accepted mutations on the default 30-second
HTTP shutdown timer. Configured supervisor grace is an assertion, not evidence
about the installed container or system service.

## Intended remote path

The NAS-owned workflow can use the existing HTTPS `ContainerPilotClient` through
a dedicated standard Nginx TLS proxy to the staging executor's private Unix
socket. A custom Python proxy or a second distributed scheduler is unnecessary.
Nginx supports both [Unix-socket upstreams](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)
and [TLS server configuration](https://nginx.org/en/docs/http/ngx_http_ssl_module.html).
This is not a claim of installed NAS-to-staging networking.

The optional [NGINX template and acceptance notes](executor-nginx-https.md)
now include an isolated actual NGINX TLS-to-UDS smoke. That proves the synthetic
wire boundary only, not the NAS route, host UID mapping or executor activation.

The proxy must expose only the submit, exact-command inspection and reconcile
routes; preserve the caller's bearer without injecting credentials; allow only
the approved NAS source; disable upstream retries; and bound request buffering.
The existing client still checks the complete response identity, size and
whole-operation deadline. Server certificate and hostname verification must
remain enabled. Mount only the executor socket and proxy-specific configuration
and certificates, never an Engine socket, issuer key or executor database.

The dedicated rootless executor daemon has networking disabled. Therefore the
network-facing proxy must remain outside that network-disabled boundary, with
measured compatible socket UID/GID permissions. Neither a guessed UID nor a
read-only Docker socket mount supplies this isolation.

## Gates before a real pilot

- Approve and provision separate TEST credentials and signing material. Do not
  reuse production agent tokens for an isolated test coordinator.
- Reconcile the staging host's retained bootstrap ownership, update its reviewed
  service definition, and measure actual namespace, UID/GID, cgroup and socket
  evidence. Account for the aggregate baseline/candidate/rollback resource peak.
- Enroll the immutable executor, manifest and independently verified baseline.
- Configure and test verified HTTPS, source restrictions, authentication and
  no-retry behavior through the actual NAS-to-staging route.
- Select one authoritative Hub database. `PilotOwner` deliberately rejects an
  existing unenrolled database or a missing enrolled owner lock. Do not silently
  adopt the live `hub.sqlite3`, recreate a missing lock or change shared-directory
  permissions. A cold enrollment/migration requires a coherent backup, stopped
  writers, explicit operator approval and preservation of existing records.
- Prove independent review, exact artifact deployment, health success, failed
  candidate rollback, interrupted-work reconciliation and restart behavior.

A synthetic transport test proves composition behavior only. A private TEST
database on the NAS may prove remote mechanics, but does not prove integration
with the existing live dashboard's records. Container recreation, actual NAS
acceptance and machine reboot acceptance remain separate checks.

## Installed selector (opt-in source)

The separate entrypoint is intentionally not selected by ordinary Hub startup:

```text
python -I -B -m engineering_hub.unified_pilot_startup validate --config /var/lib/hub/settings.json --pilot-config /var/lib/hub-private/pilot.json
python -I -B -m engineering_hub.unified_pilot_startup serve --config /var/lib/hub/settings.json --pilot-config /var/lib/hub-private/pilot.json
```

`validate` reads the existing non-secret deployment settings and protected pilot
profile. It does not read agent environment tokens, the executor credential or
CA file, acquire an owner, bind a socket, contact an executor or modify a database.
Its result means configuration shape is valid, not installed readiness.
For version-two protected storage, the selector enforces that the profile is
exactly `/var/lib/hub-private/pilot.json`. The former shared-tree private path is
not the selective protected-layout recipe. Validation also checks the existing
ledger boundary's metadata; it does not open/enroll the database. These command
forms are references, not instructions to run `serve` now. Initial protected
cutover instead starts the separately documented owned-inactive service.

`serve` is the explicit activation operation. It retains the current process UID
(including the NAS app's UID568), existing three agent tokens, deployment-selected
Hub and bus paths, and agent8770/browser8771. `PilotOwner(require_existing=True)`
requires both the existing database and ownership lock; missing state is never
created or adopted. It rotates an already valid owner epoch under the normal
ownership protocol, so do not use `serve` as a read-only probe.

The closed pilot profile contains:

- `schema`: `engineering-hub.unified-pilot.v1`.
- `manifest`: the full existing canonical pilot manifest.
- `machine_id`: `staging-pc`; `action_key`: `engineering-hub:container-pilot`.
- Explicit `allowed_actors` and canonical `allowed_task_ids`; these are an
  additional veto, not task assignment, independent review or execution proof.
- `executor`: `https_origin`, `ca_file`, `ca_sha256`, `credential_file` and
  `credential_sha256`. Both hashes use the `sha256:` prefix. Never put a bearer
  value in this JSON. The endpoint needs a certificate matching its actual DNS
  name or IP address; verification is never disabled for a LAN address.
- `supervisor_stop_grace_seconds`, at least the existing1120-second requirement.
  This field does not change or verify the installed supervisor.

The credential may live in a private0700 child of the existing EH mount, with
current-UID ownership, mode0600, no links and trusted ancestors. Downloadable bus
content and collisions with database/settings/profile/CA/audit files are rejected.
No extra host mount is mandatory. Inspect actual NAS permissions before setup;
the selector never changes them. A separate read-only secret mount is an optional
deployment choice, not a change to the current Compose.

Serving verifies the protected credential digest and refuses reuse of an existing
agent/admin bearer. It verifies the CA digest, then builds TLS trust from those
same captured bytes. The dedicated client enforces certificate and hostname
verification, no system/environment CA or proxy discovery, no TLS key-log
environment handling, no redirect and no transport retry. This follows the
explicit-context APIs in [Python SSL](https://docs.python.org/3/library/ssl.html)
and [HTTPX](https://www.python-httpx.org/advanced/ssl/); the actual configured
network route still needs independent acceptance.
