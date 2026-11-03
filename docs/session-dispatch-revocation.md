# Coordinated revocation for session-bound dispatch

Status: design only. Nothing here is implemented or enabled. It complements
`docs/supervisor-dispatch-claim-proof.md`, the Hub typed-authority design
(`docs/hub-dispatch-typed-authority.md`, Codex, local 88c7ba5) and Codex's
session gate draft (local `codex/session-dispatch-gate` 27b1a9c), whose
deferral and freeze model this document adopts. No gate or proof is released
until both halves are implemented, independently reviewed, and resolution is
authenticated by the Hub.

## Scope

- **Covered:** Hub jobs whose immutable `job_authority` row has kind `session`,
  meaning installation, session and grant revision were recorded from a live
  session operation in the same Hub transaction that created the job.
- **Not covered:** `legacy_credential` jobs. They carry no session identity and
  none is ever fabricated or inferred for them. Their authority is the enrolled
  credential principal plus an operator-approved policy; revoking that credential
  or policy is Hub-owned, keyed on (principal, policy revision), with the same
  pending semantics.
- **Unchanged:** jobs created before this lands keep their current
  queue/no-send behaviour and are never upgraded to proof eligibility, so they
  never get a gate and revocation never touches them.
- Legacy MCP tokens keep working exactly as today.

## What exists today (verified)

- The session store is its own SQLite file (`HUB_AGENT_SESSIONS_DB`), separate
  from the Hub DB (`HUB_DB`) and the bus/knowledge DB (`AGENT_BUS_DB`). Startup
  refuses a session DB that contains Hub or bus tables.
- Every session-store write runs in `AgentSessionRepository._transaction()`
  (process `RLock` plus `BEGIN IMMEDIATE`).
- Revoke paths, each one transaction in the session DB: `revoke_session`,
  `revoke_installation` (cascades to every live session of the installation)
  and `SessionHubBridge.grant_revoke`. After commit, `_invalidate` only notifies
  in-process transport listeners; it doesn't reach the Hub job store.
- Expiry is lazy: `_principal` and the bridge's `_live` refuse revoked or
  expired sessions, installations and grants when used. There is no sweeper.
- Lock order is session writer, then Hub writer, then knowledge writer. The
  bridge's `admit` holds the session `BEGIN IMMEDIATE` through the Hub commit.
- Queue submissions today are all legacy (`hub_work_jobs.actor` in `AGENTS`, no
  session provenance).
- The Hub's `WorkQueue.set_paused` is a separate, Hub-only pause.

## Design

### Gate (session DB)

A gate records which session, installation and grant revisions an in-flight
dispatch was issued under. It is staged only inside proof issuance (session
`BEGIN IMMEDIATE` first, Hub `BEGIN IMMEDIATE` nested, Hub commit then session
commit; proof bytes leave only after both succeed).

```
session_dispatch_gates(
  job_id, barrier_id,                    -- PRIMARY KEY (job_id, barrier_id)
  installation_id, session_id,
  grant_revision, session_revision, installation_revision,
  state CHECK (state IN ('open','resolved')),
  opened_at, resolved_at, resolution_evidence_sha256)
```

- Insert once, only as `open`, only if no pending revocation covers its session,
  installation or grant, and only if the session, installation and grant are
  live at the recorded revisions (the 27b1a9c `bind_locked` checks).
- One change only: `open` to `resolved`, with `resolved_at` and
  `resolution_evidence_sha256` set and every other column unchanged. No delete,
  no replace.
- A retry of the same job gets a new `barrier_id`, so it gets a new gate; the
  Hub must keep (job_id, barrier_id) distinct per attempt. A retry is never
  possible under a revoked or pending subject, because staging refuses it.

### Freeze while a gate is open

While at least one **open** gate references them, these rows are frozen (as in
27b1a9c, but scoped to open gates rather than any gate):

- the gated `agent_sessions` row: every column except `last_used_at`;
- the gated `session_installations` row: every column;
- the installation's `session_hub_grants` row: insert, update and delete.

Once no open gate references a row, normal writes (renewal, approval, expiry
changes, revocation) work again.

### Revocation (deferral)

Each revoke path keeps its single session transaction and never opens the Hub
DB, so the lock order is never inverted.

- **No open gate covers the subject:** revoke immediately, exactly as today.
- **An open gate covers the subject:** don't touch the frozen row. Insert an
  immutable pending row
  `session_dispatch_pending(revocation_id PRIMARY KEY, scope, subject,
  subject_revision, actor, requested_at)`, with a fresh unique `revocation_id`
  per revoke request, and audit `X.revocation_pending`. Several pending rows may
  exist for the same subject over time (repeat revokes, revoke after a regrant);
  "pending" means at least one row for the subject with no completion row. The API reports `state: revocation_pending, reason:
  dispatch_in_flight`. `_invalidate` still runs, so live transports drop.

Scope rules:

- **Session revoke:** deferred only if that session has an open gate. A sibling
  session of the same installation without a gate is revoked immediately.
- **Installation revoke:** deferred if any open gate references the
  installation. Pending at installation scope blocks every session of it.
- **Grant revoke:** deferred if any open gate references the installation.
  Grant-scope pending blocks Hub actions (the bridge's `_live`) and new gates,
  but not ordinary non-Hub session actions (`_principal` checks session and
  installation scope only).

What pending refuses, immediately:

- `_principal` and installation credential exchange: session or installation
  scope pending refuses with `session_revocation_pending`.
- Bridge `_live`: any scope refuses with `hub_grant_revocation_pending`.
- Gate staging: any covering scope refuses.

This is **not** an immediate halt of a dispatch that is already gated. Its
original authority is retained until resolution.

### Resolution and completion

When the Hub barrier for a gate leaves `in_flight` (verified signed supervisor
observation, or the supervisor's signed `expired_unconsumed` statement), a
resolver runs in session-then-Hub order:

1. Session `BEGIN IMMEDIATE`.
2. Hub read transaction, opened after the session writer and **held until the
   session commit returns**, so the Hub's terminal data can't change before
   completion is recorded. Inside it:
   - the barrier for (job_id, barrier_id) is terminal, and its terminal evidence
     is permanent and immutable in the Hub (trigger-protected, never deleted or
     replaced);
   - the exact signed supervisor bytes (inspection observation, or
     `expired_unconsumed` statement) are re-verified here against the
     separately enrolled supervisor key and executor pin. A stored hash is only
     a reference, never proof of authenticity;
   - the signed barrier, challenge, counter, boot id, command and reservation
     receipt match the barrier row and the gate, and the recorded
     session/installation/grant revisions match the gate;
   - an `expired_unconsumed` statement resolves only the gate whose barrier
     references that exact retired reservation (command_id, counter, receipt
     sha256) and challenge; it never resolves a different attempt.
3. Mark the gate `resolved` with the evidence hash.
4. For every uncompleted pending revocation whose subject no longer has any
   open gate, apply the deferred revocation in the same session transaction:
   set `revoked_at` (cascading to sessions for an installation, or revoking the
   grant), bump revisions, audit `X.revoked`, and insert an immutable
   completion row `session_dispatch_pending_completed(revocation_id PRIMARY KEY,
   completed_at, resolving gate (job_id, barrier_id), evidence sha256)`.
   Pending rows stay immutable.
   - An installation revocation completes only when **every** open gate on any
     session of that installation is resolved, not just the one being resolved
     now.
5. Commit the session transaction, then end the Hub read; then run
   `_invalidate` for any sessions revoked.

- **Idempotent:** an already-resolved gate or a completed pending row is a
  no-op, and crashes between steps are retried.
- **Ambiguous evidence:** missing, unverifiable or mismatched evidence leaves
  the gate open and the revocation pending.
- **What resolution means:** a signed "launched" observation proves only that
  the dispatch was accepted, not that the work it started has terminated.
  Resolution settles the dispatch outcome; stopping running work is the
  executor effect fence's job. Until the Hub has an authoritative,
  reservation-wide terminal-evidence record, an owner-bound Hub transaction
  API and an enrolled supervisor statement key, the resolver stays disabled
  and every deferred revocation simply stays pending.
- **No automatic deletion, re-dispatch or release on ambiguity.**

A subject whose pending row has completed is revoked like any other; a later
fresh grant for the same installation is a new grant revision and isn't blocked
by the completed grant-scope row (pending checks ignore completed rows).

### Expiry

Expiry stays lazy. A frozen row's `expires_at` is unchanged but still expires:
after it passes, `_principal` and `_live` refuse new use and staging refuses new
gates, while an open gate's already-issued dispatch keeps retained authority
until resolution, as with revocation.

### What revocation does to the Hub job

Nothing directly. After resolution, the job can be dispatched again only with a
new barrier and a new gate, which a revoked or pending subject can't get, so the
job stays held until an operator reassigns or cancels it.

### Emergency halt

The Hub pause is checked in the Hub half of issuance and blocks new proofs for
every job, session-bound or legacy. It is independent of session revocation.
Neither halts work already sent; that is the executor effect fence's job.

## Not covered (unchanged gaps)

- Restore or rollback of either SQLite file, and old or direct writers
  (including DDL-capable ones), break these guarantees. They need an enforced
  deployment boundary and an external anchor.
- `_invalidate` stays a best-effort in-process notification.
- Legacy-credential revocation semantics are the Hub's.

## Tests to write with the implementation

- Each revoke kind with no gate (immediate), an open gate (pending) and only
  resolved gates (immediate).
- Sibling session revoke of a gated installation is immediate.
- Grant pending blocks Hub actions and new gates but not plain session actions.
- Revoke versus staging in both orders (deterministic barriers on the session
  writer).
- Hub commit succeeded, session commit failed: no proof bytes, barrier held,
  no gate, recovery holds.
- Resolution: valid evidence resolves the gate and completes the pending
  revocation in one transaction (`revoked_at` set, audit written, completion
  recorded); bad or mismatched evidence leaves both open; repeat is a no-op;
  crash between steps.
- After resolution, frozen rows unfreeze: grant renewal and installation
  updates work.
- Retry of the same job needs a new barrier_id; staging refuses it under a
  revoked or pending subject.
- Simultaneous scopes: session, installation and grant revocations pending
  together over several gates on several sessions; each completes only when all
  of its own gates resolve; the installation cascade waits for every gate.
- Repeat revokes and revoke-after-regrant: each gets its own revocation_id and
  completes independently; a completed revocation never blocks a later grant
  revision.
- Resolution authenticity: a correct evidence hash with tampered or unsigned
  bytes, a different supervisor key, a different pin, or a signed statement for
  another reservation, challenge, counter or boot leaves the gate open.
- The Hub's terminal barrier data changing between check and session commit is
  impossible while the Hub read is held (deterministic test that a Hub writer
  blocks until the session commit returns).
- Lazy expiry of a frozen session refuses new use but keeps the gate open.
- Trigger tests: gate insert once, single transition, no delete or replace;
  pending and completion rows immutable; freeze only while a gate is open.
- A legacy-credential job never gets a gate and is unaffected by session revoke.
- Lock order: no path opens the Hub writer before the session writer.
