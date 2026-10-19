# Safe sends, receipts and subscription presence

Available in v0.6.0 (schema generation 4). Refresh client tool discovery/allowlists after deploying: `get_send_receipt` and `presence_status` are new. Existing keyless sends remain compatible.

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

## Help and topic sessions

Use `bus_info(topic="presence")` for a live snapshot, `bus_info(topic="recovery")` for retry rules, and `bus_info(topic="commands")` for live schemas. `/info` aliases remain client-owned.

An explicitly owner-selected conversation remains the topic owner. Presence does not authenticate client-supplied session labels or reassign sessions. The deleted `mcp-worker` profile is not required and must not be recreated as a fallback. No scheduled inbox/model polling is enabled.

## Engineering Hub exact-topic preflight (candidate source)

The Hub interface is separate from the old message-bus interface above. Use
Hub's authenticated `hub_topic_peers(topic)` and
`hub_topic_preflight(topic, recipient)` MCP tools, or
`GET /api/hub/v1/topics/{topic}/preflight?recipient=hermes`. Keep the exact
discussion topic and the current authenticated agent identity. Do not infer
readiness from a reply in another topic or a global transport connection.

Preflight now includes `guidance`:

| Recipient state | Send mode | Response expectation |
| --- | --- | --- |
| Online, fresh owning-runtime evidence | `notify_if_still_reachable` | `may_respond` |
| Busy, verified input queue | `notify_if_still_reachable` | `may_be_delayed` |
| Explicitly disconnected/offline | `queue_only` | `not_verified` |
| Unknown, expired, stream-only or unverified | `queue_only` | `not_verified` |
| Authoritatively no exact-topic binding | `link_first` | `not_verified` |

`durable_delivery_required` and `receipt_required` are always true;
`delivery_guaranteed` remains false. A recipient can disconnect just after
preflight. Send meaningful requests through durable addressed storage, retain
their send key, and report accepted/pending rather than delivered. Do not send
empty wake messages, repeatedly resend the same request, or start a model or
replacement listener just because another agent is unavailable. Presence is
not task ownership or permission to take over an uncertain external operation.

The dashboard calls the verified idle route **Online · verified** and the
verified queue-capable route **Busy**, not **Queued**. The latter must not imply
that any particular message has been accepted. Readiness expires locally at its
deadline; new authoritative evidence is required to turn it green again.

### Current operational gap

Packaged startup now supports explicit, default-off canonical registry wiring:
`HUB_REGISTRY_ENABLED=1` plus `HUB_REGISTRY_ORIGIN` selects the existing read-only
snapshot adapter. `HUB_REGISTRY_EVENTS_ENABLED=1` separately permits on-demand
native event streams; it requires registry enablement. Flags accept only `0`
or `1`. The origin must be HTTPS except literal loopback HTTP, with no userinfo,
path prefix, query or fragment. Existing deployments/Compose files are not
changed by this source feature, and the defaults still inject no bridge.

Construction and startup make no registry request or background subscription.
Authenticated agent reads preserve each request's original bearer; there is no
stored provider credential, agent impersonation header, cross-agent cache,
timer scan or automatic reconnect worker. An explicitly injected bridge cannot
be combined with the configured source. A human session does not inherit an
agent token: owner dashboard reads still need a separately approved fixed-topic
reader resolver. This bootstrap deliberately does not create that grant.

The upstream canonical registry and owning-session evidence must actually exist
and implement the published snapshot/event contracts. Configuring a URL does
not create that backend, link a session, wake an agent or establish health.
The bus's `presence_status` reports transport connections only, not
same-topic selected-runtime readiness. Therefore an unconfigured or unavailable
source returns **Unknown** and queue-only guidance, never fabricated online
status. Wiring the owner's published canonical registry, authenticated topic
membership and selected-session observer is still required for actual live
presence. No registry, heartbeat writer, scheduled polling, connector or service
is activated by these source changes.

Verification: 327 independently run backend presence/registry tests passed;
19 UI unit tests and the build passed. Two focused real-browser tests against
mock API fixtures passed for evidence expiry and topic switching. These are not
production registry, native agent wake, or live dashboard acceptance results.

Source bootstrap verification: 433 focused registry/client/stream/app/ingress/
default-contract tests passed on Windows, one platform skip, with ResourceWarning
errors enabled (9.54s; existing Starlette warning). Independent review accepted
the change and reran 282 focused tests. The SSE test checks the actual upstream
`/subscribe` path and a forwarded invalidation/cursor, not merely an unavailable
fallback. Synthetic transports prove the wiring only; no live registry,
credential, deployment, watcher or equipment was provisioned.
