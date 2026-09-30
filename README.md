# Agent Message Bus

A small local FastAPI and FastMCP Streamable HTTP service for reliable Hermes/Codex messages. It uses SQLite in WAL mode, explicit per-agent bearer tokens, leases, retention, and a redacted hash-chained JSONL audit log.

## Scope and safety

This repository is independent of VaultBridge. It does not modify Hermes cron, Codex configuration, the VaultBridge deployment, or any existing vault data. The test deployment uses Docker host networking so the application can enforce real client IPs, binds on the Hermes host, and uses both bearer authentication and an application IP allowlist. LAN exposure is limited to the explicitly approved Codex source IP; there is no public tunnel or DNS.

## Development

```bash
uv sync --all-groups
uv run pytest -q
uv run uvicorn agent_message_bus.app:app --host 127.0.0.1 --port 8765
```

Configuration is read from environment variables; see `.env.example`. Use long, different random values for `HERMES_TOKEN`, `CODEX_TOKEN`, and `ADMIN_TOKEN`.

REST health is available at `/health`. Authenticated message routes are under `/api/messages`; MCP Streamable HTTP is at `/mcp`.

Authenticated Hermes/Codex clients can upload multipart artifacts to `POST /api/files` with a `path` form field and `file` part, list them with `GET /api/files`, read metadata with `GET /api/files/{path}` (or `/metadata`), and stream bytes with `GET /api/files/{path}/download`. Paths are relative to `AGENT_BUS_FILE_ROOT` (default `data/files`) and cannot traverse or follow symlinks. Only the admin token can delete files with `DELETE /api/files/{path}`. File metadata (size, SHA-256, content type, and timestamps) is stored in the message-bus SQLite database.

See [`docs/file-management.md`](docs/file-management.md) for the endpoint contract and path-safety details.

## Docker

```bash
cp .env.example .env  # replace all token placeholders
 docker compose config
docker compose up -d --build
curl -fsS http://127.0.0.1:8765/health
docker compose restart
curl -fsS http://127.0.0.1:8765/health
HERMES_TOKEN=... CODEX_TOKEN=... uv run python tests/live_mcp_smoke.py --url http://127.0.0.1:8765/mcp
```

The live smoke test lists all six MCP tools, performs a Hermes-to-Codex send/receive/claim/ack round trip, verifies REST state and invalid-token rejection, and prints `MCP smoke test passed`. A separate restart check should verify that both an acknowledged row and a newly pending row survive container restart.

Runtime data is persisted only in `./data`, including the `files` artifact root, mounted as `/var/lib/agent-message-bus`. No VaultBridge directory is mounted.

## Future integration

Read `docs/hermes-integration.md`, `docs/codex-integration.md`, and `docs/cutover-checklist.md` before any live integration. Do not change existing agent configuration until local, restart, authentication, retention, backup/restore, and approval gates pass.
