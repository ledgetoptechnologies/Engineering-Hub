# Safe cutover checklist

Cutover is not part of the local build. Beau must explicitly approve any live Hermes or Codex configuration change.

- [ ] `uv run pytest -q` passes.
- [ ] Docker Compose parses and the image starts.
- [ ] Health, REST, and live MCP tests pass with separate tokens.
- [ ] Hermes cannot read Codex-addressed messages and Codex cannot read Hermes-addressed messages.
- [ ] Sender identity is derived from the bearer token, not a request field.
- [ ] Duplicate claims are prevented; expired claims are retryable.
- [ ] Acknowledgement is not deletion.
- [ ] Retention deletes only old acknowledged/rejected rows and never pending or claimed rows.
- [ ] Audit verification passes and records contain no tokens or full bodies.
- [ ] Container restart preserves an acknowledged message and a new pending message.
- [ ] SQLite backup and restore has been tested with the service stopped or a consistent snapshot method.
- [ ] LAN binding, application IP allowlist, and host firewall rules are reviewed; no public exposure exists.
- [ ] A manual rollback to the current VaultBridge watcher is written and rehearsed.
- [ ] Beau approves changing either agent's live configuration.

Rollback: stop the new watcher/client, leave bus data intact for diagnosis, and resume the existing VaultBridge watcher. Do not delete or migrate VaultBridge notes as a rollback action.
