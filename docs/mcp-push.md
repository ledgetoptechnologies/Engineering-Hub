# Native MCP inbox resource subscriptions

Native resource subscriptions use a Linux inotify source attached to the existing SQLite WAL database. On supported local Linux filesystems, `create_app()` starts it automatically during the FastMCP root-app lifespan, waits for watch installation and one head-only startup snapshot, and only then enables `resources.subscribe`. The source has no timer, cron, interval query, model heartbeat, or polling fallback.

## Activation and health

`create_app(..., push_events_operational=...)` accepts:

- `None` (default): auto-enable only when Linux inotify/epoll/eventfd are available and the database path is on a recognized local filesystem. Startup additionally verifies that the existing database is in WAL mode and that watches/readiness succeed. Unsupported platforms/filesystems run with subscriptions disabled and unadvertised.
- `True`: require the native source. Unsupported paths or startup/readiness failures abort application startup; the service never silently falls back to polling.
- `False`: explicitly disable native subscriptions, for fixtures and compatibility tests.

The `push_events_operational` parameter name is retained for caller compatibility; `True` now means “require the native source,” not “trust a manual in-process signal.” `PushPublisher` can still be instantiated directly with `enabled=True` in isolated tests that explicitly publish manually.

`GET /health` includes `push.state`, `push.subscriptions_enabled`, `push.backend`, and, once running, the source cursor. A runtime source failure logs a CRITICAL message, flips subscription health off, clears active registrations, and sends subscribed clients a standard `notifications/message` with `resource_subscriptions_unavailable`. Further subscribe attempts fail; new MCP sessions no longer negotiate subscribe capability. No replacement source or polling path is started.

## Resource contract

The authenticated bearer identity is the only source of the recipient name. `resources/list` returns that identity's concrete inbox URI:

```text
agentbus://inbox/<authenticated-identity>
```

Clients subscribe and unsubscribe on that exact URI using standard MCP `resources/subscribe` and `resources/unsubscribe`. An update is only `notifications/resources/updated` with `params.uri` set to the inbox URI; it contains no message body, message ID, or JSON-RPC request ID. It is an invalidation hint, not message delivery.

Read pages with `resources/read`:

```text
agentbus://inbox/codex?after=0&limit=50
```

`after` is an exclusive durable delivery cursor (default `0`); `limit` is bounded to 1–100 (default 50). The response JSON contains `messages` and `next_cursor`. The resource reads only messages for the authenticated recipient. Bodies are returned only to that recipient; messages are not claimed or acknowledged. Persist the cursor and read again after reconnect. Subscribe before the initial catch-up read so a concurrent commit is either included in the read or prompts another read.

## Event source and cursor reconciliation

The event source watches the database inode, WAL inode, and their exact directory entries. Inotify readiness is only a hint. For startup and each relevant filesystem batch it takes one `BEGIN IMMEDIATE` writer barrier before querying the durable sequence head and rows, so an uncommitted WAL transaction is never announced as committed. A held writer is allowed one bounded SQLite busy wait; timeout fails the source closed.

At startup, watches are installed first, then the adapter receives a `None` cursor and captures the current AUTOINCREMENT head without reading historical message bodies or generating historical notifications. Later reconciliations page `message_delivery_sequence` in bounded pages (512 rows by default), selecting only `recipient` and sequence number; gaps from retention are skipped and the cursor advances to the observed head. The publisher retains only recipient names, never body or ID data.

The source fails closed on inotify queue overflow, database/WAL inode or directory-entry replacement, non-WAL startup, corrupt sequence metadata, a failed barrier, or delivery scheduling failure. The health state and subscribed clients make runtime failures observable. It does not retry by polling.

All SQLite repository inserts—including REST, MCP, and other processes—are observed from the same filesystem event source. MCP send has no direct publish hook, so the same commit is not double-notified. The in-process publisher uses a bounded dirty bit per active subscription: commits coalesce while a send is in flight, and a commit arriving after the prior client read causes one follow-up invalidation. At most 32 subscription send tasks exist, and one scheduled drain per recipient is retained; neither path builds an unbounded queue.

## Example client calls

```python
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession
from pydantic import AnyUrl

uri = AnyUrl("agentbus://inbox/codex")
async with streamable_http_client(url, http_client=http_client) as (read, write, _):
    async with ClientSession(read, write, message_handler=on_message) as session:
        await session.initialize()
        await session.subscribe_resource(uri)
        page = await session.read_resource(AnyUrl("agentbus://inbox/codex?after=0&limit=50"))
        # On each resource-updated notification, read after the saved next_cursor.
        await session.unsubscribe_resource(uri)
```

## Keepalive and session recovery

The tested MCP 1.30 transport uses `sse-starlette`'s server-originated SSE comment keepalive every 15 seconds on its persistent GET stream. These comments are transport liveness signals, not inbox queries, resource notifications or reasons to invoke a model. A real quiet-stream test observes the wire comment while event-source snapshot reads remain unchanged. Clients should use a finite read-idle timeout covering several keepalives (for example 45–60 seconds), rather than waiting indefinitely on a dead connection.

MCP session IDs and subscriptions are process-local. Server restart, explicit DELETE or expiry invalidates them. A stale session can return HTTP404; on that response, a terminated stream or a transport read timeout, create a fresh MCP session, initialize, subscribe to the same authenticated inbox, and catch up from the last durably saved cursor. Reconnect backoff is for transport recovery only, never scheduled inbox/model polling. Do not clear the cursor or replay an uncertain write automatically. Runtime `resource_subscriptions_unavailable` means fail closed and report the source failure, not quietly continue with a dead subscription or switch to polling.

## Bounds and authentication

The in-process registry caps active subscriptions at 32 and removes registrations on explicit unsubscribe or authenticated Streamable HTTP `DELETE`. Failed notification sends drop their stale registration. There is no background SQLite query while the source is idle.

The MCP middleware authenticates every request and binds each returned MCP session ID to the principal that initialized it. Subsequent `GET`, `POST`, and `DELETE` requests with a missing/unknown or differently authenticated session are rejected; an invalid bearer token remains `401`. This binding is separate from the resource recipient check.

FastMCP 1.30 provides no subscribe/unsubscribe decorator and hard-codes resource subscription capability to false. The implementation registers the low-level resource handlers on the existing FastMCP server and wraps only its capability calculation behind a guarded SDK-surface check. FastMCP remains the ASGI root, preserving its session-manager lifespan and all 21 tools.

## Provenance and verification

`src/agent_message_bus/sqlite_events.py` adapts the proven Linux inotify + eventfd + epoll + `BEGIN IMMEDIATE` design from `/home/bkoltz/.hermes/cache/scratch/mcp-push-os-events/sqlite_inotify.py`. The standalone proof contract and captured evidence are in:

- `/home/bkoltz/.hermes/cache/scratch/mcp-push-os-events/contract.md`
- `/home/bkoltz/.hermes/cache/scratch/mcp-push-os-events/evidence.json`

The repository tests use real Streamable HTTP clients, a real loopback ASGI server, and independent SQLite writer processes:

```bash
env -u PYTHONPATH uv run --locked pytest tests/test_native_push_events.py tests/test_mcp_push.py -q
env -u PYTHONPATH uv run --locked pytest -q
```

Coverage includes automatic startup and shutdown, head-only startup catch-up, page-bounded cursor reads with retention gaps, no idle event-source SELECTs, recipient-only MCP/REST/process invalidations, body/ID absence from notification wire data, concurrent process commits and retained final cursor, rollback silence, in-flight dirty-bit regression, overflow and database-replacement fail-closed behavior, client-visible source failure, disabled unsupported-platform capabilities, and authentication/session binding for `GET`, `POST`, and `DELETE`.
