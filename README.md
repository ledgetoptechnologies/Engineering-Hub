# Agent Message Bus

A self-hosted, authenticated MCP Streamable HTTP service for Hermes, Codex, and Claude Code. It provides durable agent messaging, shared-file storage, audit records, and (when included in a release) shared-knowledge tools. SQLite and artifacts live in the service's persistent data directory.

This repository prepares a private GHCR container package and a TrueNAS SCALE Compose deployment. **This change does not publish, deploy, or alter any agent configuration.** See the integration guide for the reported current state and what MCP does—and does not—connect automatically.

## Security boundaries

- The local `compose.yaml` example binds to loopback only. The TrueNAS file binds to one LAN interface and allows only listed client IPs.
- Use a distinct bearer token for every enabled agent and keep the admin token separate.
- The TrueNAS sample binds to one host interface, uses an application source-IP allowlist, runs as UID/GID `10001`, and drops Linux capabilities.
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

`.github/workflows/container.yml` runs tests, Compose validation, and a container build for pull requests and `main`. A SemVer tag such as `v0.2.0` runs the same gates and publishes:

- `ghcr.io/ledgetoptechnologies/internal-mcp:0.2.0`
- `ghcr.io/ledgetoptechnologies/internal-mcp:sha-<full-commit-sha>`

There is intentionally no mutable `latest` tag. Protect `v*` Git refs and do not move/rebuild a release tag; pin a digest for a deployment that needs content-addressed immutability. The workflow uses the repository-scoped `GITHUB_TOKEN` with `packages: write` and publishes OCI source, version, and revision labels. GitHub leaves a first-published package private by default; this project keeps that visibility.

## TrueNAS SCALE

Use [`compose.truenas.yaml`](compose.truenas.yaml) as a prebuilt-image compose file (no `build:` block). Before installing, follow [`docs/truenas-deployment.md`](docs/truenas-deployment.md): authenticate TrueNAS to the private package, create a local ZFS data path, set its ownership to UID/GID `10001`, replace all four secret placeholders, and adjust the LAN bind and client allowlist to the real addresses. Do not deploy until the `0.2.0` package has actually been published and the operator approves the cutover.

## Three-agent integration

See [`docs/three-agent-integration.md`](docs/three-agent-integration.md) for the Windows PowerShell Codex and Claude Code setup, the Hermes client snippet, separate-token handling, and the limits around agent wake-ups and shared knowledge. It documents intended setup only; it does not assert that any client is configured.

Other operational references: [`docs/current-integration-inventory.md`](docs/current-integration-inventory.md), [`docs/cutover-checklist.md`](docs/cutover-checklist.md), [`docs/wake-daemon.md`](docs/wake-daemon.md), [`docs/file-management.md`](docs/file-management.md), and the retained [`docs/vaultbridge-audit.md`](docs/vaultbridge-audit.md).
