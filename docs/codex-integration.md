# Codex integration plan (no cutover)

Codex uses recipient `codex` and sends replies to recipient `hermes`. It must use only the Codex bearer token and cannot read messages addressed to Hermes. The Windows watcher must be a Scheduled Task or equivalent that starts at boot/logon and polls every five minutes or on the approved schedule.

Codex is not assumed to be online continuously. The bus keeps pending messages in SQLite until a Codex process claims them. A client crash leaves the finite claim lease to expire; the message then becomes retryable. A successful acknowledgement is durable and is retained before cleanup. Codex replies through the bus, not `Hermes/Inbox/` and not an outbox, only after cutover approval.

The watcher should use a bounded receive limit, claim before work, reject with a concise reason when it cannot process a message, and acknowledge only after the requested work has completed. It should handle 401, connection failure, and duplicate claim responses without deleting local state. Store tokens outside the repository and never put them in task arguments or logs.

Before cutover, run the two-client live smoke test, restart the service, verify pending delivery, test token isolation, and document the Windows task name and rollback command. Existing Codex configuration remains unchanged in this phase.
