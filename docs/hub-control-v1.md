# Engineering Hub control-plane build

> **Current deployment and evidence:** use the
> [current unified Hub deployment guide](simple-hub-deployment.md), not the
> original separate-service instructions below. The approved layout uses agent
> port 8770, browser port 8771, no 8765 alias, and only the
> `/mnt/Plugins/App_Data/Engineering-Hub` host storage root. Human identity uses
> separate Hub Cloudflare Access, not Ops or the original GitHub OAuth plan.
> Source support and published images are not proof of live configuration.
> The [2026-10-21 bounded TEST acceptance](staging-pilot-acceptance-2026-10-21.md)
> records the real queued failure/rollback and shutdown checks, plus separately
> qualified live browser observations. It does not establish complete TrueNAS,
> identity/session, reboot or production acceptance. Historical design and
> checkpoint evidence below is preserved.

## Historical initial deployment design

This is an additive, independently deployed service. It does **not** replace or
change the current message bus, its port 8765, image name, tokens, storage or
native push listener. Default control service port is 8770 on loopback only.

## Goal addendum: topic participant readiness

Beau explicitly requires agents to know whether their peers are connected to the
same session topic, so unanswered sends do not masquerade as active discussions.
This is an acceptance requirement for the active Engineering Hub build, not a
claim that the live integration already provides it.

- Expose the same topic-scoped participant readiness to agents through MCP and
  to humans through the dashboard. Distinguish invited, linked, reachable,
  stale/unknown and explicitly disconnected; topic membership alone is not proof
  of an active listener or a reachable chat.
- Notify linked participants of meaningful readiness changes using the existing
  event-delivery path, with bounded deduplication rather than another AI polling
  job. Include freshness and session binding without exposing credentials.
- Before a coordinated exchange, report unavailable or unconfirmed peers. A
  durable queued message may wait for reconnection, but must not be represented
  as delivered, processing or completed merely because the bus accepted it.
- Verify offline, restart/reconnect, stale presence and wrong-topic cases, and
  ensure agents do not generate empty or receipt-only reply loops. Readiness is
  informational; it does not authorize work or override ownership boundaries.

## Implemented contract

### Hub connections versus topic readiness

In unified mode, `/api/hub/v1/overview`, `/api/hub/v1/agents` and MCP
`hub_overview` expose a redacted snapshot of the embedded bus's active streams
and inbox subscriptions. This is a connection to this server process only:
it does not prove a running model, correct-topic session, wake delivery or task
completion. No observed stream does not mean the agent is offline. Standalone
Hub instances without the embedded bus leave this observation absent/unknown.

Connection snapshots include `observed_at` and a maximum 30-second `valid_until`.
Positive transport evidence expires at most 45 seconds after a stream opens or
its last successful server write, using a monotonic clock. The first write has
the same bounded startup grace. Snapshot refreshes cannot extend that deadline;
`valid_until` can be shorter than 30 seconds. Retained streams without recent
activity contribute to `stale_streams`, not `active_streams` or subscriptions,
and the connection state is `stale` when no fresh stream remains. Blocked body
sends on authenticated persistent MCP GET streams time out after 15 seconds.
These are server-side observations: an intermediary may still accept writes
after its downstream client disappeared. Neither stream freshness nor a stored
subscription proves client receipt, agent readiness, or correct-session wake.
The dashboard expires them locally; it does not poll or start a listener.
Use Refresh for a new snapshot. Malformed or failed observations are unavailable,
not connected. Topic tracking is independently labelled `not_configured` only
when no topic observer is configured; a configured observer failure remains
unavailable/unverified. A bus connection never upgrades topic readiness.

After explicit owner enrollment, the ordinary unowned factory deliberately
cannot reopen the ledger. See [owned, inactive Hub](owned-inactive-hub.md) for
the explicit continuation entrypoint that serves normal records and the bus
without selecting an executor. It does not provision private storage, enroll
state, enable staging, or provide reboot/restore acceptance.

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
Fence generations are strict positive integers bounded to SQLite's signed 64-bit
maximum, `9223372036854775807`; larger values are rejected before HTTP submission.
Timeouts, malformed responses and uncertain outcomes do not retry. A durable
advance lock prevents competing controllers. A hard crash can leave that lock
reserved; there is deliberately no automatic expiry or takeover. Inspect the
original command read-only and reconcile with actual effect evidence before
operator recovery. These restrictions are safer than silently replaying a
possibly completed deployment, but need a recovery UI before live activation.

`HubOutboxPublisher.drain_batch` is bounded and explicitly invoked. It sends only
event pointers, persists prepared payloads and stable bus idempotency keys before
I/O, and records **bus acceptance** only after a valid message ID. Its result uses
`accepted`, not `delivered`: this proves neither chat delivery nor task completion.
The correlation root must agree with the approved topic. Conflicting persisted
legacy envelopes remain intact and held with `routing_conflict`, never silently
rewritten under a possibly accepted idempotency key. Unconfirmed sends report
`bus_acceptance_unconfirmed` and preserve the original key/payload for reconciliation.
Each explicit batch makes one bounded authenticated `bus_info` identity check,
then atomically pins that server-proven principal to this Hub database before
preparing or sending pointers. The injected client's authenticated session must
stay immutable throughout the batch. Changing principals is held with
`sender_identity_conflict`; missing identity or legacy prepared receipts without
sender provenance are held, never adopted under today's token. Same-principal
token rotation retains the original sender-scoped idempotency protection.
Its current linked-discussion routing is for the
isolated build test; no publishing worker is activated by importing it.

## Opt-in autonomous staging

The following describes the original dispatcher source and its fixture-only
checkpoint. Its pending-pilot statements are historical; the later native queued
TEST workflow has separate actual evidence in the acceptance record linked above.
Neither source path is enabled by ordinary Hub startup.

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

## Historical standalone private configuration

This section records the original standalone profile and deferred OAuth setup.
It is not the current installation path; use the unified deployment guide linked
above for persisted settings, agent tokens and Cloudflare Access verification.

The opt-in, standalone `compose.hub-control.yaml` profile and
[`hub-control-deployment.md`](hub-control-deployment.md) provide the private
operator deployment path. They have not been activated and do not modify the bus.

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

### Dashboard source and isolated image checks

`hub-ui/` contains the React dashboard. From that directory, use `npm ci`,
`npm test`, `npm run build`, and `npm run test:e2e` (Playwright needs an installed
browser; Windows tests use Edge). `npm run dev` binds only to loopback and proxies
the Hub API to loopback port 8770. The dev server is not a production deployment.
The explicit `?demo=1` workspace uses synthetic, browser-memory fixtures and makes
no API calls. The normal URL always uses the real cookie-authenticated API.

The independent `docker/Dockerfile.hub-control` builds and bundles the dashboard;
the live message-bus image and its port remain unchanged. It serves the UI and API
from one origin. The image runs as UID/GID 10001 with `HUB_DB` defaulting to
`/var/lib/hub/hub.sqlite3`. A real deployment must explicitly provide protected,
durable local storage there; the image filesystem itself is not a backup.

After building a local image, run
`python scripts/hub_dashboard_container_smoke.py --image sha256:<immutable-local-image-id>`.
This creates and removes only its uniquely named disposable container: no network,
host ports, host mounts, production tokens or OAuth credentials. It verifies static
assets and their content types, security headers, UID/GID, auth-disabled startup,
denial of private API reads, and actual MCP initialization/discovery for three
synthetic identities. Pulling is disabled. Cleanup reconciles the uniquely labeled
container even if the startup response was interrupted; it removes only the
verified owned container ID and reports ambiguous cleanup instead of guessing.
Its disposable tmpfs deliberately does **not** establish storage/reboot acceptance.

Browser connection indicators expire locally without HTTP status polling. Native
SSE invalidations are coalesced; disconnects invalidate readiness and block writes
until reconciled. Topic readiness remains advisory, separate from durable mail,
claiming work, executing work and completing it.

Local verification on 2026-10-17: production Vite/TypeScript build, eight API-client
unit tests, twenty real-Edge Playwright tests, and the network-disabled container
smoke passed. The Hub Python suite passed 410 tests with two Windows-specific
skips. Browser tests mock private API data and exercise real native browser fetch,
Origin/cookies/CSRF, stale-response handling, distinct peer expiry deadlines,
topic switching, invalidation coalescing, conflict recovery and session expiry.
They do not establish a real GitHub OAuth callback or live registry attachment.
Public REST responses now use strict DTOs in the published OpenAPI contract.
`python scripts/check_hub_ui_contract.py` validates actual TypeScript overview and
topic fixtures against the Python response models without a server or credentials.
The standalone deployment profile also passed five structural tests and actual
Docker Compose configuration validation with fixture-only variable values. No
service was created or started by that configuration check. The isolated Linux
suite passed 672 tests without resource warnings; see the qualified snapshot and
test-only loop ownership fixes in `hub-linux-validation-2026-10-17.md`. Final
executor/container integration remains a separate gate.

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
An opt-in, request-scoped canonical registry bridge is implemented in source.
It forwards each authenticated agent's original bearer, never an impersonation
header or shared service credential. Human access requires a separately supplied
fixed-topic read-only resolver and remains unavailable by default. No credentials,
owner selections or trusted runtime attestations are provisioned by this bridge.

`GET /api/hub/v1/topics/{topic}/events` is a separate authenticated SSE channel
for canonical registry invalidations. Its epoch and cursor never share the Hub
outbox namespace. It accepts paired `after`/`epoch` query parameters, rejects
malformed local cursors before upstream I/O, and performs an upstream ACL check
before streaming. The upstream transport is bounded and closes on malformed
frames, source loss or required snapshot recovery; it does not reconnect, poll
or renew evidence on keepalive comments. Reads downgrade expired/future evidence
and never infer absent membership from an incomplete inventory.

This bridge is constructor-injected only: the default server does not activate
it from environment variables. Synthetic HTTP/MCP/stream tests establish its
request-scoping and failure contracts, not live registry/provider integration.
Actual source deployment, scoped reader enrollment, exact-session attestations
and three-agent reconnect acceptance remain required before claiming end-to-end
topic readiness. The Hub outbox stream alone cannot prove topic presence.

## Historical release acceptance checklist

This checklist and the implementation-status paragraph below record the earlier
checkpoint, not current remaining-work claims. The OAuth choice was superseded
by separate Hub Cloudflare Access, and the bounded real TEST pilot has since
passed as documented in the linked acceptance record. That record does not close
the separate live deployment, browser, reboot or production acceptance gates.

1. Fixture/controller source covers typed allowlisted targets/actions/artifacts,
   durable command dedupe/fencing, health, rollback, pause and restart
   reconciliation. Frozen Linux tests verify those paths with simulated host
   responses. A real host adapter and autonomous application pilot are still
   required; source tests alone do not close this gate.
2. Dashboard source has independent review and browser regression evidence.
   Real private GitHub OAuth sign-in and CSRF acceptance still need the dedicated
   OAuth application Beau deferred. No demo or synthetic session substitutes.
3. Inventory all existing deployment writers/timers; prove exclusive control of
   one disposable staging pilot before enabling any project target.
4. TrueNAS storage/UID/mount/backup verification and actual restart acceptance.
5. Explicit production/action-bound approvals and GitHub automation gates before
   enabling those capabilities. Existing shared write credentials mean Hub-only
   review gates are not bypass-proof repository protections.
6. Topic-level peer reachability and visible queued/unroutable delivery status as
   specified in the active goal addendum above.

Current implementation is not a completed first usable release. Shared task and
decision persistence, independent review, bounded controller recovery, native
Hub events and the private dashboard are source-verified. The default deployed
factory intentionally lacks a host executor, canonical registry and human OAuth
configuration. Separate operator-injected lifecycle/registry modules do not
activate merely because their code is in the image. Remaining release work must
connect those approved providers and verify actual running behavior, not replace
missing acceptance with another round of fixture-only assertions. See
`hub-linux-validation-2026-10-17.md` for precise evidence scopes and
`hub-review-followups.md` for named peer findings and outstanding integration.

The staging PC runs software tests and tiny fixtures, never full LiDAR, OpenSplat
or photogrammetry workloads. Processing reservations/adapters remain later work.
