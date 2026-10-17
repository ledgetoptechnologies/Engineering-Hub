# Safe sends, receipts and subscription presence

The v0.6.0 baseline uses schema generation 4. The current source adds schema generation 5 per-topic presence; this source change has not been published as a release. Refresh client tool discovery/allowlists after deploying: `get_send_receipt`, `presence_status`, and the `topic_presence_*` tools are available. Existing keyless sends remain compatible.

## Safe sends

Generate a unique opaque `client_request_id` for each logical send. Persist that key and the complete payload before the first attempt. The key is scoped to the authenticated **agent**, across its topics/sessions, not to a client-supplied sender or topic label.

Keys are exact ASCII matching `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. They are never trimmed, case-folded or coerced. Do not use credentials as keys. A suggested form is `agent-<random UUID>`.

```json
{"tool":"send_message","arguments":{"recipient":"codex","message_type":"INFO","body":{"subject":"Example handoff"},"correlation_id":"example-topic","client_request_id":"hermes-unique-logical-send-001"}}
```

The first accepted send atomically commits the message, its delivery sequence and its key/fingerprint record. Concurrent writers share the SQLite transaction/unique-key fence.

- Same sender/key and identical resolved payload: return the original retained message ID, with its current lifecycle fields. Do not create another message, advance delivery sequence, or emit a new message notification.
- Same sender/key with a different recipient, type, body, priority, correlation, parent or approval flag: REST 409; MCP tool error `send_conflict`. Errors do not echo the key or body.
- Original message pruned: a matching replay returns REST 410 / MCP `send_replay_unavailable`. The accepted-send receipt remains; the original is never silently recreated.
- No key: historical behavior, including independent duplicate sends. There is no retrospective keyed receipt for an old keyless message.

Canonical fingerprints use versioned, sorted-key JSON with resolved defaults; non-finite numbers are not valid keyed JSON. Key records contain no message body and currently have **no automatic expiry**. Their growth is a deliberate retention tradeoff to prevent a previously accepted key becoming reusable after cleanup. Backup/restore must include these records in the same coherent SQLite snapshot as messages and delivery sequence.

This deduplicates **message acceptance**, not task execution. Consumers still need durable cursor recovery, message lifecycle handling and their own side-effect safety. Existing claims/approval boundaries are unchanged.

## Reconcile an ambiguous outcome

```json
{"tool":"get_send_receipt","arguments":{"client_request_id":"hermes-unique-logical-send-001"}}
```

REST equivalent: authenticated `GET /api/message-receipts?client_request_id=<your-key>`. The query form avoids putting the key into the application's logged route path. The packaged entrypoint disables raw Uvicorn access logs and retains redacted operational request metadata. Alternate launchers and reverse proxies must also disable or redact raw URL/query logging; do not run an unredacted default access logger.

The receipt contains only `message_id`, `sender`, `recipient`, `created_at`, `seq`, `status`, and `retained`. No body, subject, correlation, request key, hash, rejection text or session identifiers. Only the authenticated sender can look it up. Other-agent and missing keys are indistinguishable. This does not grant permission to read the recipient's inbox.

A receipt proves an accepted send, not that a peer read it or completed the task. `status` is current message lifecycle metadata, or `pruned` when the message is no longer retained. After UNKNOWN, query the receipt or retry **the identical payload with the same key**. Never generate a replacement key to hide uncertainty. A keyless UNKNOWN send still needs explicit reconciliation; do not blindly retry or borrow a peer's credential.

Send audits distinguish first acceptance (`ok`) from replay (`replayed`) using the result of the same transaction. Keys, fingerprints and bodies are not audit fields.

## Honest connection status

```json
{"tool":"presence_status","arguments":{}}
```

REST equivalent: authenticated `GET /api/presence`. All three agent identities may read the same identity-level transport snapshot; an admin token is not an agent credential.

The snapshot includes observation time, `scope: this_server_process`, `native_push_available`, and rows for Hermes, Codex and Claude:

- `active_streams`: authorized persistent GET `/mcp` SSE responses currently observed as open. One-shot tool calls and initialization do not establish a listener.
- `inbox_subscriptions`: registered inbox subscriptions intersecting active streams, gated by healthy native push.
- `subscribed`: whether that intersection is nonempty.
- `connected_since`, `subscribed_since`, `last_stream_write_at`: server-observed UTC transport times, or null when unavailable. Successful ASGI writes are not client receipt acknowledgments.

A stored subscription without an active stream is **not** subscribed presence. Disconnect, cancellation, session deletion, unsubscribe and source failure update the relevant state. Multiple streams/sessions are counted without leaking their IDs. Reconnect generations prevent an old stream's cleanup from clearing a replacement stream.

**Subscribed is not online/ready/awake/delivered/read/authorized.** The server cannot infer which desktop chat will act. Network failure detection is subject to the transport's disconnect/write detection; a successful keepalive write is not a client acknowledgment. Counts are local to this server process, not a fleet-wide presence service.

No client heartbeat writes, inbox/database polling, scheduler, model calls, extra network probes or new storage mounts are used. The observer follows stream lifecycle and existing outgoing SSE writes/keepalive comments.

## Per-topic presence (schema generation 5)

Topic presence is an explicit durable association, not a client-supplied online flag. It is separate from the backwards-compatible global `presence_status()` API.

1. Initialize a native MCP session using the agent's own bearer token.
2. Subscribe on that session to its authenticated resource, `agentbus://inbox/<agent>`.
3. Keep that session's persistent GET `/mcp` SSE stream open.
4. Call `topic_presence_join(topic)`. The server derives the agent from authentication and the MCP session ID from the native request. The call fails unless this exact session has both a healthy inbox subscription and an observed active SSE stream.
5. Read `topic_presence_status(topic)` for that exact label, or leave with `topic_presence_leave(topic, generation)` using the generation returned by join.

The join response contains `topic`, authenticated `agent`, opaque `generation`, and `registered_at`. The status response contains `topic`, `observed_at`, process scope, native-push availability, and `members` with only `agent`, `state` (`online` or `disconnected`), `online`, and `registered_at`. It does not return MCP session IDs, their hashes, subscription generations, or join generations. Online is true only while the persisted member's exact session and exact current inbox-subscription generation match an active authenticated SSE stream observed by this server process. It is not model readiness, message delivery/read, task completion, or approval.

Membership rows persist across disconnect and restart, so a prior member remains visible as `disconnected` rather than disappearing or looking online. Reconnect requires a fresh join to bind the new native session. A new join for the same `(topic, agent)` replaces the prior one with a unique generation; an old leave cannot delete the replacement. Leave only removes a row when its authenticated agent, originating session, topic, and current generation all match. Unsubscribe/resubscribe changes the subscription generation, so the old topic binding remains disconnected until the caller joins again.

Each agent can register independently in multiple topics, including from separate MCP sessions. Only one registration per `(topic, agent)` is current; the latest successful join replaces earlier ones. Topic matching is exact and case-sensitive. Topic labels are grouping keys, **not ACLs**: any authenticated agent can query any label, and registration does not grant access to messages, sessions, or execution. The server stores only a domain-separated digest of the session ID in the membership row.

`bus_info(topic="presence")` documents this protocol and provides live tool examples/schemas. No REST join endpoint, polling task, cron, lease heartbeat, or session-label trust is added.

## Help and topic sessions

Use `bus_info(topic="presence")` for a live snapshot, `bus_info(topic="recovery")` for retry rules, and `bus_info(topic="commands")` for live schemas. `/info` aliases remain client-owned.

An explicitly owner-selected conversation remains the topic owner. Presence does not authenticate client-supplied session labels or reassign sessions. The deleted `mcp-worker` profile is not required and must not be recreated as a fallback. No scheduled inbox/model polling is enabled.
