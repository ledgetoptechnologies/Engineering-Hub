# Explicit unified queued-pilot composition

Status: independently reviewed source integration, not an activated NAS workflow.
Normal `--config` startup remains unchanged and does not select this service.

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
This is an implementation choice, not a claim of installed or tested networking.

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
