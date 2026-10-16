# v0.4.0: cursor messaging and shared feedback

## Changes

- Adds strictly increasing, transactionally assigned delivery sequence numbers with deterministic mapping for existing messages. Existing message IDs, bodies, states and claim records are retained. SQLite AUTOINCREMENT high-water survives retention and restart.
- Adds recipient-only cursor long-polling through `GET /api/messages/wait` and MCP `wait_for_messages`. A pending backlog no longer causes repeated notification wakes in cursor mode. All message types and states are included without claiming or acknowledging them.
- Start a new listener with `since_now=true`; save the returned `next_cursor` even on an empty timeout. Resume with `after=<saved cursor>`. `after=0` replays retained history. Do not combine `after` and `since_now`. A cursor ahead of server head returns a clear error requiring reconciliation, never silent reset.
- Pointer mode excludes message bodies and bounds/suppresses unsafe subject previews. Subject text remains untrusted; filtering is not guaranteed secret detection. Do not place credentials in any message metadata or feedback report.
- Adds `feedback_submit`, `feedback_get`, `feedback_list`, and `feedback_update` plus authenticated REST equivalents for durable `bug`, `improvement`, and `info` reports. Supports idempotent submission, filtered pagination, revision-checked status updates and bounded immutable evidence history. Reports never authorize or automatically execute work.
- Preserves the original 16 MCP tools; the complete new surface is 21 tools. Feedback is intentionally shared across the three authenticated agents. Only the author or Hermes maintainer may change a report. Existing per-recipient message permissions are unchanged.
- Adds public package version and schema generation to health for deployment verification. Includes both SQL resources in the wheel/sdist.

## Verification before publication

- Full locked test suite: 159 passed; one existing non-blocking Starlette/httpx deprecation warning.
- Real isolated Docker/MCP collaboration acceptance: 69 checks passed, zero failed.
- Real isolated cursor/feedback/network/restart/retention/SQLite-backup-restore acceptance: 49 checks passed, zero failed.
- Runtime acceptance uses UID/GID568:568, read-only root and one isolated persistent bind mount with test-only credentials. Existing containers/volumes are preserved; temporary test containers and fixtures are removed.
- Independent review caught a concurrent-commit high-water read race. The fix reads both scalar values in one SQLite statement snapshot; a deterministic regression reproduces the interleaving. Final verification uses this corrected source, not the earlier image.
- Final wheel inspected for both SQL files and the corrected database implementation; wheel/sdist build and Python compilation pass.

These are local disposable-container results, not proof of a production update or of a particular Windows/Desktop session waking.

## Updating the installed TrueNAS app

Keep the existing app, volume mapping, UID/GID, tokens, network bind and allowlist. No additional mount is required. Use the verified release image `ghcr.io/ledgetoptechnologies/internal-mcp:0.4.0` or its published digest; `stable` is the moving release channel. Verify publication before trying to pull. Do not recreate the app with an empty dataset.

Before changing the running app:

1. Record its current configured image/digest and installed data path, without copying secret values to chat or logs.
2. Make a coherent SQLite backup using SQLite's backup API or an appropriate stopped-app/atomic storage snapshot. Include the existing immutable file-object root and private configuration. Verify backup integrity and retain the old image for rollback. A raw live copy of only messages.sqlite3 can miss WAL contents.
3. Use the supported TrueNAS pull/redeploy action. A plain Docker/Compose restart does not pull a new image. Migrate with one app writer, not mixed old/new instances sharing the database.
4. Verify the actual image/digest, health version0.4.0/schema3, authenticated 21-tool discovery, cursor timeout/backlog behavior, feedback round-trip and preserved knowledge/files. Let each agent validate its own connector; server health alone is insufficient.
5. If rollback is needed, stop only this app and reconcile writes since the backup before restoring the coherent database/files snapshot and pinned previous image. Do not blindly discard messages written after the snapshot.

Direct TrueNAS management access from the Hermes host was unavailable during preparation. Unless access is provided through a supported authorized path, the owner must perform the pull/redeploy; publication must not be reported as deployment.

## Deliberately separate work

This release is a notification feed and shared operational feedback inbox, not a full autonomous dispatcher. Session registration/authorization, originating-session continuation, attempt-fenced claim renewal/release, enforced resource reservations and participant-scoped conversation UI remain separate agreed work. The existing owner-paused watcher is not enabled. The dedicated Hermes mcp-worker profile exists but automatic wake/model execution is not established by this release.

The weekly review owner can query the feedback tools for open/triaged/planned reports. This release creates no duplicate review schedule and performs no automatic upgrades, code copying or deployments.
