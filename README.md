# Engineering Hub

Engineering Hub is the private development/staging management platform under
construction. The repository was renamed from `internal-MCP`; its existing Agent
Message Bus remains independently deployable, with its current image identity,
port 8765, tokens and persistent storage unchanged.

The additive control-plane implementation is documented in
[Hub control v1](docs/hub-control-v1.md), with the REST contract in
[`contracts/hub-v1.openapi.json`](contracts/hub-v1.openapi.json). Its default port
is 8770 on loopback. Live OAuth and real host dispatch remain disabled until
their separate setup and acceptance gates pass; fixture tests are not production
deployment or TrueNAS reboot proof.

## Existing Agent Message Bus

A self-hosted, authenticated MCP Streamable HTTP service for Hermes, Codex, and Claude Code. It provides durable agent messaging, shared-file storage, audit records, and (when included in a release) shared-knowledge tools. SQLite and artifacts live in the service's persistent data directory.

This repository builds a private GHCR container package and a TrueNAS SCALE Compose deployment. Publication, deployment and client wake behavior are separate verification gates; source changes alone do not prove any of them.

## Safe sends and connection visibility

v0.6.0 adds optional sender-scoped `client_request_id` deduplication, private `get_send_receipt` reconciliation, and server-observed `presence_status`. Presence distinguishes an open native SSE stream/subscription from chat readiness or a read receipt. Existing keyless sends and native-only delivery remain compatible. See [safe sends, receipts and presence](docs/safe-sends-and-presence.md).

## Native event-driven MCP push

Native delivery uses recipient-scoped MCP resource subscriptions over persistent server-sent events. Native Linux filesystem events reconcile committed SQLite WAL changes; there are no periodic inbox queries or model checks. Clients subscribe before catch-up, then read their cursor only on actual notifications or reconnection. See [native MCP push](docs/mcp-push.md). A host/session connector is still required to turn a notification into agent execution.

## Help and discovery

Call `bus_info()` for the live version, features and tool list. Use `bus_info(topic="commands")` for actual argument schemas, or choose `messaging`, `push`, `presence`, `recovery`, `feedback`, `changes` or `worker` for examples and limits. This is read-only and never executes its examples. A client may map `/info` or `/bus-info` to this tool; the server does not override built-in client slash commands.

## Messaging and feedback

v0.4.0 adds a durable recipient-only cursor notification feed through REST and MCP, preserving the legacy wait contract. Initialize with `since_now=true`, then save and resume with `after=next_cursor`; `after=0` replays retained history. See [auto-wake v1](docs/auto-wake-v1.md).

The shared bug/improvement/info inbox supports idempotent submission, filtered pagination, revision-checked status changes and attributed evidence history. Reports are review items, never automatic execution requests. See [feedback tools](docs/feedback.md). Session connectors and full task/resource fencing are separate integrations, not guarantees provided by the notification feed.

## Security boundaries

- The local `compose.yaml` example binds to loopback only. The TrueNAS file binds to one LAN interface and allows only listed client IPs.
- Use a distinct bearer token for every enabled agent and keep the admin token separate.
- The TrueNAS sample binds to one host interface, uses an application source-IP allowlist, runs as UID/GID `568:568`, and drops Linux capabilities.
- Keep SQLite, its WAL/SHM sidecars, the audit log, and files together on a local ZFS dataset. Do not put SQLite WAL on SMB/NFS.
- The current package/repository are private. GHCR package visibility is not changed by this workflow. TrueNAS needs authorized `read:packages` access to pull a private image.
- Do not expose the endpoint through public DNS, a public tunnel, or a whole-VLAN firewall rule.

## Develop and test

```bash
uv sync --all-groups
uv run pytest -q
cp .env.example .env
# Replace every token placeholder; keep .env private (chmod 600 on POSIX).
mkdir -p data
AGENT_BUS_UID="$(id -u)" AGENT_BUS_GID="$(id -g)" docker compose -f compose.yaml up -d --build
curl -fsS http://127.0.0.1:8765/health
```

For an authenticated local MCP round trip, load the four local test tokens in your shell and run:

```bash
set -a
. ./.env
set +a
uv run python tests/live_mcp_smoke.py --url http://127.0.0.1:8765/mcp
```

The live smoke test discovers the server's tools and exercises message delivery, authentication, and REST state. Do not hard-code token values in shell history or commit `.env`.

## GHCR image

`.github/workflows/container.yml` runs tests, Compose validation, and a container build for pull requests and `main`. A SemVer tag such as `v0.6.0` runs the same gates and publishes:

- `ghcr.io/ledgetoptechnologies/internal-mcp:0.5.2`
- `ghcr.io/ledgetoptechnologies/internal-mcp:sha-<full-commit-sha>`

Stable releases also publish the moving `stable` tag; there is intentionally no mutable `latest` tag. Protect `v*` Git refs and do not move/rebuild a release tag; pin a digest for a deployment that needs content-addressed immutability. The workflow uses the repository-scoped `GITHUB_TOKEN` with `packages: write` and publishes OCI source, version, and revision labels. GitHub leaves a first-published package private by default; this project keeps that visibility.

## TrueNAS SCALE

Use [`compose.truenas.yaml`](compose.truenas.yaml) as a prebuilt-image compose file (no `build:` block). Before installing, follow [`docs/truenas-deployment.md`](docs/truenas-deployment.md): authenticate TrueNAS to the private package, create a local ZFS data path, set its ownership to UID/GID `568:568`, replace all four secret placeholders, and adjust the LAN bind and client allowlist to the real addresses. For updates, preserve the installed volume mapping and tokens exactly. Verify the selected package is published and back up the existing SQLite database and file objects before pulling/redeploying. A container restart alone does not fetch a newer image.

## Three-agent integration

See [`docs/three-agent-integration.md`](docs/three-agent-integration.md) for the Windows PowerShell Codex and Claude Code setup, the Hermes client snippet, separate-token handling, and the limits around agent wake-ups and shared knowledge. It documents intended setup only; it does not assert that any client is configured.

Other operational references: [`docs/current-integration-inventory.md`](docs/current-integration-inventory.md), [`docs/cutover-checklist.md`](docs/cutover-checklist.md), [`docs/wake-daemon.md`](docs/wake-daemon.md), [`docs/file-management.md`](docs/file-management.md), and the retained [`docs/vaultbridge-audit.md`](docs/vaultbridge-audit.md).
