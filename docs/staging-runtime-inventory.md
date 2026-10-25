# Staging runtime visibility

EH connects to a dedicated staging worker over private HTTPS with a separate
worker credential and operator-provisioned CA. It does not receive a Docker
socket, expose Docker TCP or SSH into the staging computer. Job containers
receive neither the worker credential nor a Docker socket.

`GET /api/hub/v1/staging/runtime` is an authenticated **human staging-admin**
read. Project read grants and agent tokens do not disclose the host-wide list.
The existing staging resource views remain project scoped. Its response is
`{configured, state, snapshot}`. A snapshot contains `schema` equal to
`hub-staging-runtime.v1`, `observed_at`, `sources`, `containers` and `services`.
Each source has `id` (`worker` or `host`), `state` (`observed`, `unavailable`,
`stale` or `not_configured`) and its own `observed_at` or null.

The overall `ready` state means a valid bounded observation was returned, not
execution authority or availability of every source. Current rows are omitted
when their source is stale/unavailable; an empty list alone is not healthy host
evidence. Current timestamps must be at most90seconds old and no more than
10seconds ahead. Responses are bounded to128KiB,256containers and256services.

Containers expose only ID, name, image reference, state, scope and `managed:false`.
Worker scope identifies the configured isolated daemon; host scope identifies
an optional host observation. Neither adopts an existing container into EH's
resource registry. There are no start/stop/delete controls for observed workloads.
Image references are observations, not latest-version comparisons. Commands,
environment, mounts, logs, internal endpoints and host paths are excluded.

## Existing host workloads

The unprivileged worker queries only its fixed local Unix Docker endpoint.
It must not receive the rootful socket to observe PA/Viewer. The optional
root-owned `scripts/collect_staging_runtime.py` runs two fixed read-only commands:
selected-field Docker container listing against the local rootful socket and a
bounded systemctl list of running services. It publishes sanitized JSON for the
worker, listens on no port and accepts no EH commands.

Its `--output` directory must already exist, be root owned and not writable by
the worker/others. `--once` collects once; otherwise its explicitly started
process refreshes every30seconds. Provision external CPU/RAM/PID limits and a
root-owned package path. It installs no service or boot autostart. The worker's
optional `host_snapshot` selects the absolute file. Reads check anchored
ancestors, no symlinks, owner/mode/size and freshness. Missing evidence stays
explicitly unverified.

## Modular connection and first installation

The ordinary Hub loads `staging-service.json` under its existing state root,
using its ordinary repository and shared work queue. The older protected-ledger
and PilotOwner cutover is **not** a modular build/test prerequisite. Its separate
deployment/recovery rules and old held campaigns remain intact.

Keep ordinary Compose/data mappings. A first-install bundle can be applied by
`scripts/install_staging_connection.py` inside the existing UID568 Hub container.
It validates an independently supplied bundle SHA256, explicit human admin IDs,
private HTTPS, certificate-only CA and dedicated worker token before effects.
It exclusively creates `staging-private` and `staging-service.json`, publishing
config last; existing targets are refused. It changes no deployment settings,
bus, ledger, ACL or existing installation. The privately transported bundle holds
a TEST credential: never put it in chat/Git. Retire that transport through the
operator's private handoff procedure after successful installation. The script
creates no credentials and does not restart the app.

Start with `auto_run:false` for verified status and owner policy setup. Actual
jobs still require explicit grants, reviewed offline recipes, accepted rootless
kernel limits, durable state and free storage. Do not lower a free-space floor
or prune unrelated apps to force admission. Preview hostname setup is independent
of private build/test submission.
