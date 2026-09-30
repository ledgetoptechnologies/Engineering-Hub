# Current integration inventory

## Hermes

- Watcher: `~/.hermes/scripts/vault_bridge_watcher.py`.
- Cron job: `8ca807033aa2`.
- Interval: every five minutes.
- Watched path: `Hermes/Inbox/` only; path/mtime fingerprints suppress the agent on `NO_CHANGE`.
- Direction: the intended inbox-only design is Hermes writing to `Codex/Inbox/` and Codex writing to `Hermes/Inbox/`; outboxes are not part of the new design.
- Existing integration state: the VaultBridge watcher and cron remain unchanged during this build.

## Codex

Codex runs on Beau's main rig and is not assumed to be continuously powered on. A future watcher must be restart-safe and should run as a Windows Scheduled Task or equivalent, starting at boot/logon and polling approximately every five minutes. Pending bus messages remain durable until Codex claims and acknowledges them.

## Network facts

- Hermes host: `192.168.60.92`.
- TrueNAS/VaultBridge: `192.168.50.80`.
- Beau's main rig/Codex: `192.168.20.151`.
- The first bus phase binds to loopback. A later LAN phase may bind to `0.0.0.0` only with application allowlisting for the two explicit client IPs and a host firewall rule. No public tunnel, Cloudflare route, DNS record, or whole-VLAN exposure is part of this repository.

## Files and systems that must remain unchanged

Do not modify the current VaultBridge deployment, its Markdown notes, or its semantic index. Do not modify `~/.hermes/scripts/vault_bridge_watcher.py`, Hermes cron job `8ca807033aa2`, Hermes configuration, Codex configuration, or any existing inbox files during local acceptance. The bus uses its own `data/` directory and does not mount unrelated host paths.
