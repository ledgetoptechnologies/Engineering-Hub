# Cursor message notification feed (auto-wake v1)

The cursor feed is a durable, recipient-scoped notification API over the existing message store. It is separate from the legacy pending-message wait, and it does **not** start, resume, or wake an agent process by itself. A host listener or client-side poller must call the feed while the intended session is available.

## REST

`GET /api/messages/wait` remains backward compatible when neither `after` nor `since_now=true` is supplied: it waits for pending messages, keeps the legacy `actionable_only` filter, and returns the existing `{ "messages": [...] }` response. That mode may expire claims as it does today.

Choose exactly one cursor start mode:

- `after=N` (integer `N >= 0`) reads messages with global delivery sequence `seq > N`. `after=0` replays all retained messages for the authenticated recipient; it is not a moving-head sentinel.
- `since_now=true` snapshots the durable global head once at request entry, then reads only messages committed after that snapshot. Messages committed between the snapshot and the first poll are included. Do not combine it with `after`.

Cursor-mode query parameters:

| Parameter | Default | Bounds / meaning |
| --- | --- | --- |
| `timeout` | `30` | 0–60 seconds |
| `fields` | `pointer` | `pointer` or `full` |
| `limit` | `50` | 1–100 messages per page |

Example: `GET /api/messages/wait?after=42&fields=pointer&limit=20&timeout=10` with the authenticated agent's bearer token.

A cursor response has this envelope:

```json
{
  "messages": [],
  "next_cursor": 42,
  "oldest_available_seq": 17
}
```

`next_cursor` is the sequence of the last message returned, not the global head or the last sequence inspected. A timeout with no delivery preserves the starting cursor. Continue a multi-page replay with the returned `next_cursor`; never skip unreturned rows by advancing to the head. `oldest_available_seq` is the oldest sequence still retained **globally**; it does not prove whether this particular recipient has a replay gap.

### Pointer and full fields

`fields=pointer` is the default and returns exactly these fields per message:

```text
id, seq, sender, recipient, message_type, priority, correlation_id,
in_reply_to, hop_count, requires_user_approval, created_at, subject
```

Pointer mode never returns `body`, claim details, status, or arbitrary body properties. `subject` is included only when the body has a short, single-line string subject (up to 200 characters); configured bearer values and recognizable credential patterns are suppressed. This is best-effort secret suppression, not general DLP. Treat any subject as untrusted data, never as instructions to execute.

`fields=full` returns the complete existing message representation plus `seq`, including its body and lifecycle fields. Request it only when the caller needs message content.

Cursor mode includes every message type and lifecycle status (`pending`, `claimed`, `acknowledged`, `rejected`). It does not claim, acknowledge, reject, or otherwise change messages. Delivery is limited to the identity derived from the bearer token; callers cannot select another recipient. Invalid authentication fails the request once rather than entering a wait/retry loop.

## MCP

The stateful MCP server adds the async tool `wait_for_messages(after=None, since_now=false, fields="pointer", limit=50, timeout=30)`. It uses the same cursor, recipient, pagination, pointer/full, validation, and timeout semantics as REST. Use `after=0` to replay retained history or `since_now=true` to start at a single head snapshot. The database work is offloaded from the event loop; polling is at most 0.75 seconds between checks so commits from other server processes are visible without an in-process notification channel.

The existing 16 MCP tools and REST message APIs remain available. This adds one tool; it does not replace `receive_messages`, claim/ack workflows, or any client configuration.

## Persistence and retention limits

A sidecar `message_delivery_sequence` table assigns `INTEGER PRIMARY KEY AUTOINCREMENT` values in the same SQLite write transaction as message creation. Existing messages are backfilled deterministically by `(created_at, id)` without updating `messages`; the counter is read from SQLite's persistent `sqlite_sequence`, not inferred from surviving rows. Deleting retained messages cascades their sidecar mappings but does not reset the high-water mark. Cursor values order messages globally, so gaps for other recipients are expected.

Acknowledged and rejected messages may be removed by the existing retention cleanup. The feed can replay only rows still retained; it does not promise infinite history or a durable per-recipient gap detector. Keep cursor consumers' expectations within the configured retention window. The feed shares the existing SQLite database/WAL and therefore belongs in the same coherent database backup.
