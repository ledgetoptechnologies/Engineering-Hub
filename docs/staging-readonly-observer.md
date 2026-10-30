# Snapshot-only staging observer

This source-only optional observer serves the existing sanitized host snapshot at
`GET /v1/runtime` without constructing a worker, Docker backend, journal, queue,
or execution endpoint. It does not itself collect inventory. A separately
provisioned root-owned `hub-staging-runtime-collector` supplies the snapshot using
its fixed read-only commands. Missing, invalid, stale, or future-dated snapshots
remain unavailable or stale; no execution readiness is inferred. The worker
source is explicitly not configured. Existing Prometheus metrics remain separate.

Run as an unprivileged service with no Docker socket, against a protected
collector output, with a dedicated private bearer token, e.g. `python -m
hub_staging_service.observer --snapshot /operator/path/snapshot.json --token-file
/operator/private/token --port 8781`. It listens only on IPv4 loopback; a separate
operator-provisioned TLS proxy must authenticate and restrict the actual NAS
source IP, preserve the bearer, and admit only the exact GET path. Use a pinned
CA and matching certificate identity. Do not expose it through the public tunnel.
The observer trusts only the actual loopback peer, not forwarded headers. The
service has no installer, persistence, TLS, credential generation, or collector
scheduling. Configure process resource bounds and protect the snapshot directory
and every ancestor as required by the existing inventory reader.

This does not connect the Hub dashboard by itself. Independent review, a private
proxy and CA/token handoff, installed build verification, and end-to-end
acceptance are required before reporting a live connection. Nothing here changes
machine holds or authorizes jobs, worker activation, or host cleanup.

The optional Hub connection reads `staging-observer.json` beside the ordinary
staging configuration. Its exact keys are `schema` (1), `enabled` (boolean),
`origin` (private literal-IP HTTPS), `credential_file`, `ca_file` (absolute
protected files visible to the Hub), and `admin_actors` (nonempty exact human
IDs). Invalid, missing, or disabled configuration leaves it unavailable. The
observer takes precedence for the runtime read when separately configured;
project work and the execution service retain their existing configuration and
queue. This is on-demand only and uses the existing pinned-CA client, no proxy
or redirects, 15-second total deadline, and 128 KiB response cap. The configured
TLS proxy must enforce the actual NAS source IP; Hub authentication remains the
existing human session plus explicit observer administrator. Installation and
end-to-end testing remain separate operator work.

The staging service wheel also installs `hub-staging-runtime-collector`; it is
the same implementation as the compatibility `scripts/collect_staging_runtime.py`
launcher. Use a root-owned, immutable wheel installation for both observer and
collector, with a root-owned snapshot directory, and verify the exact artifact
identity before enrollment.
