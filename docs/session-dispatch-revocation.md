# Coordinated revocation for session-bound dispatch

Status: design only. Nothing here is implemented or enabled. It complements
`docs/supervisor-dispatch-claim-proof.md` and the Hub typed-authority design
(`docs/hub-dispatch-typed-authority.md`, Codex, local commit 88c7ba5). No proof is
released and no dispatch is enabled until both are implemented and reviewed.

## Scope

- **Covered:** Hub jobs whose immutable `job_authority` row has kind `session`,
  meaning installation, session and grant revision were recorded from a live
  session operation in the same Hub transaction that created the job.
- **Not covered:** `legacy_credential` jobs. They carry no session identity and
  none is ever fabricated or inferred for them. Their authority is the enrolled
  credential principal plus an operator-approved policy; revoking that credential
  or policy follows the same pending semantics below, but keyed on
  (principal, policy revision) and owned by the Hub, not the session store.
- **Unchanged:** jobs created before this lands. They keep their current
  queue/no-send behaviour and are never upgraded to proof eligibility, so they
  never get a gate and revocation never touches them.
- Legacy MCP tokens keep working exactly as today.

## What exists today (verified)

- The session store is its own SQLite file (`HUB_AGENT_SESSIONS_DB`), separate
  from the Hub DB (`HUB_DB`) and the bus/knowledge DB (`AGENT_BUS_DB`). Startup
  refuses a session DB that contains Hub or bus tables.
- Every session-store write runs in `AgentSessionRepository._transaction()`
  (process `RLock` plus `BEGIN IMMEDIATE`).
- Revoke paths, each one transaction in the session DB:
  `revoke_session` (sets `agent_sessions.revoked_at`),
  `revoke_installation` (sets the installation's `revoked_at` and cascades to
  every live session of it), and `SessionHubBridge.grant_revoke` (sets
  `session_hub_grants.revoked_at`). After commit, `_invalidate` only notifies
  in-process transport listeners; it does not reach the Hub job store.
- Expiry is lazy: `_principal` and the bridge's `_live` refuse revoked or expired
  sessions, installations and grants when used. There is no sweeper.
- The bridge's documented lock order is session writer, then Hub writer, then
  knowledge writer. `admit` holds the session `BEGIN IMMEDIATE` through the Hub
  commit, so a revoke either wins first (the operation is denied) or waits until
  the operation has committed.
- Queue submissions today are all legacy: `hub_work_jobs.actor` must be in
  `AGENTS`, `hub_work_*` tools aren't in the bridge's tool set, and the queue
  writes no session provenance.
- There is no emergency halt in the session store. The Hub's
  `WorkQueue.set_paused` is separate and Hub-only.

## Design

### Session gate (session DB)

New table `session_dispatch_gates`, written only inside the proof-issuance
operation described in the typed-authority design (session `BEGIN IMMEDIATE`
first, Hub `BEGIN IMMEDIATE` nested inside it, Hub commit first, then session
commit):

```
gate_id TEXT PRIMARY KEY, installation_id, session_id, grant_id,
grant_revision, hub_job_id, barrier_id, challenge_sha256,
state TEXT CHECK (state IN ('open','resolved')), opened_at, resolved_at,
resolution_evidence_sha256
```

Triggers: insert once and only as `open`; the only update is `open` to
`resolved`, once, with `resolved_at` and `resolution_evidence_sha256` set and
every other column unchanged; no delete; no `INSERT OR REPLACE` over an existing
row. At most one open gate per `(hub_job_id, barrier_id)`.

### Revocation (session DB only)

Each revoke path keeps its current single session transaction and current
effects (setting `revoked_at` refuses every later `_live` check, so new gates and
new session actions are refused at once). Inside the same transaction it also:

1. finds the open gates for the affected subjects (the session; every session of
   the installation; or the grant);
2. inserts one `session_revocation_pending(revocation_id, kind, subject_id,
   revoked_at, state CHECK IN ('pending','complete'))` row, plus a link row per
   open gate;
3. marks the revocation `complete` immediately if there are no open gates.

The revoke never opens the Hub DB, so the session-then-Hub lock order is never
inverted. The revoke result reports how many in-flight dispatches it is waiting
on. This is **not** an immediate halt: an already-barriered dispatch keeps its
original authority until the supervisor resolves it.

Races, all decided by the session writer lock:

- Revoke commits first: the issuance operation's live check fails; no gate, no
  barrier, no proof.
- Issuance commits first: the gate is open; the revoke records it as pending.
- Issuance's Hub commit succeeded but its session commit failed: the Hub barrier
  exists with no gate. Recovery treats a barrier without a matching gate as
  held and never releases proof bytes (the typed-authority design already
  forbids releasing bytes unless both commits returned success).

### Resolution

When the Hub barrier for a gated job leaves `in_flight` (verified signed
supervisor observation, or the supervisor's signed `expired_unconsumed`
statement), a resolver closes the gate, in session-then-Hub order:

1. session `BEGIN IMMEDIATE`;
2. Hub read transaction: the barrier row is terminal and its evidence verifies
   against the pinned keys; the job, barrier and challenge match the gate;
3. mark the gate `resolved` with the evidence hash; if every gate linked to a
   pending revocation is now resolved, mark that revocation `complete`;
4. end the Hub read, commit the session transaction.

Resolution is idempotent and retried after crashes. Ambiguous evidence leaves the
gate open; nothing is deleted or re-dispatched automatically.

### What revocation does to the Hub job

Nothing directly. After its barrier resolves, the job can be dispatched again
only through a new proof, which requires a new gate and therefore a live
session/grant: a revoked one is refused, so the job stays held until an operator
reassigns or cancels it. Natural session or grant expiry behaves the same way
(retained authority for what is in flight; nothing new).

### Emergency halt

The Hub's pause (`set_paused`) is checked in the Hub half of issuance, so it
blocks new proofs for every job, session-bound or legacy. Session revocation and
Hub pause are independent; either one blocks new proofs. Neither halts work
already sent; that remains the executor effect fence's job.

## Not covered (unchanged gaps)

- Restore or rollback of either SQLite file, and old or direct writers, break
  these guarantees; they need an enforced deployment boundary and an external
  anchor.
- `_invalidate` stays a best-effort in-process notification; nothing relies on it.
- Legacy-credential revocation semantics are the Hub's (typed-authority design).

## Tests to write with the implementation

- Each revoke kind with zero, one and several open gates; pending vs complete.
- Revoke versus issuance in both orders (deterministic barriers on the session
  writer): denied, or gate open and pending.
- Hub commit succeeded, session commit failed: no proof bytes, barrier held, no
  gate, recovery holds.
- Resolution: valid evidence resolves and completes; bad or mismatched evidence
  leaves the gate open; repeated resolution is idempotent; crash between steps.
- After revocation and resolution, a new proof for the same job is refused.
- Gate and pending-row triggers: insert once, single transition, no delete, no
  replace, columns immutable.
- A legacy-credential job never gets a gate and is unaffected by session revoke.
- Pre-existing jobs are never proof-eligible.
- Lock order: no code path opens the Hub writer before the session writer.
