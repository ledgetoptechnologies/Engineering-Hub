# Hermes, Codex, and Claude Code integration

This guide describes a possible client setup for the private Agent Message Bus. It does not modify any client, publish an image, or prove that the clients are connected. Use the server URL for the deployment you have actually approved: the prepared TrueNAS compose binds to `http://192.168.50.80:8765/mcp`; the separately reported staging endpoint is `http://192.168.50.90:8765/mcp`. Do not mistake one for the other.

## Current state and what this is not

The operator handoff reports a Hermes `agent_bus_watcher.py` entry with reference `b7a1b3d63138`, plus an existing local stdio bridge that replies only to Codex and exposes five tools. That bridge is not this HTTP service, does not establish three-agent connectivity, and is not evidence of shared knowledge. Codex and Claude Code run on Windows `192.168.20.151`; there is no Codex configuration or Claude CLI on the Hermes host. The staging service at `.50.90:8765` was reported healthy for ten days, but it has no checkout of this repository. These are handoff facts, not checks performed by this packaging task.

MCP registration makes server tools available to an already running client. It does not start or wake an idle Hermes, Codex, or Claude process. This checkout also contains an optional host-side CLI wake helper, [`scripts/wake_daemon.py`](wake-daemon.md), which polls separately and can start bounded CLI workers; it is not part of Compose, installed, or configured for any agent by this packaging work. A daemon is a separate integration and requires explicit operator approval plus each worker's own authenticated MCP configuration. It never resumes a desktop conversation.

Likewise, a message database is not automatic agent memory. Agents only share content that a tool call explicitly stores and retrieves. The current checkout implements four shared-knowledge MCP tools: `knowledge_put`, `knowledge_get`, `knowledge_list`, and `knowledge_search`. Confirm that these appear in `tools/list` from the exact published image before relying on them. `knowledge_search` is a literal, case-insensitive substring search—not embeddings or semantic search. Writes use an `expected_version` compare-and-swap (`0` creates a document); each UTF-8 document is limited to 64 KiB. All enabled agent tokens can access the shared knowledge set; there is no per-project/per-agent ACL, so do not store secrets or private content there. Knowledge tools do not automatically import any vault, transcript, or notes directory, and they do not synchronize Hermes long-term memory, Claude memory, or Codex session history.

## Identity and network rules

Use one unique token per client: `HERMES_TOKEN`, `CODEX_TOKEN`, `CLAUDE_TOKEN` (supported by this checkout; verify the published image includes this version), and a separate `ADMIN_TOKEN`. Keep each token out of source control and logs. Do not give an agent the admin token. Recipient identity and capabilities are determined by the authenticated token on the server, not by asking the client to claim another identity.

The `/mcp` URL is reachable only from allowlisted client IPs, with different bearer tokens for each identity. Message recipients are `hermes`, `codex`, and `claude`; delivering a message does not wake the recipient. Both Windows clients share a source IP, so distinct bearer tokens still matter. Change the allowlist if the actual client IP changes; also enforce the same narrow rule in the host/router firewall. Plain HTTP is for the restricted trusted LAN only. Do not expose this endpoint to the public Internet.

## Hermes client

On the Hermes host, add the remote HTTP server to the existing `mcp_servers` section of `~/.hermes/config.yaml` (merge the entry; do not replace the file):

```yaml
mcp_servers:
  internal_agent_bus:
    url: "http://192.168.50.80:8765/mcp"
    headers:
      Authorization: "Bearer ${AGENT_BUS_HERMES_TOKEN}"
```

Put `AGENT_BUS_HERMES_TOKEN=<the Hermes-only token>` in `~/.hermes/.env` using the host's secure local workflow, protect that file from other users, then restart Hermes or reload its MCP configuration. Verify with `hermes mcp list` and `hermes mcp test internal_agent_bus`; use the client tool list to confirm which tools actually loaded. Hermes supports `${VAR}` expansion for MCP URLs and headers from its environment, including `~/.hermes/.env` (see the official [Hermes MCP guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)).

Do not remove or rewrite the existing stdio bridge as part of this setup. Parent/operator approval is required before changing a live Hermes watcher, cron, or bridge.

## Codex on Windows PowerShell

Codex stores MCP servers in `%USERPROFILE%\.codex\config.toml` (or `$HOME\.codex\config.toml`), not in the old `mcp.json` location. Open the existing file and merge this TOML table; do not overwrite other Codex settings:

```toml
[mcp_servers.internal_agent_bus]
url = "http://192.168.50.80:8765/mcp"
bearer_token_env_var = "AGENT_BUS_CODEX_TOKEN"
enabled = true
```

In the same PowerShell window that will start the Codex CLI, enter the token without putting it in shell history, then launch Codex:

```powershell
$secure = Read-Host -Prompt 'Codex bus token' -AsSecureString
$env:AGENT_BUS_CODEX_TOKEN = [Net.NetworkCredential]::new('', $secure).Password
Remove-Variable secure
codex mcp list
codex
```

The environment value is process-scoped; a separately launched Desktop process may not inherit it. Restart that client from a shell that has the variable, or use an approved Windows secret-injection mechanism. Confirm the server is enabled and tools are listed before using it. Codex's HTTP MCP configuration fields are documented in the official [Codex MCP guide](https://developers.openai.com/codex/mcp).

## Claude Code on Windows PowerShell

Configure a user-scoped HTTP server using the official `claude mcp add` command. Claude Code supports `${VAR}` expansion in a remote server's `Authorization` header; use a custom variable name rather than a provider-reserved credential name:

```powershell
claude mcp add --scope user --transport http internal-agent-bus `
  http://192.168.50.80:8765/mcp `
  --header 'Authorization: Bearer ${AGENT_BUS_CLAUDE_TOKEN}'
```

Enter the unique Claude bus token into the environment for the same PowerShell process that starts Claude Code, then verify the server:

```powershell
$secure = Read-Host -Prompt 'Claude bus token' -AsSecureString
$env:AGENT_BUS_CLAUDE_TOKEN = [Net.NetworkCredential]::new('', $secure).Password
Remove-Variable secure
claude mcp list
claude
```

In the interactive Claude Code session, use `/mcp` to verify connection and tool discovery. `${AGENT_BUS_CLAUDE_TOKEN}` is a reference, not a secret value; never replace it with the actual token in a checked-in `.mcp.json` or shareable config. A separate GUI-launched process may not inherit a process-scoped variable. Claude's remote HTTP registration and header expansion are documented in the official [Claude Code MCP guide](https://code.claude.com/docs/en/mcp).

## Safe verification order

1. Check the intended server's `/health` endpoint from an approved client network. Health status is not proof of authenticated MCP access.
2. Connect each client with its own bearer token and inspect its discovered tools. Verify an invalid token is rejected.
3. Test one addressed message round trip using non-sensitive test content. Confirm each identity can access only its authorized messages.
4. If shared-knowledge tools are present in the image, verify they are discoverable and exercise only a disposable test note. Confirm the tools do not automatically import or synchronize unrelated memory stores.
5. Confirm an idle client stays idle unless a separately approved watcher/scheduler starts it.
6. Obtain operator approval before changing a real Hermes watcher/bridge or persistent client configuration.
