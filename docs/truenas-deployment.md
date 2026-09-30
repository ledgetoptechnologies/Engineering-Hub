# Future TrueNAS deployment

This document is preparation only. Do not deploy this repository to TrueNAS in the local acceptance phase, and do not modify the existing VaultBridge service.

A future Custom App deployment should use the built image with one persistent dataset mounted only at `/var/lib/agent-message-bus`. The dataset must contain the SQLite database, WAL/SHM sidecars, and redacted audit JSONL. Set an explicit non-root UID/GID with ownership and permissions that allow the container to write the dataset but not unrelated datasets. Do not mount the VaultBridge Markdown vault or its semantic index.

Bind the service to `192.168.50.80` or the selected TrueNAS interface only after application and host firewall allowlists permit Hermes `192.168.60.92` and Beau's main rig `192.168.20.151`. Add a restart policy and a health check for `/health`; do not configure public DNS, Cloudflare, a tunnel, or whole-VLAN access.

Before deployment, create a consistent SQLite backup, verify restore into a disposable instance, and document the image digest. Rollback is to stop the new app, restore the prior image/dataset snapshot if needed, and resume the current VaultBridge watcher. Never delete or migrate VaultBridge notes as part of rollback. Production deployment requires explicit Beau approval after the cutover checklist is complete.
