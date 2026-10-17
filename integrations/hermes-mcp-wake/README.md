# Agent Bus Wake: Hermes backend plugin

An opt-in native MCP inbox listener for explicitly linked conversations on the owning Hermes desktop backend. No CLI worker, cron job, inbox polling, or long-poll fallback is used.

## Installation and update boundary

Install `plugin.yaml` and `dashboard/` under the default home's `~/.hermes/plugins/agent-bus-wake/`, outside the Hermes core checkout. Add `agent-bus-wake` to `plugins.enabled` without changing other entries. The supported dashboard API hot-mounts on its first authenticated request. Call `POST /api/plugins/agent-bus-wake/start` with `{"server_name":"agent-bus-production"}` for the initial opt-in.

The router lifespan restores the saved opt-in on subsequent backend startups, without a renderer start button or timer. Code and state are outside the core checkout, so normal core updates do not overwrite them. Future gateway compatibility is not guaranteed: failures must remain fail-closed. Replacing an already-loaded Python module requires normal backend restart. Pausing is persistent.

This adapter supports the owner's loopback/SSH desktop backend and does not bypass OAuth-gated backend authentication. It uses the normal authenticated `/api/ws` JSON-RPC route through an in-process ASGI WebSocket. Tokens stay in memory and never appear in network URLs/access logs.

## Bindings and control

The authenticated `/api/plugins/agent-bus-wake` namespace exposes `/status`, `/servers`, `/start`, `/pause`, `/bindings` and `/bindings/{id}`. Status reports actual native subscription attachment, cursor, dispatch counts and `delivery_mode: "backend"`. Server discovery returns names only, not credentials.

POST `/bindings` with `{"topic":"exact-topic","stored_session_id":"owner-selected-stored-id","owner":{"connection_id":"local","profile":"default","target_profile":"default"}}`. Here `local` means this Python backend's own session store, not the renderer's physical location. The backend may be reached over SSH. Any explicitly selected conversation in this default-profile store can be linked. Other profiles are not modified. Each server/recipient/topic maps to one selected conversation; reassignment requires unlinking first. Ambient foreground/latest chats are never adopted.

Agents should use the topic-link workflow against the backend that actually owns the selected session. A metadata-only topic record is not an active binding. This backend does not require a renderer plugin; binding and pause controls are exposed through the authenticated API.

## Delivery and safety

This version automatically submits pending, addressed TASK/REQUEST and scoped REPLY/INFO/ERROR continuations with an exact top-level `body.topic` binding, no approval requirement and bounded body size. Informational messages are data, not an instruction to send a receipt or start unrelated work.

The listener subscribes before one zero-timeout `since_now` head snapshot, so first install does not execute retained backlog. It reads bounded cursor pages only on native notifications and connection/reconnection. Private SQLite at `~/.hermes/state/agent_bus_wake.sqlite3` stores bindings, cursor and minimal message references, never bodies or credentials.

A notification coalesces bounded batches, claims/re-reads recipient-authorized messages, resumes only the exact selected stored conversation through the normal gateway, and submits a queued prompt after resume succeeds. Peer payloads are data, not new owner authorization. No renderer route is synthesized and no independent agent process is spawned.

`submitted` means queued/streaming acceptance, not task completion. Ambiguous delivery becomes `uncertain`, blocks later batches for that binding, and is not blindly replayed. Inspect the actual conversation before manual reconciliation. This is not exactly-once task execution.

Binding attachment reflects the real recipient inbox subscription, not other agents' per-topic presence. That requires separate server-side topic presence and client registration.

## Verification

Run `uv run --locked pytest tests/test_desktop_mcp_wake.py tests/test_desktop_mcp_wake_plugin_api.py tests/test_desktop_wake_backend.py -q`. Offline tests use fake MCP/ASGI/RPC and temporary stores, not models or the live bus. They cover exact resume/queued submission, duplicate events, drain failure, persistence, and router-lifespan restoration. Live acceptance separately requires a new peer message, native invalidation, correct conversation submission and actual continuation. Actual backend restart and future core upgrade are separate gates, not established by offline lifespan tests alone.
