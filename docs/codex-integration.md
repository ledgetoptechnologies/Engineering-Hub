# Codex integration notes

Codex runs on the Windows client at `192.168.20.151` per operator handoff. No Codex settings were modified by this packaging task. Use the copy/paste Windows PowerShell and TOML instructions in [`three-agent-integration.md`](three-agent-integration.md#codex-on-windows-powershell); they target `~/.codex/config.toml`, use a separate bearer token, and do not put the secret in the TOML file.

MCP settings do not start an idle Codex process. The client must be running to use the tools, or a separately approved Windows watcher/scheduled task must start and poll it. Do not claim shared notes or automatic memory sync until the released knowledge tools are visible and explicitly exercised.
