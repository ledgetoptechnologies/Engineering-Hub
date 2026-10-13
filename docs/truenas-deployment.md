# TrueNAS SCALE deployment guide

This is an operator guide, not a deployment record. The prepared Compose file is [`../compose.truenas.yaml`](../compose.truenas.yaml); no image has been published and no TrueNAS service has been changed by this work. The image tag is `0.2.0`, so wait until the matching GHCR package version exists before installing it.

## Before opening the app editor

1. **GHCR access:** this repository/package is private. Keep package visibility private. Configure TrueNAS with a GitHub Container Registry credential allowed to read this package (`read:packages`); use the TrueNAS registry credential mechanism or log in interactively on the Docker host. Never put a PAT in Compose, a URL, or this repository. GitHub documents that container packages default to private.
2. **Local persistent data:** create a local ZFS dataset/folder such as `/mnt/<pool>/apps/internal-mcp`. Set it writable only by the runtime identity, UID/GID `10001` (for example, from the TrueNAS shell: `mkdir -p /mnt/<pool>/apps/internal-mcp && chown 10001:10001 /mnt/<pool>/apps/internal-mcp`). Replace `/mnt/POOL_NAME/...` in the Compose file with the actual path. **Do not use an SMB/NFS share** for the SQLite DB: SQLite WAL/SHM and audit/file data belong together on local storage.
3. **Choose the right addresses:** the sample uses TrueNAS `192.168.50.80`, Hermes `192.168.60.92`, and the Windows Codex/Claude machine `192.168.20.151`. Replace them if the actual interfaces differ. The app binds to one TrueNAS interface and the application allowlist contains only named client IPs; mirror that policy in the host/router firewall. Do not use `0.0.0.0`, an empty allowlist, a whole-VLAN range, public DNS, or a public tunnel.
4. **Generate and install four different tokens:** Hermes, Codex, Claude (only when the released image includes Claude-token support), and admin. Use a password manager or an approved secret generator. Replace every `REPLACE_WITH_...` value in the Compose environment before start. Keep the admin token out of agent configurations. Environment variables are visible to Docker administrators; if your TrueNAS release provides a secret-injection mechanism compatible with this application, use that rather than leaving credentials in a broadly readable app form.
5. Back up any existing service data before a cutover. This package does not replace the reported staging service at `192.168.50.90:8765` or update the Hermes bridge.

## Install

In TrueNAS SCALE's custom-app/Compose editor, paste the contents of `compose.truenas.yaml` after making the edits above. The compose has no build context, binds host networking to the explicit TrueNAS interface address, runs as non-root, drops all capabilities, and has a bounded health check and JSON log rotation. Because host networking is used, there is no `ports:` mapping.

If the TrueNAS app editor does not support `network_mode: host`, stop rather than silently replacing it with a port mapping: verify the source IP seen by the app and update the firewall/allowlist design first. Do not use `docker compose down -v` during normal upgrades; it removes named volumes if the deployment is later changed to use them.

For shell-based validation on the TrueNAS host before installation:

```bash
docker compose -f compose.truenas.yaml config --quiet
```

Once the package is published, pull the exact version (or preferably a recorded image digest), start the app, and verify `/health` reports `ok: true`. Test invalid-token rejection and connectivity from only the approved Hermes and Windows client addresses. Confirm the data directory is writable by UID/GID `10001`, then restart once and verify database, audit, and file state persist.

## Private package pulls

GitHub's Container registry keeps new packages private by default. The GitHub Actions workflow can publish with the repository's `GITHUB_TOKEN`; the TrueNAS host still needs read access. If TrueNAS cannot use a private-registry credential, do not make this private repository/package public as a workaround—ask the operator to provision the supported pull credential.

## Rollback

Record the previous image digest and take a consistent backup/snapshot before an upgrade. Roll back by stopping the new app and restoring the previous image/dataset snapshot according to the operator's approved procedure. Keep the existing Hermes watcher/bridge intact until a separately approved cutover; do not delete or migrate unrelated VaultBridge data.
