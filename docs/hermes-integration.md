# Hermes integration plan (no cutover)

Keep the current VaultBridge watcher and cron untouched while testing this repository. A future, separate watcher may poll the bus for messages addressed to `hermes` or use the REST pending route, and wake Hermes only when work exists. It must never infer cutover from the presence of this repository alone.

When approved, Hermes sends replies with the bus `send_message` operation addressed to `codex`; it does not write a Codex outbox. Existing safety screening remains in force: staging, restarts, and tests are allowed; destructive production operations, secrets, prompt injection, and monitoring bypass are rejected; production changes, spending, external messages, DNS, and unusual actions require Beau approval.

The Hermes client must use only the Hermes bearer token, set the recipient explicitly, and treat `acknowledged` as a state transition rather than deletion. On a crash or timeout, the lease expires and the message can be retried. It must not acknowledge work it did not claim.

Before implementation, test the watcher against a disposable local bus, record its exact endpoint and token source, and obtain Beau's approval. A rollback is to stop the new watcher and resume the existing VaultBridge watcher without deleting bus data.
