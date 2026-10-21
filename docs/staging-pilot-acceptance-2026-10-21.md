# Bounded queued staging pilot: actual acceptance

The isolated Linux TEST campaign `5550ee4d-2ce0-4ed3-8aca-d7dcca8bcdbf`
passed a real native queued failure/rollback test on 2026-10-21. A separate
reviewer independently re-read the protected receipts, Engine state and closed
SQLite databases over SSH. This is container/staging acceptance, not a TrueNAS
deployment, production approval or reboot-recovery test.

## Scope and identity

- Shared staging host: `192.168.50.90`; existing isolated rootless Engine and
  dedicated TEST tokens/signing key only. No new credentials or access grants.
- Aggregate limits remained one CPU, 3.25 GiB RAM, zero swap, 256 PIDs and a
  dedicated 3 GiB test volume. No boot autostart was enabled.
- Application source: `d0d85409b123b9ee0f531dd2dd02669bb71745ea`.
  Claude's live source review of task
  `de03426c-f7da-4b3e-a110-1e2a0887687e` was imported as a snapshot by Codex;
  it was **not** a new TEST-principal review or an execution authorization.
- Executor implementation: `d5656bfd9271a1b1c831294f544c909d33652b3b`;
  its core identity remains `cce316a326c8db1d681ad7e591f45dd4cc60d3c0`.
  These are distinct identities.

## Actual native workflow

Exactly one queue submission created job
`a5762028-3677-4abf-ac26-ec97e3d0ce55`. The native event runtime, not an
external claim/run/completion wrapper, executed run
`9b5a3c70-369b-4dfb-8dbe-0b2ff012f08c`.

| Phase | Actual result |
| --- | --- |
| Deploy immutable missing-index TEST image | Succeeded; config `245d7459…` |
| Candidate smoke test | Failed, `health_failed`, verified unhealthy evidence |
| Deploy immutable healthy rollback | Succeeded; config `378cdb3c…` |
| Rollback smoke test | Succeeded, verified healthy evidence |

The controller finished `rolled_back`. The queue finished `failed`, with no
reconciliation required: that is the correct negative-candidate outcome, not a
successful candidate deployment. Native worker activity became false; its last
job was the same failed job. The machine reservation cleared at fence 1.

Independent host-Python probes inside only the rollback network namespace
returned `/health` 200, `/` 200 and unauthenticated overview 401. Actual
image/profile, PID/start, UID mapping, capabilities, cgroup limits and mount
flags were checked. The failed candidate remains retained for inspection;
rollback does not imply an unrequested stop or deletion of that container.

## Shutdown and durable proof

The coordinator received one explicit TERM and exited 0/PID 0 without OOM.
The original root launcher then received one explicitly bound SIGUSR1 request;
native `graceful_stop()` stopped the exact executor, reconciled terminal Engine
accounting, committed its journal as `stopped` and removed its own issuer socket.
The launcher exited 0. No force, replay, unknown-container adoption or old held
journal cleanup was performed.

After both writers stopped, independent immutable read-only SQLite checks proved
the private job/run/UUID-derived request mapping, exact owner epoch, quiescent
attempt, absent advances, released machine and all four command responses.
The final target ledger selected the healthy rollback container at fence 1.
Both databases passed integrity checks with no nonempty WAL/journal and stable
identities/hashes across the audit.

| Protected receipt | SHA-256 |
| --- | --- |
| Native observations | `754cd32ce62c737051b2800ef5ca4e9b264266a2736edcbc864f207dfaf06046` |
| Independent health | `0387648c2a2df2d485274b18f05751b6cabc5a6fd04427788019e171bd1a815c` |
| Coordinator terminal | `b9acbf8c2c2d3b73179c530e7f65efd56cd1b993152d1d9fe12ccc1e831cf0d0` |
| Native executor stop | `1f5c179fc72a6b60a2d6cbcf307e9275c19c8185fafc8e42d76bfa11a5be83eb` |
| Closed-ledger audit | `9b90c951ae8c28e31b09b636c7f0958049dafe03b436c2e59d4f6fe92c62d6a3` |

Sixteen public TEST receipts were captured under the operator's
`Documents/Codex/eh-completed-rollback-5550` directory; manifest SHA-256
`0b17438bd7eab849999ceb9b408910fde6face10154cfaaea7474bb471deced6`.
No credentials or database copies are included. Shared Hub note:
`engineering-hub/runbooks/bounded-pilot-5550-baseline-checkpoint.md`.

## Integration corrections and remaining gates

### Separate live dashboard observation

After the TEST campaign, a read-only browser check of the existing
`https://hub.ledgetoptechnologies.com/` session at approximately 20:55 UTC
on 2026-10-21 showed the authorized LTDS email and `Cloudflare Access`, with
the dashboard visible and no extra sign-in gate. An explicit workspace refresh
completed and displayed current transport snapshots for all three agents.
An explicit Shared notes refresh changed the list from 15 to 17 entries; opening
`runbooks/bounded-pilot-5550-baseline-checkpoint.md` displayed version 7 and its
current queued-rollback evidence. This verifies an agent-written note is readable
in the actual authenticated human UI, not only through an isolated API fixture.

Separate direct-origin GET probes from the authorized workstation also passed:

| Probe | Actual response |
| --- | --- |
| Browser root on NAS port 8771, no assertion, public Hub Host | 401 `access_required` |
| Same request plus spoofed allowed-email header | 401 `access_required` |
| Same plus malformed test assertion | 401 `access_required` |
| Agent overview on port 8770 without a token | 401 `unauthorized` |

These requests used no cookies or credentials, no proxy or redirect, and changed
no configuration. They prove these specific direct-origin rejection cases, not
every invalid-signature/issuer/audience combination or network isolation.

This observation did not log out, create a new login, change a note or decision,
inspect cookies, or modify any service. It does not verify the deployed image
revision, the other allowed email, 30-day session expiry,
human-edit conflict handling, reboot recovery or exact-session wake. In the
refreshed view Codex had active transport streams but zero observed inbox
subscriptions; the UI correctly did not equate transport connectivity with
topic readiness. The live software queue remained unconfigured and no staging
target was enrolled. The isolated TEST pilot must not be described as live
NAS-to-staging dispatch.

### Client corrections

Two pre-submission client failures were reconciled before the first POST:
Linux pathname sockets exceeded `sun_path`; the corrected executor observer uses
a retained, identity-checked directory FD. The browser-only socket correctly
rejected agent Bearer credentials. Agent requests instead used the existing
literal loopback listener inside the exact coordinator network namespace, with
process-owned listener-inode and PID/start checks. Browser security was unchanged.

Focused repository regression checks: 92 passed, one Linux-only skip on Windows;
the actual native Linux pilot supplies separate runtime evidence. Earlier tests
against the default Windows temporary directory had four setup permission errors;
a fresh dedicated test directory resolved those without product changes.

Still separate: publication of the executor fix, approved deployment/activation,
actual TrueNAS acceptance and the remaining authenticated-dashboard checks above, reboot/session recovery,
and incremental project enrollment. External collaborators additionally need
enforced project authorization and separate revocable identities before access.
No production, PA/Viewer, TrueNAS, Cloudflare or live MCP changes were made by
this pilot. Its success does not create generalized builder trust or authority
to resume an old ambiguous execution.
