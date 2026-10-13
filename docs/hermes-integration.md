# Hermes integration notes

No live Hermes configuration was changed. The reported existing Hermes stdio bridge replies only to Codex and exposes five tools; it is separate from the prepared HTTP message bus and is not shared-knowledge integration.

For the example of adding the HTTP service as a *client* without replacing the existing bridge, see [`three-agent-integration.md`](three-agent-integration.md#hermes-client). It uses Hermes' `mcp_servers` HTTP configuration and an environment-expanded bearer header. Obtain operator approval before changing the active Hermes watcher, cron, bridge, or long-lived client configuration.

MCP registration alone does not wake Hermes. A watcher/scheduler must separately poll or launch it; this checkout's optional [`scripts/wake_daemon.py`](wake-daemon.md) is not installed or configured by this task. Shared knowledge is explicit tool-mediated storage/retrieval, not an automatic sync of Hermes persistent memory or VaultBridge notes.
