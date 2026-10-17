# Engineering Hub control-plane build

This is an additive, independently deployed service. It does **not** replace or
change the current message bus, its port 8765, image name, tokens, storage or
native push listener. Default control service port is 8770 on loopback only.

## Implemented contract

The REST contract is `contracts/hub-v1.openapi.json`; the running service exposes
`/openapi.json`. `/api/hub/v1/overview` supplies tasks, decisions, runs, configured
projects and explicit capability flags. Unknown host/agent state is not online.

Humans assign tasks. A distinct existing agent identity claims each task, records
an exact 40-character commit and obtains a different agent's review of that exact
commit. All changes use expected revisions. Replacing a candidate invalidates
review, without resetting the three-failure repair budget. SQLite transactions
persist each domain change, audit event and outbox event together.

Decision requests are not execution approvals. Agents may request decisions but
only the configured human can answer; even answered protected decisions have
`execution_authorization=false`. Protected action grants, production deployment,
GitHub merging and real host dispatch are not enabled by this scaffold.

The separate control service MCP endpoint is `/mcp/`. It reuses the existing
Hermes/Codex/Claude bearer identities and authenticates discovery and every call.
Its nine `hub_*` tools expose task/review/decision-request/read operations, not
human assignment, decision answers, pause changes or deployment. This is not a
change to the existing production bus's MCP tool list.

## Fixture integration modules

`FixtureWorkflow` is an explicitly invoked fixture-only coordinator, not an
enabled API endpoint or unattended worker. Operator-supplied manifests pin both
candidate and rollback digest/provenance before any effect. It deploys, smokes,
then records success, or records failure and attempts the configured rollback.
Failed targets remain reserved until confirmed rollback; each new run receives
a durable increasing per-target fence. No shell, Docker socket, download URL or
live application driver is accepted.

The executor client sends one typed command and verifies every echoed field.
Timeouts, malformed responses and uncertain outcomes do not retry. A durable
advance lock prevents competing controllers. A hard crash can leave that lock
reserved; there is deliberately no automatic expiry or takeover. Inspect the
original command read-only and reconcile with actual effect evidence before
operator recovery. These restrictions are safer than silently replaying a
possibly completed deployment, but need a recovery UI before live activation.

`HubOutboxPublisher.drain_batch` is bounded and explicitly invoked. It sends only
event pointers, persists prepared payloads and stable bus idempotency keys before
I/O, and records delivery only after a valid message ID. It does not equate bus
delivery with task completion. Its current linked-discussion routing is for the
isolated build test; no publishing worker is activated by importing it.

## Opt-in autonomous staging

`StagingDispatcher` selects durable `task.reviewed` events only when the task's
current revision, independent review, exact candidate and operator-owned fixture
manifest agree. Each event has a durable reservation and stable run request key;
run creation, its acceptance receipt, target fence and audit/outbox writes commit
together. Exact retries return the same run rather than allocating another one.
Unknown effects stay held. Known target-busy or pause rejection may be deferred
and retried on a later durable event, without inventing another run identity.
Changed candidates/manifests cannot reuse an old review. Three failed attempts
remain the task's lifetime limit.

An explicitly supplied `staging_dispatcher` dependency enables `StagingRuntime`
inside the control process. It consumes the **existing** native Hub feed, catches
up bounded outbox history, and otherwise waits without DB/model/status polling.
No second native watcher is created. It checks observer health at each safe
boundary and waits for an active bounded workflow on orderly shutdown, instead
of cancelling a deployment mid-effect. Actual process crashes still require
inspection before taking over uncertain work; a timeout is not quiescence proof.

Normal environment startup has no dispatcher or host manifests and remains
disabled. Agents cannot enable it through a tool or API. Authenticated
`GET /api/hub/v1/autonomy` exposes the actual runtime state and redacted durable
receipts. The current configured driver is fixture-only; no source test or
MockTransport result establishes a real application rollout or staging target
activation. Host inventory and real HTTP/container acceptance remain gates.

## Private configuration

Use the existing approved secret/environment mechanism, not command arguments,
repository files, chat, or browser storage. Required variables:

- `HERMES_TOKEN`, `CODEX_TOKEN`, optional existing `CLAUDE_TOKEN` (same bus values).
- `HUB_DB`: dedicated local durable path, separate from `AGENT_BUS_DB`.
- `HUB_BASE_URL`: exact private HTTPS origin; HTTP permitted only on loopback.
- `HUB_GITHUB_CLIENT_ID`, `HUB_GITHUB_CLIENT_SECRET`: human OAuth application.
- `HUB_BEAU_GITHUB_ID`: explicitly configured positive numeric GitHub account ID.
- Optional `HUB_UI_DIRECTORY`: built dashboard directory.

Start with `python -m engineering_hub`. Bind a non-loopback address only as part
of a reviewed private deployment. Terminate HTTPS at the approved private ingress.
Do not expose it publicly or put it behind the public operations application.
Configure GitHub OAuth callback as `<HUB_BASE_URL>/api/hub/v1/auth/callback`.
Do not log callback query strings at any proxy. The supplied entry point disables
Uvicorn access logs; alternative launchers must do the same.

OAuth is deny-by-default when unconfigured. No first-user enrollment, dev login
route, browser bearer-token entry or agent-to-human fallback exists. The backend
checks the numeric allowlist, one-use browser-bound state, PKCE, HttpOnly session
cookie, exact Origin and session CSRF token for human mutations. The human OAuth
identity is separate from a future GitHub automation credential.

Beau chose to create a dedicated OAuth application later. Until it is securely
configured, live human sign-in stays disabled; mock UI and internal test sessions
must be labelled as fixtures, not represented as usable production login.

## Storage and recovery

Keep the control DB on local storage on its host, not an SMB SQLite WAL path.
Use SQLite's backup API for a coherent backup while running, or stop all writers
before copying the database. Copying only the main database while WAL is active
is not a backup. Protect the DB and backup: they contain session verifiers and
private task/decision records. Retain NAS dataset backups independently.

`python -m engineering_hub.backup SOURCE DESTINATION` creates a new coherent
backup, verifies SQLite integrity and prints its SHA-256 and size, not its
contents. It refuses to overwrite existing files. Choose a restricted destination
directory: POSIX files are owner-only; Windows files inherit directory ACLs.
The destination parent must be private and trusted: SQLite reopens the exclusive
new file by name, so this utility does not defend against a hostile process that
can replace files or directories there while the backup is running.

After restore, leave dispatch disabled until executor command state, target
identity, current immutable artifact and fence generation are reconciled. Do not
replay unknown commands blindly. A container restart test is not proof of TrueNAS
reboot, ZFS restore, credentials, network partitions or desktop-session recovery.

## Active goal addendum: topic-level peer reachability

Beau explicitly added this requirement to the build goal: agents must be able to
tell whether another agent is connected to the **same session topic**, so a send
does not silently disappear into a topic without a reachable recipient. This is
required scope, not a claim that the feature is already implemented.

Expose the same authenticated per-topic reachability evidence to the dashboard
and agent API/MCP. Distinguish unlinked, linked but listener unverified,
reachable, busy/queued, offline and unknown/stale. A generic authenticated MCP
connection, stream/subscription count or healthy service is not proof that the
intended chat can accept a message. Bind readiness to the exact topic and current
session/connector generation, and invalidate stale evidence on disconnect or
restart. Keep private session identifiers and transport credentials out of peer
status responses.

Before a topic-directed request, show the sender the recipient's route/readiness
status. Report durable acceptance, awaiting connection, delivery to the selected
session, processing and completion separately. Presence can change after any
check: preflight status is advisory, never a delivery guarantee. Preserve offline
messages; do not discard them, silently reroute to an unrelated default chat,
launch replacement agents or retry with fresh message IDs. Unroutable or stale
bindings must yield visible, recoverable status. Notify interested participants
of meaningful reachability changes through the existing event-driven mechanism,
without polling/model checks or acknowledgement-only message loops.

Acceptance must cover a peer online on a different topic, missing bindings,
disconnects, stale generations after restart, busy sessions with queued work,
offline durable delivery after reconnection, and a disconnect between preflight
and send. Test reachability independently from message receipt and task completion.

The read-only Hub projection is implemented at
`GET /api/hub/v1/topics/{topic}/peers` and
`GET /api/hub/v1/topics/{topic}/preflight?recipient=hermes` (or codex/claude), with
matching `hub_topic_peers` and `hub_topic_preflight` MCP tools. Topic roots are
bounded stable identifiers, not display names or slash-separated subthreads.
Responses carry `advisory=true` and `delivery_guaranteed=false`; public peer
records separate link, transport, availability and evidence levels. Verified
ready/queued records have `valid_until`, so a client can locally expire cached
readiness without polling. Reads use the control server's current clock, not a
cached observer clock. Missing authoritative registry integration returns
`unknown_stale`, never fabricated online status or asserted absence.

No writable readiness or competing binding registry exists in the Hub. Hermes
owns the canonical bus-side binding/evidence registry slice; its owner-selection
and trusted bridge mechanisms are not yet integrated or live. Ordinary agent
tokens do not mint human selection or trusted runtime attestations. Actual
Codex/Claude/Hermes conversation and reconnect acceptance remains required.

`GET /api/hub/v1/events` is an authenticated SSE invalidation feed for **Hub
outbox changes**. It subscribes before the first snapshot, uses native Linux
SQLite commit notifications, bounds slow-subscriber queues, and emits only
`observer_epoch`, `cursor` and `kind`. Clients re-read current data on an
invalidation or reconnect; this is not a historical replay stream. Epoch changes
invalidate caches, and source loss or overflow closes the stream with an explicit
unavailable pointer. Human session expiry/revocation is checked before every
event. `/api/hub/v1/events/status` reports source availability. Unsupported native
platforms return 503 for streaming, without silently enabling status polling.
The canonical topic-registry invalidation bridge is still pending: the Hub
outbox cursor is not a bus registry cursor, and this local stream alone does not
prove topic presence updates are wired end to end.

## Acceptance gates still required

1. Isolated fixture executor: typed allowlisted targets/actions/artifacts, durable
   command dedupe and fencing, health, rollback, pause and restart reconciliation.
2. Independently reviewed dashboard; real private OAuth sign-in and CSRF flow.
3. Inventory all existing deployment writers/timers; prove exclusive control of
   one disposable staging pilot before enabling any project target.
4. TrueNAS storage/UID/mount/backup verification and actual restart acceptance.
5. Explicit production/action-bound approvals and GitHub automation gates before
   enabling those capabilities. Existing shared write credentials mean Hub-only
   review gates are not bypass-proof repository protections.
6. Topic-level peer reachability and visible queued/unroutable delivery status as
   specified in the active goal addendum above.

The staging PC runs software tests and tiny fixtures, never full LiDAR, OpenSplat
or photogrammetry workloads. Processing reservations/adapters remain later work.
