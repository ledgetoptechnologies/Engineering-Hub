# Current integration inventory (operator-reported)

This page records the integration facts supplied for the packaging work. They were not independently verified against the remote hosts. The repository checkout and the live staging service are separate; this work does not change either system.

## Hermes

- The operator reports an `agent_bus_watcher.py` entry associated with reference `b7a1b3d63138`; the exact local path/scheduler semantics are not asserted here.
- The existing Hermes stdio bridge replies only to Codex and exposes five tools. It is not the shared HTTP MCP service and does not provide shared-knowledge integration.
- No live watcher, cron, bridge, Hermes config, or inbox state was modified.

## Codex and Claude Code

- Both run on the Windows machine at `192.168.20.151` (per operator handoff).
- There is no Codex config or Claude CLI on the Hermes host; do not treat the Hermes host as their client machine.
- No existing Codex/Claude configuration was changed. See [`three-agent-integration.md`](three-agent-integration.md) for example client-side setup.

## Service addresses and deployment boundary

- A staging bus at `192.168.50.90:8765` was reported healthy for ten days; this task did not probe it and its host has no checkout of this repository.
- The prepared TrueNAS Compose sample targets the separate host/interface `192.168.50.80`; it is not deployed.
- Reported client source IPs for the sample allowlist: Hermes `192.168.60.92`, Codex/Claude Windows host `192.168.20.151`. Confirm before using.
- Restrict LAN access at both the application allowlist and host/router firewall. No public tunnel, DNS record, or whole-VLAN exposure is part of this packaging task.

## Shared knowledge and wake-up limits

The current checkout implements `knowledge_put`, `knowledge_get`, `knowledge_list`, and `knowledge_search`; verify discovery from the exact release image before relying on them. Writes are version-checked, search is literal substring matching, and each enabled agent can access the same knowledge set, so do not store secrets there. These tools are not connected to the reported staging host by this task and do not synchronize agent memory. MCP registration does not wake an idle agent. The optional `scripts/wake_daemon.py` is a separate long-poll service and was not installed or configured; enabling it requires explicit approval. See [`wake-daemon.md`](wake-daemon.md) and [`three-agent-integration.md`](three-agent-integration.md).

Keep the retained [`vaultbridge-audit.md`](vaultbridge-audit.md) as historical audit evidence. It is not a description of the new service's live state.
