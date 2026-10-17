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
Its seven `hub_*` tools expose task/review/decision-request/read operations, not
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

After restore, leave dispatch disabled until executor command state, target
identity, current immutable artifact and fence generation are reconciled. Do not
replay unknown commands blindly. A container restart test is not proof of TrueNAS
reboot, ZFS restore, credentials, network partitions or desktop-session recovery.

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

The staging PC runs software tests and tiny fixtures, never full LiDAR, OpenSplat
or photogrammetry workloads. Processing reservations/adapters remain later work.
