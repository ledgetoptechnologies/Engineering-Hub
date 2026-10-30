# Snapshot-only staging observer

This source-only optional observer serves the existing sanitized host snapshot at
`GET /v1/runtime` without constructing a worker, Docker backend, journal, queue,
or execution endpoint. It does not itself collect inventory. A separately
provisioned root-owned `collect_staging_runtime.py` supplies the snapshot using
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

This does not connect the Hub dashboard by itself: its current runtime route is
coupled to the execution-capable staging module and expects an enabled project
configuration. A separate read-only Hub composition and authentication review,
private proxy and CA/token handoff, installed build verification, and end-to-end
acceptance are required before reporting a live connection. Nothing here changes
machine holds or authorizes jobs, worker activation, or host cleanup.
