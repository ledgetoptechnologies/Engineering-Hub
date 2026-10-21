# Engineering Hub

Engineering Hub is the private development/staging management platform, including
the Agent Message Bus from the renamed `internal-MCP` repository. The current
owner-selected deployment combines the dashboard and MCP while preserving agent
identities and existing data. Source, image publication, operator deployment and
live acceptance are separate states; this README does not certify the NAS state.

## Start here: current unified deployment

Use [the small deployment guide](docs/simple-hub-deployment.md) and
[`compose.hub-unified.yaml`](compose.hub-unified.yaml), not the legacy standalone
bus/control examples below.

- Agents: `http://192.168.50.80:8770/mcp`, with their existing separate tokens.
  No old-port alias or replacement empty message history.
- Humans: `https://hub.ledgetoptechnologies.com`, through the existing tunnel to
  browser port 8771. Hub verifies signed Cloudflare Access identity; this is a
  separate Access application from Ops, not an additional GitHub login.
- One host mount: `/mnt/Plugins/App_Data/Engineering-Hub:/var/lib/hub`.
  Non-secret `settings.json`, Hub state and preserved bus state live below it.
  Keep credentials out of the settings file and Git.
- Image: `ghcr.io/ledgetoptechnologies/engineering-hub-control:latest`, promoted
  from tested `main` builds. Pull and redeploy to update; restarting does not pull.

Normal Hub startup does **not** enroll a host executor or enable autonomous live
deployment. The [actual bounded staging pilot](docs/staging-pilot-acceptance-2026-10-21.md)
proved native queued candidate failure, verified rollback and clean shutdown in
an isolated TEST environment. It did not prove live NAS-to-staging dispatch,
production activation, or reboot recovery. The optional executor still needs a
repeatable installation and cross-host integration before general enrollment.

See [shared notes and tracking](docs/shared-notes-and-tracking.md) for the human
and agent record interfaces, [Hub control v1](docs/hub-control-v1.md) for the
implementation contract/history, and [`contracts/hub-v1.openapi.json`](contracts/hub-v1.openapi.json)
for the REST contract. Project filters alone are not authorization for outside
collaborators; external access requires enforced project permissions first.

## Agent Message Bus capabilities

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

- The current unified profile separates agent and browser ingress. Browser port
  reachability is not authentication: unsigned or forged LAN requests must fail.
- The legacy local `compose.yaml` example binds to loopback only. The standalone
  TrueNAS bus example binds to one LAN interface and allows listed client IPs;
  it is not the current unified Hub installation path.
- Use a distinct bearer token for every enabled agent and keep the admin token separate.
- The legacy standalone TrueNAS sample binds to one host interface, uses an application source-IP allowlist, runs as UID/GID `568:568`, and drops Linux capabilities.
- Keep SQLite, its WAL/SHM sidecars, the audit log, and files together on a local ZFS dataset. Do not put SQLite WAL on SMB/NFS.
- The current package/repository are private. GHCR package visibility is not changed by this workflow. TrueNAS needs authorized `read:packages` access to pull a private image.
- Do not expose the agent MCP ingress through public DNS or the browser tunnel.
  Forward only the authenticated browser surface for Hub's public hostname.

## Develop and test the standalone bus

These are local fixture commands for the independently testable legacy bus, not
instructions to restore port 8765 or its old data mount on the live NAS.

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

## GHCR images

The current Hub image is built by `.github/workflows/hub-control.yml`. Tested
`main` publication promotes the verified registry digest to
`ghcr.io/ledgetoptechnologies/engineering-hub-control:latest`. PR checks do not
publish. Registry publication is not operator deployment or executor enrollment.

The separate legacy bus workflow below retains its own release-tag policy;
its absence of `latest` does not apply to the Hub package.

`.github/workflows/container.yml` runs tests, Compose validation, and a container build for pull requests and `main`. A SemVer tag such as `v0.6.0` runs the same gates and publishes:

- `ghcr.io/ledgetoptechnologies/internal-mcp:0.6.0`
- `ghcr.io/ledgetoptechnologies/internal-mcp:sha-<full-commit-sha>`

Stable releases also publish the moving `stable` tag; there is intentionally no mutable `latest` tag. Protect `v*` Git refs and do not move/rebuild a release tag; pin a digest for a deployment that needs content-addressed immutability. The workflow uses the repository-scoped `GITHUB_TOKEN` with `packages: write` and publishes OCI source, version, and revision labels. GitHub leaves a first-published package private by default; this project keeps that visibility.

## TrueNAS SCALE: current versus legacy

For the current Hub, follow [the small deployment guide](docs/simple-hub-deployment.md)
with `compose.hub-unified.yaml` and the single Engineering-Hub data root. Preserve
existing tokens and coherent SQLite-plus-object state; do not reintroduce the
retired bus mount. Validate the actual Access identity, forged LAN rejection,
preserved data and agent connections after an operator update. Container checks
do not prove actual NAS or reboot acceptance.

The following is retained **only for standalone bus deployments**, not the
current Hub:

Use [`compose.truenas.yaml`](compose.truenas.yaml) as a prebuilt-image compose file (no `build:` block). Before installing, follow [`docs/truenas-deployment.md`](docs/truenas-deployment.md): authenticate TrueNAS to the private package, create a local ZFS data path, set its ownership to UID/GID `568:568`, replace all four secret placeholders, and adjust the LAN bind and client allowlist to the real addresses. For updates, preserve the installed volume mapping and tokens exactly. Verify the selected package is published and back up the existing SQLite database and file objects before pulling/redeploying. A container restart alone does not fetch a newer image.

## Three-agent integration

For durable shared notes, work tracking and the human dashboard interface, see
[`docs/shared-notes-and-tracking.md`](docs/shared-notes-and-tracking.md).

The separate Linux-only [fixture executor](hub_executor/CONTRACT.md) and
`compose.hub-test.yaml` are isolated acceptance tools, not application deployment
adapters. They do not enable dispatch in the default Hub or modify the existing
message-bus service. See the [qualified integrated test evidence](docs/hub-linux-validation-2026-10-17.md)
and the separate [application-pilot contract draft](docs/hub-container-pilot-contract-draft.md).

See [`docs/three-agent-integration.md`](docs/three-agent-integration.md) for the Windows PowerShell Codex and Claude Code setup, the Hermes client snippet, separate-token handling, and the limits around agent wake-ups and shared knowledge. It documents intended setup only; it does not assert that any client is configured.

Other operational references: [`docs/current-integration-inventory.md`](docs/current-integration-inventory.md), [`docs/cutover-checklist.md`](docs/cutover-checklist.md), [`docs/wake-daemon.md`](docs/wake-daemon.md), [`docs/file-management.md`](docs/file-management.md), and the retained [`docs/vaultbridge-audit.md`](docs/vaultbridge-audit.md).
