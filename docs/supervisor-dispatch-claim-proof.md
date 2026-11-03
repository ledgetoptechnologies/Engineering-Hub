# Supervisor dispatch: fresh one-use claim proof and Hub in-flight barrier

Status: design only. Nothing here is implemented or enabled. Dispatch over the
supervisor route stays disabled until both halves below exist and are reviewed.

## The race

Reserve-before-dispatch lets the Hub commit a signed reservation receipt in its
own transaction, together with its job, run, fence and owner checks. It doesn't
settle who may dispatch at the moment the bytes arrive:

1. The Hub commits "job J, owner W, fence F, reservation R" and starts an async dispatch.
2. Before the request reaches the supervisor, W's lease expires, or an operator
   pauses, reassigns or revokes J.
3. The supervisor still receives a well-formed dispatch for R and sends it.

A recheck in the Hub just before sending doesn't close this; the change can land
after the recheck. The supervisor has to be able to check, itself, that the Hub
said "W may dispatch R now", recently and only once. The Hub must also be unable to
change ownership while such a statement may still be acted on.

## Design

Two halves. Neither is sufficient alone.

### A. Durable Hub in-flight barrier

A new Hub table, `dispatch_in_flight(job_id, run_id, worker, fence, reservation_digest,
challenge_nonce, issued_at, state)`, guarded by triggers:

- At most one non-terminal row per job (unique while `state = 'in_flight'`).
- A row is inserted in the **same Hub transaction** that checks, against the
  authoritative job store, the current owner, lease, fence, run and that the
  reservation receipt is committed for this job. The claim proof (B) is signed
  for that row, but no usable proof bytes leave the signer, are logged, or are
  returned until the barrier row's commit is durable (committed and synced). If
  the commit or sync fails, the proof is discarded and never released. Tests
  cover commit failure and fsync failure.
- Leaving `in_flight` needs evidence: `dispatched` / `refused` only from a
  verified signed supervisor observation recorded in the Hub transaction (the
  inspection transport); `expired_unsent` only from a signed supervisor statement
  that the challenge nonce was never consumed and can no longer be (see B.4).
- Lease renewal, pause, reassignment, revoke and run-cancel for that job all check
  the table in their own transaction. While a row is `in_flight` they can't complete:
  - **pause / reassign / cancel**: refused, or recorded as *pending* and
    applied only after the row leaves `in_flight`;
  - **revoke** (incl. emergency halt): marks the job `revoke_pending`, blocks new
    proofs at once, and completes only after the row resolves. If it can't resolve,
    the job stays held for an operator (below);
  - **lease expiry**: doesn't free the job while a row is `in_flight`; the job is
    `held` (unknown), not reclaimable.
- **Crash**: the row is durable; after a Hub restart the job is held until
  inspection resolves it. Nothing re-dispatches automatically.

### B. Fresh one-use claim proof, verified by the supervisor

1. **Challenge.** The Hub asks the supervisor for a dispatch challenge for
   reservation R. The supervisor durably records `{nonce, reservation, issued_mono}`,
   where `issued_mono` is the supervisor's own `CLOCK_BOOTTIME` and its boot id,
   and returns the nonce. Outstanding challenges are capped per executor, as in
   the CI handoff's fresh-claim challenge.
2. **Proof.** Inside the barrier transaction (A) the Hub signs, with its pinned
   Hub dispatch key under its own Ed25519 domain, canonical
   `{protocol, audience, hub_key_id, job_id, run_id, worker, fence, lease_epoch,
   reservation_receipt_sha256, command_id, counter, executor, challenge_nonce,
   supervisor_boot_id}`. It deliberately carries **no wall-clock lease time**.
3. **Dispatch.** The Hub sends `dispatch(receipt, command, proof)`. In one journal
   transaction, before any byte goes out, the supervisor:
   - verifies the proof against the pinned Hub key and that every field matches
     the receipt, the reservation row and the command exactly;
   - requires the nonce to be outstanding, unconsumed, issued in the current boot,
     and `now_mono - issued_mono <= proof_window` (a few seconds), measured only on
     the supervisor's own monotonic clock, so no cross-host clock trust is needed;
   - consumes the nonce (one use) and marks the reservation dispatched, then sends.
   Any failure is held with zero bytes.
4. **Expiry is one-sided and provable.** A nonce not consumed within `proof_window`
   is dead for good. In **one** supervisor journal transaction the supervisor
   checks that the nonce is not consumed and its reservation not dispatched, and
   marks the nonce expired (revoked). Only after that commit can it sign
   `{nonce, reservation, status: expired_unconsumed}`. A dispatch racing the
   expiry either consumes first (expiry then refuses) or finds it expired (held);
   both orders are race-tested. That signed statement is the only thing that lets
   the Hub move the barrier row to `expired_unsent` without an operator. A
   restored or rolled-back supervisor journal could resurrect an outstanding
   nonce; that stays an open anchor gap.
5. **Hub-side margin, not a proof.** The Hub issues a proof only if the worker's
   lease has at least `proof_window + transit_margin + skew_margin` left. This
   does **not** prove the lease is unexpired when the bytes are sent: the proof can
   be delayed arbitrarily (Hub suspend, slow network) and the window only runs on
   the supervisor's clock from challenge issue. The margin only keeps the normal
   case from becoming an operator hold. What the supervisor actually verifies is
   described under "Authority model".

### Authority model

The authority a dispatch carries is **retained original ownership under the
barrier**, not an unexpired wall-clock lease.

- While a barrier row is `in_flight`, the original owner keeps authority to have
  that one reservation dispatched, even if its wall-clock lease expires or the Hub
  is suspended. Lease expiry during that time doesn't revoke it; it only makes the
  job `held` for everything else (no renewal, no reclaim, no reassignment).
- So a proof delayed past the Hub-side lease expiry, or across a Hub suspend, may
  still be accepted, provided the supervisor-side window and nonce checks pass.
  That is the defined behaviour, and it is tested as such.
- The only way to end that authority without the supervisor's signed outcome is
  the supervisor-side expiry in B.4. Nothing in the Hub can end it unilaterally.
- If a deployment needs "never send after wall-clock lease expiry", that requires
  an expiry check the host can enforce itself (a supervisor-trusted clock or a
  supervisor-held lease). It is out of scope here and isn't provided by this design.

### Revocation is coordinated, not immediate

Session and grant revocation live in a separate database from the job store. So:

- Every revoke and expiry path (session revoke, grant revoke, installation
  revoke, emergency halt, lease expiry) must coordinate with the Hub job store,
  with pending semantics: it records the revocation, blocks new proofs at once,
  and the affected jobs reach `revoke_pending` until their barrier rows resolve.
- There is no promise of an immediate halt of a dispatch already authorised by a
  proof. Halting work already sent is the executor effect fence's job.

### Why both

- Without A, ownership can change after the proof is signed, and the supervisor
  would accept a proof that was true when made.
- Without B, the supervisor has no host-verifiable reason to believe a dispatch
  reflects current Hub authority; an old Hub decision, a replay or a Hub that lost
  its lease could dispatch.
- Together: ownership can't change while a proof could still be used (A), and a
  proof can't be used after `proof_window` or twice (B). So any dispatch the
  supervisor performs happened while the Hub's barrier was holding ownership fixed.

## Failure handling

| Case | Result |
| --- | --- |
| Hub crash after barrier commit, before send | Row in flight, job held. Inspection: nonce consumed -> resolve from signed outcome; signed expired_unconsumed -> `expired_unsent`, job freed. |
| Supervisor crash after nonce consumed | Inspection reports dispatched / unknown; unknown stays held. |
| Network loss either way | Same as crash; never re-send under the same proof or counter. |
| Revoke / emergency halt while in flight | New proofs blocked at once; job `revoke_pending` until resolved; already-sent work is the effect fence's problem, not this one's. |
| Supervisor rolled back (restored journal) | A consumed nonce could reappear as unconsumed. Not detected without an external anchor; listed as open. |
| Hub rolled back | A resolved barrier row could reappear in flight (safe: held), or an in-flight row could vanish (unsafe). Same open anchor gap. |

## Weaker alternative: operator-exclusive mode

If A or B isn't built, the only honest mode is this: dispatch over the route
requires an explicit operator lock on the executor, and while it is held no
automatic pause, reassignment, revoke or lease expiry may act on jobs for that
executor. Every dispatch is operator-attended. The Hub must say so in the UI and
in the job record, and the mode must refuse unattended or concurrent jobs. It
gives no guarantee against an operator acting through another path; it relies on
the operator. This is strictly weaker and should only be a stopgap.

## Open (unchanged)

- Old or direct writers (anything that changes job ownership, leases or
  reservations without going through the barrier checks) and restores of either
  database break these guarantees. They need enforced exclusion (single writer,
  schema/trigger checks, refusing old schema versions), not just convention.
- No external non-rollback anchor for the Hub DB or the supervisor journal.
- Detached executor work isn't fenced by any of this.
- Keys: Hub dispatch key, supervisor reservation and inspection keys; enrollment
  and rotation not designed here.
- Cross-uid / fd delegation limits of SO_PEERCRED, as documented for the route.

## Proposed split

- Hub agent (Codex): the barrier table, triggers and the job-transition checks
  (A), plus proof signing inside that transaction. No send.
- Claude: supervisor challenge issue, proof verifier, nonce consumption in the
  dispatch transaction, the signed expired_unconsumed statement, and extending
  inspection to report nonce state. Disabled and default-refusing throughout.
