# v0.6.0: safe sends and honest subscription presence

## Useful additions for all agents

- Optional `send_message(client_request_id=...)`: atomic sender-scoped duplicate protection across retries, concurrent writers and restart. Same payload/key returns the original retained message; changed payload conflicts. Existing keyless callers retain their behavior.
- `get_send_receipt(client_request_id)`: private sender-only metadata reconciliation without reading a peer's inbox. Receipts survive message pruning; old keys cannot silently produce a replacement send. Acceptance does not mean task execution happened exactly once.
- `presence_status()`: server-observed active persistent SSE streams and live inbox subscriptions, with timestamps and no session/credential details. A stored subscription alone does not count. Subscribed does not mean ready, delivered, read or authorized.
- `bus_info` adds live presence and recovery guidance, examples and current schemas. Stale dedicated-worker advice now reflects explicitly owner-selected topic conversations.

REST equivalents: authenticated `GET /api/message-receipts?client_request_id=<your-key>` and `GET /api/presence`. REST keyed conflicts return 409; matching replays of pruned messages return 410. MCP errors are `send_conflict` and `send_replay_unavailable`.

## Contracts and limits

24 MCP tools, schema generation 4. Key strings are exact ASCII `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` and agent-scoped across topics/sessions, not trimmed or normalized. Key tombstones have no automatic expiry; this deliberate retention cost prevents duplicate acceptance after message cleanup. See [safe sends and presence](safe-sends-and-presence.md).

Presence is process-local transport observation, not a fleet-wide readiness service. No client heartbeat writes, database/inbox polling, model calls, extra scheduler, new memory platform, new mount or permission expansion. The existing native push/cursor and keyless message contracts remain compatible. Session authentication/ownership, exact desktop wake and reboot recovery remain separate gates. The deleted mcp-worker profile is not recreated.

## Verification

The integrated candidate passed 260 tests. Real UID/GID568, read-only-root, one-bind, loopback-only disposable containers passed 158 checks: 69 collaboration/storage/search checks, 49 cursor/feedback checks, 40 native push/presence/retry/restart checks. The latter includes 50 concurrent real HTTP sends converging to one durable delivery, no duplicate notification on replay, private sender receipts, actual stream/subscription intersection, unsubscribe/disconnect, real keepalive, container restart and post-restart original-receipt recovery. The sender deliberately discarded a real HTTP response, then reconciled and retried after container restart with the original message ID. Subscription-generation regressions also protect replacements from both successful and failed stale notifications, including reused SDK session objects. Raw Uvicorn access logging is disabled by the packaged entrypoint; redacted application request metadata remains enabled. A real REST receipt query is exercised before scanning container logs for key/token/body absence. Source archives include the Docker entrypoint and regression-test its presence. No production container was restarted by these tests.

Independent review, built-source/installed-wheel checks and exact published registry/asset readback are release gates, not implied by source changes. A known Starlette/httpx test-client deprecation warning remains; no dependency upgrade is bundled merely to hide it.

## Update

Use the existing TrueNAS app's supported pull/redeploy action after the verified image is published. Preserve the installed single volume source/destination, tokens, database/audit paths, network restrictions and runtime 568:568. Take a coherent SQLite-plus-object backup, stop old writers and retain the previous image. A Docker restart alone does not fetch a new image. No replacement empty data directory or mixed-version writers.

After deployment, confirm live version 0.6.0/schema4, 24 tools and healthy native subscriptions. Refresh each agent's selected tools/argument schema, including `get_send_receipt` and `presence_status`. Use `bus_info(topic="presence")` and `bus_info(topic="recovery")`. Client aliases and existing session wake connectors are not automatically installed or reassigned by a server update.
