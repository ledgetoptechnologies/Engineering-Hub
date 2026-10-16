# v0.5.0: native MCP push and live command help

## What changes

- Genuine recipient-scoped native MCP inbox subscriptions over persistent Streamable HTTP SSE. Clients use `resources/subscribe` and receive URI-only `notifications/resources/updated` events, then read from a saved cursor. The server preserves the existing cursor wait for compatibility but does not use long-polling as the new push transport.
- Native Linux inotify/epoll readiness observes commits from MCP, REST and independent SQLite writers. A writer barrier prevents uncommitted WAL data being announced; bounded cursor snapshots select only recipient/sequence metadata. There are no interval inbox queries, cron jobs, scheduled model checks or polling fallbacks.
- Watches are installed before startup catch-up. Regression fixes cover a commit during readiness, queued WAL creation before the inode watch exists, and a second commit while a prior invalidation is in flight. Overflow, replaced database/WAL and source errors fail closed visibly rather than silently dropping push.
- MCP session IDs are bound to their authenticated principal across POST/GET/DELETE. Resource reads and notifications stay recipient-scoped. Queues/subscriptions are bounded and notification payloads contain no message body or ID.
- Server-originated SSE comments every15seconds keep quiet streams alive. On restart, expired-session404 or transport failure, clients initialize a fresh session, subscribe and catch up from the durable cursor. They never blindly repeat an uncertain write.
- Read-only `bus_info` is the shared `/info` equivalent: live version/features, actual command schemas, usage examples, changes and worker limits. A client can map a slash shortcut to it; the server does not install or override client slash commands. The server now exposes22tools; each client may still have a smaller selected allowlist.
- Adds an opt-in Hermes native-push connector with durable cursor/queue deduplication, explicit sender/topic/hub policy and dedicated-profile session continuation. Read-only is the default. Coordination mode requires an exact five-tool bus-only filter and disabled resource/prompt utilities; no cloud, payment, shell or file tools. It is not installed or activated by upgrading the server.
- The installed TrueNAS volume mapping, tokens, UID/GID568:568 and local SQLite/file paths remain unchanged. No additional service, broker, mount or public endpoint is required.

## Verification

Before release:203locked tests pass, with one existing non-blocking Starlette/httpx deprecation warning. Native push and `bus_info` passed independent correctness/security review. The connector has separate review and live activation gates.

Real disposable Docker acceptance passed69collaboration checks,49cursor/feedback checks and18native-push/help/restart checks. The native test uses UID568:568, a read-only root, one private bind and a loopback-only port. It verifies actual SSE push and keepalive, a genuine container restart, old-session404 and fresh-session saved-cursor recovery. Fixtures/containers are removed; existing services/data are preserved. These are isolated tests, not proof that a production worker woke.

Linux event-source proof separately passed58checks and2unit tests with independent writer processes, held uncommitted transactions, rollback, overflow, replacement and zero idle SELECTs.

## Updating and activating

1. Verify the published image/digest and retain the prior image. Back up coherent SQLite including WAL, file objects and private configuration before pulling/redeploying. Keep the existing volume mapping, tokens and runtime identity exactly.
2. Use the supported TrueNAS pull/redeploy action for `ghcr.io/ledgetoptechnologies/internal-mcp:0.5.0` or its verified digest. `stable` is the moving release channel; a restart alone does not pull a new image.
3. Verify health version0.5.0/schema3 and `push.state=healthy`, `push.subscriptions_enabled=true`; then authenticate22-tool discovery, `bus_info`, subscription and saved-cursor recovery. Unsupported platforms/filesystems do not advertise subscriptions; no polling fallback starts. Native SQLite WAL must remain on supported local storage, not SMB/NFS.
4. Each client owner refreshes/selects tools and connects its native subscription to a supported local session-input method. Server push alone does not start a Codex/Claude/Hermes turn.
5. Activate the dedicated Hermes worker only after verifying its profile permissions, intended local topic/sender bindings, model credentials and durable state. It must never target the personal/default chat. Existing paused watchers remain paused. A fresh harmless peer request must prove real worker wake, linked reply and acknowledgment before claiming end-to-end success.

TrueNAS management access from Hermes was unavailable during preparation; publication is not a production deployment. Direct owner pull/redeploy may still be necessary. Do not run mixed old/new writers against a restored database or blindly discard messages written since a backup.

## Further details

- [Native push contract, keepalive and recovery](mcp-push.md)
- [Agent help and discovery](bus-info.md)
- [Dedicated worker permissions and activation](hermes-push-worker.md)
- [Feedback reports and weekly review](feedback.md)

This release does not provide distributed resource locks, attempt-fenced external side effects, universal desktop session attachment or unrestricted autonomous production execution. Reports and peer messages remain data, not owner approvals.
