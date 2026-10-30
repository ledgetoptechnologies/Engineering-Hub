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

## Inert private proxy example

`deployment/staging-observer/nginx-http.conf` is a non-installing NGINX http
context example, not a validated or running configuration. Independently review
and replace every placeholder with a verified bind address, distinct free ports,
certificate paths and actual NAS source. It accepts only the original exact
`GET:/v1/runtime` including absence of a query string; the dedicated bearer is
forwarded, not stored in nginx configuration. It strips other request headers and
request bodies. The existing observer checks its own bearer and actual loopback
peer. The client must verify the dedicated CA and literal-IP certificate SAN.
Never attach this to the public tunnel.

NGINX is not currently installed on the assessed host; its package provenance,
version, module support, service identity and resource limits need review. The
example alone does not configure a firewall, bound pre-header connections or
provision credentials. Enforce the measured NAS TCP source at the host/network
boundary and bound TCP connection admission there before exposing the listener.
HTTP `limit_conn` is not a pre-header or TLS-handshake connection limit. Header
and handshake timeouts do not establish a concurrent connection bound. Preserve
existing UFW rules and verify the effective policy before any narrow change.

Before activation, run `nginx -t` against the exact reviewed configuration and
operator-provisioned TLS material in an isolated test setup. Test correct and
incorrect source, CA, IP SAN and bearer; exact method/path/query variants and
body handling; redirects and duplicate authorization; incomplete TLS/headers
under bounded connection load; upstream unavailable/stale; and resource limits.
Then test the actual NAS-to-staging route and rollback of only the newly owned
proxy and firewall rule. Static checks and a successful syntax test do not prove
firewall enforcement, TLS peer identity, resource limits, or live readiness.
