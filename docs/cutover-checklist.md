# Safe integration / cutover checklist

No publish, deployment, watcher change, or client configuration change is part of the packaging task. An operator must explicitly approve any live cutover.

- [ ] `uv run pytest -q` passes and both Compose files validate.
- [ ] Build the image and verify a healthy non-root container; record its immutable digest.
- [ ] Preserve the private GHCR visibility and confirm TrueNAS has only the required `read:packages` access.
- [ ] Use a local ZFS dataset for DB/WAL/SHM, audit, and artifacts; verify permissions for UID/GID `10001` and backup/restore.
- [ ] Review host-interface binding, exact agent source-IP allowlist, and host/router firewall. Do not enable public exposure.
- [ ] Test each supported agent with its own token; verify invalid tokens fail and one agent cannot access another agent's messages.
- [ ] Discover MCP tools from the actual released image instead of assuming fixed tool names/counts.
- [ ] If shared-knowledge tools are present, test a disposable note and verify explicit write/read behavior. Do not describe this as automatic long-term-memory or vault synchronization.
- [ ] Verify an idle Codex/Claude/Hermes process is not woken solely by server registration. The optional `scripts/wake_daemon.py` is a separate, unconfigured integration and requires explicit approval before service installation.
- [ ] Restart the service and verify database, audit, message, and file persistence.
- [ ] Keep the existing Hermes watcher/stdio bridge intact until a separate cutover is approved; record a rollback plan that resumes the known-good integration without deleting data.
- [ ] Get operator approval before changing live agent configuration or enabling a watcher/scheduled task.
