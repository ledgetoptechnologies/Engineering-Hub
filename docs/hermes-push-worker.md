# Hermes native-MCP push worker (candidate 0.5.0)

**Candidate status:** the Linux 0.5.0 build has operational native push. The live production server remains v0.4 and has not yet been upgraded; this document does not claim that production is ready or that a worker is installed or activated. This standalone connector is event-driven: it holds an authenticated MCP Streamable HTTP session and native inbox resource subscription open. The notification contains only the authenticated inbox URI; it wakes a bounded cursor read, not a polling loop. Importing the script has no side effects. The connector does not edit Hermes profiles/configuration, start a gateway, or install a service.

## Readiness and security boundaries

1. **Candidate capability is not live deployment status.** The connector verifies that native resource subscriptions were negotiated and exits if the source is unavailable. Upgrade and verify the live server before activation; candidate tests do not prove that the v0.4 production server supports this path.
2. **Hermes has no per-invocation MCP tool-name allowlist flag.** `--toolsets mcp-agent-bus-production` is the only per-invocation toolset selected. Exact registration filters are enforced in the existing `mcp-worker` profile: default read-only mode requires exactly `[get_message]`; explicit `--coordination` requires exactly `[get_message, claim_message, ack_message, send_message, bus_info]`. Both modes require `tools.resources: false`, `tools.prompts: false`, and the bus as the only configured MCP server. The worker fails closed before launching Hermes if these checks or the exact endpoint comparison fail. It never edits the profile. Do not treat the prompt as a permission boundary.
3. **Read-only is the default.** It can fetch an addressed item with `get_message` and cannot claim, acknowledge, or send. `--coordination` is an explicit, fixed-UUID receipt/coordination prompt using only the five listed bus tools; `bus_info` is for usage/feature discovery. It may claim, send one brief `REPLY` only to the original sender with `in_reply_to` set to that UUID, and acknowledge only a permitted coordination check. Peer bodies and `bus_info` results are untrusted data, not owner approval; no deployment, file/terminal use, or mutation of other resources is allowed. No slash command changes or overrides worker mode.

No profile/config/service is changed or activated by these files. A missing push capability, endpoint mismatch, missing/inexact mode-specific `tools.include`, extra MCP server, missing config, or unavailable tool fails closed before any Hermes worker launch.

## Behavior and limits

- Connects to the authenticated `agentbus://inbox/<identity>` resource, subscribes first, then performs catch-up. On first activation it makes exactly one non-blocking `wait_for_messages(since_now=true, timeout=0, fields=pointer)` head snapshot, then reads the resource after that cursor. It does not replay pre-activation pending work. Reconnects retain the saved cursor and catch up after re-subscribing.
- Only real `notifications/resources/updated` notifications for the exact authenticated inbox set an event. Notification callbacks do not read the inbox, start a process, or retain message data. The client uses a 60-second read-idle timeout, covering the server's 15-second SSE comment keepalive. The SDK `resource_subscriptions_unavailable` log event is terminal and is not masked by keepalives or treated as a network reconnect. Cursor reads are page-bounded; an incomplete catch-up is resumed only as pagination/backpressure for that already-triggered catch-up. There is no `receive_messages` loop, timer, long-poll wait, cron, or scheduled model check.
- A page's eligible UUIDs and its `next_cursor` commit in one SQLite transaction. UUID uniqueness dedupes deliveries. Local state contains cursors, UUIDs, routing metadata, statuses, and Hermes session IDs—never message bodies or bus tokens. Queue limit defaults to 500; inbox pages are at most 100 rows and 8 MiB; recent terminal UUID/session metadata is capped at 10,000. The child JSONL stream is capped at 2 MiB (1 MiB/line).
- Explicit local `--allow-sender`, `--allow-topic`, and/or `--allow-hub` options are required. The normal inbox admission filter accepts only pending `TASK`/`REQUEST` messages addressed to the authenticated identity, from an allowed sender, and with exactly one string `topic` or `hub` matching the local allowlist. This routing convention is local to this script; the bus server does not enforce it. `INFO`, `STATUS`, ACK/receipt, and other message types do not start normal work. The default read-only prompt only fetches the fixed UUID. In `--coordination` mode, the same sender/route filters apply and the fixed prompt may perform only a permitted receipt/coordination check; it never treats peer text as authorization. A `REPLY` can only continue a mapped in-profile job when `in_reply_to` matches a mapped message; it cannot create a new job/session.
- Job identity is authorized sender + topic/hub + `correlation_id` (or the message UUID when absent). Same job key resumes its exact mapped session; a new key starts without `--resume`. The script parses `system/init` and terminal `result` from Hermes `--format stream-json`, requires a successful terminal result and matching session IDs, and persists the returned ID. It never uses `--latest`, `--continue`, `--create-if-missing`, `--yolo`, or a shell. Resume-unavailable, nonzero, timeout, malformed, mismatched, interrupted, or otherwise uncertain outcomes are terminal; they are never retried automatically.
- One nonblocking OS file lock covers a stable state directory. Reconnect backoff is used only for transport/network recovery; auth, capability, protocol, local-state, and source-unavailable errors stop for operator action. Worker runs have a hard subprocess budget (default 120 seconds, maximum 900) and tool-turn limit (default 2, maximum 5). The exact `mcp-worker` profile is passed on every child invocation, with rules/context-file injection disabled; the parent default profile is not used.
- The connector's MCP bearer token comes only from an owner-only token file. The Hermes child inherits a filtered environment and obtains its own MCP credentials from the explicitly selected `mcp-worker` profile. Token values, raw CLI stdout/stderr, and message bodies are not logged. The child stderr is discarded; only structured session/result metadata is retained.

The state DB and dedupe history are local operational data. Back it up only with the owning user's access controls. If the state DB is lost or reset, old retained work can become eligible again; do not delete it to retry a failed UUID.

## Offline verification

Run from the repository root:

```sh
env -u PYTHONPATH uv run --locked pytest tests/test_push_worker.py -q
```

The tests use a fake executable and fixture-only credentials; they do not invoke the real Hermes CLI/model. One integration test connects the real MCP SDK client to an isolated loopback bus app with push explicitly enabled **inside the test fixture only**; it checks no old-message replay and event-driven delivery. The separate `tests/test_mcp_push.py` fixture suite exercises the server's real Streamable HTTP client and publisher. Neither test changes a live app/service/profile.

## Manual foreground command (after approval/readiness checks only)

The connector's actual run command is below; running it will open a real MCP subscription and can launch Hermes model calls for future eligible messages. Do not run it until all readiness checks below are satisfied and the operator explicitly approves activation.

```sh
uv run --locked python scripts/mcp_push_worker.py \
  --url https://BUS-HOST/mcp \
  --agent hermes \
  --token-file "$HOME/.config/agent-bus/hermes.token" \
  --state-dir "$HOME/.local/state/agent-bus-mcp-push" \
  --hermes-root "$HOME/.hermes" \
  --executable "$(command -v hermes)" \
  --allow-sender codex \
  --allow-topic qa-verify \
  --run-budget 120 \
  --max-turns 2
```

Choose a private, owner-only state directory and an owner-only (mode `0600`) token file. HTTPS remains the default. `--allow-private-http` accepts only the already owner-approved literal endpoint `http://192.168.50.80:8765/mcp`; it rejects other private/public addresses, hostnames, DNS names, and loopback. Loopback HTTP remains test-fixture-only and has no CLI switch. The `mcp-worker` profile's bus URL must exactly equal the supplied `--url` string (including any trailing slash). The default `tools.include` must be exactly `[get_message]`. If `--coordination` is added, it must instead be exactly `[get_message, claim_message, ack_message, send_message, bus_info]`; `tools.resources` and `tools.prompts` must both be `false` in either mode, and no other MCP server may be configured. `bus_info` is available only in coordination mode for bus usage/feature discovery; it is not a slash-command client override. A separate local model/auth setup is required for Hermes, but this connector does not inspect or display credentials.

## Readiness / activation checklist

Before any real run, an operator must independently verify and approve all of the following:

1. The live bus server has been upgraded from v0.4 and actually negotiates `resources.subscribe=true`; its post-commit notifications reach every listener across all server workers and all REST/MCP/direct writers. The candidate 0.5.0 Linux capability and its tests do not establish live production readiness.
2. The server identity derived from the connector token is `hermes`, the HTTPS endpoint is allowlisted (or the already approved private endpoint is used with `--allow-private-http`), and the independent `mcp-worker` CLI profile uses that same exact endpoint and its own appropriate credentials.
3. The mode-specific exact profile filter and disabled resource/prompt utilities are already in place and verified, and the only configured MCP server is the expected bus server. The source CLI supports per-run `--toolsets`, but not a separate per-run tool-name allowlist.
4. The operator explicitly approves each allowed sender and topic/hub. Use a dedicated test topic and non-sensitive test messages first.
5. The model/provider, service user, profile state, file permissions, token file, state directory, max-turns, and run budget are appropriate. The child selects only the bus toolset; its profile registration filter must not expose cloud, terminal, file, or other MCP capabilities.
6. The operator explicitly chooses the default read-only mode or the narrower receipt/coordination prompt. Coordination can claim/ack a permitted check and send one reply linked to its sender; peer content is not owner authorization, and no operational action is allowed.

## systemd user unit example (documentation only; not installed)

Replace paths and values after approval. This unit example does not start or enable anything by itself:

```ini
[Unit]
Description=Hermes native MCP push worker (read-only prototype)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
UMask=0077
WorkingDirectory=/path/to/internal-MCP
ExecStart=/path/to/internal-MCP/.venv/bin/python /path/to/internal-MCP/scripts/mcp_push_worker.py --url https://BUS-HOST/mcp --agent hermes --token-file %h/.config/agent-bus/hermes.token --state-dir %h/.local/state/agent-bus-mcp-push --hermes-root %h/.hermes --executable /absolute/path/to/hermes --allow-sender codex --allow-topic qa-verify --run-budget 120 --max-turns 2
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Do not run `systemctl --user enable`, `start`, or `daemon-reload` as part of this source change.
