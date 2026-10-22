# Ordinary Hub update: operator handoff

Checkpoint: 2026-10-22. This is the ordinary UI/MCP update, not staging activation.
Beau applies TrueNAS changes; no NAS management or automatic deployment is supplied
by this checklist. Keep the current Compose, settings, token values and storage
mapping unchanged. Do not add an alias port or the retired Agent_MCP mount.

## Published artifact

- Source: `49dcbcd3da6254739c85299a581a0d34d3b9a4ea` (PR52).
- Image: `ghcr.io/ledgetoptechnologies/engineering-hub-control:latest`.
- Digest: `sha256:39b04915948b041ed2a47e673caef038c704d4df5f245279ecdbf393c3c928f9`.
- Successful publication: GitHub Actions run `39851111234`. Both saved registry
  and latest manifest hashes match this digest; authenticated registry pull-back
  and all five container smoke profiles passed. Main CI passed 5404 tests, with
  196 skips; browser checks passed 115 tests. These are build/container checks,
  not evidence that TrueNAS installed it.

The last independently observed NAS build was healthy `a333704`, not this build.
That is a dated observation, not a permanent status. Recheck before acting.

## Apply and accept

1. Retain a recoverable complete state checkpoint, including SQLite sidecars and
   dependent files. An online ZFS snapshot can capture running state; verify its
   coverage and coherent recovery rather than copying only the SQLite main file.
2. Pull/redeploy the existing app through TrueNAS. A simple restart does not
   fetch a new image. No Compose edit is needed for this ordinary update.
3. Check `http://192.168.50.80:8770/health`: `ok` must be true and `build_commit` must
   equal the full source above. If identity differs, do not assume the update
   succeeded or resume dependent staging work.
4. Open `https://hub.ledgetoptechnologies.com` using the existing Hub Cloudflare
   Access policy. Check preserved notes, tasks and decisions. Test a clearly
   named disposable project and note. An agent prepares disposable task/decision
   context, image attachment and labeling set; Beau reads, answers and labels in
   the UI. Check preserved message history through MCP, not a browser inbox
   (there is no browser inbox page). Confirm edits survive app recreation.
5. Check all three agents with their existing identities at port 8770. Confirm
   unsigned/spoofed browser access at port 8771 is denied; do not paste tokens or
   signed assertions into chat or logs. Correct-session wake requires its own
   addressed request/reply check; an open stream is insufficient.
6. Record the build and results in the existing Hub update check-in. Do not
   create a second decision or treat its answer as execution authorization.

Keep Project Alpha, tunnel networking and Ops sign-in unchanged. Browser port
8771 remains LAN-reachable but requires a verified Access assertion; this is
application-level protection, not physical network isolation.

## Recovery and remaining work

After new-format writes, do not point the old `a333704` image at this state or
call a pre-upgrade checkpoint a lossless rollback. Preserve the new authority
and use reviewed compatible forward recovery; see
[record-format recovery](Hub-record-format-recovery.md).

PR53 initial protected-ledger enrollment and PR54 idle-stream shutdown are tested
development follow-ups, not included in this published artifact. They do not
require changing this ordinary update's Compose. The measured stream fix is not
proof of the original NAS exit137 cause.

Live staging still needs an accepted protected-state layout, current executor
and private TLS/proxy enrollment, NAS-to-executor workflow acceptance and recovery
checks. The current rootless Engine identity alone does not prove those things.
The earlier [isolated pilot](staging-pilot-acceptance-2026-10-21.md) proved a real
bounded negative-candidate rollback campaign, not the live NAS connection or
reboot behavior. Do not enable execution merely to test UI features.

Thomas's project-scoped login and arbitrary-code modules are not enabled by this
release. Adding another human to the current global allowlist would not provide
LandScript-only access.
