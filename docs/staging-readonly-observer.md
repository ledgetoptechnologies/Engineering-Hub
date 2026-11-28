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

`deployment/staging-observer/nginx-http.conf` is a BLOCKED, non-installing
NGINX http context draft. It must not be deployed. Independently review
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

Independent review identified three unresolved proxy trust boundaries. NGINX
may normalize duplicate Authorization headers before forwarding `$http_authorization`,
so the observer may see only one header and cannot enforce its duplicate-header
rejection on the original request. NGINX error logs may include request URIs,
even with access logs disabled. Finally, source filtering depends on actual TCP
peer addresses; inherited real-IP rewriting or forwarded headers could change the
meaning of `allow`. A dedicated configuration without real-IP rewriting and a
tested duplicate-header rejection mechanism are required, along with a reviewed
error-log policy. No complete mechanism has been validated here. Do not infer
safety from the template or static test; a syntax check alone would not resolve
these boundaries.

A bounded isolated test of NGINX 1.26.3 with a dedicated main-context
`error_log /dev/null info` and disabled access logs produced no request logs or
stderr on normal startup, duplicate Authorization, malformed URI/query, or
upstream failure. Discarding error logs sacrifices operational diagnostics;
observe process health separately without collecting request headers or URIs.
A missing-certificate startup failure still wrote its certificate path to stderr
before the configured log destination took effect. Independently review service
stderr handling and configuration/path contents: `/dev/null` does not prove
startup failures or all versions and modules are safe. In the same isolated
configuration duplicate Authorization produced 400; method, query, encoded and
case variants produced 404; an unapproved loopback peer with a spoofed forwarded
IP produced 403. These are version-specific tests, not production acceptance.

To repeat the isolated, synthetic proxy checks with the existing cached test
image, set `EH_TEST_NGINX_PROXY=1` and run
`pytest tests/test_staging_observer_proxy_integration.py`. The harness never
pulls an image or publishes a host port; it runs a bounded networkless container
and removes its exact task-owned container. It skips if the Docker runtime or
cached image is unavailable. These checks do not prove production configuration,
startup-failure secrecy, kernel firewall behavior, or TLS acceptance.

The opt-in suite also runs `tests/test_staging_observer_tls_integration.py`
using two cached immutable images sharing a networkless namespace and a
task-owned volume, with no published ports. It tests the real Hub client,
observer and proxy using synthetic TLS and a fabricated protected snapshot.
The collector is not run: the snapshot is fixture data. Both containers use root for fixture creation and the Hub client; the observer
process drops supplementary groups and runs as synthetic UID/GID 12345 with its
own mode-0600 token. Root-owned snapshots and ancestor protections are checked,
including unsafe modes. This does not validate the actual service account, Hub
UID 568 file permissions, host firewall or live NAS route. Both containers and the volume are removed and absence verified.

The same isolated test composes the actual Hub application and exercises its
authenticated runtime route using a synthetic internally created human session.
It also checks wrong CA and wrong bearer through the real `StagingObserver`
client, in addition to raw HTTP negative checks. It does not exercise a real
identity provider or deployment.
