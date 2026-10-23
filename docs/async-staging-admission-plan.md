# Async staging admission: source implementation and acceptance design

Status: **implemented opt-in source, not activated**. Isolated gate, broker,
queue, signed local TLS and actual unified service tests cover the integration.
Immutable full-suite verification and real-host acceptance must still be reported
separately; source tests do not establish NAS, Engine, restore or reboot readiness.
This document does not grant
admission, change any configuration, provision credentials, launch workers or
prove NAS, deployment, restore or reboot acceptance. The explicit trusted-session
selector remains unsuitable for activation until its shared-loop responsiveness
and the other real-host acceptance requirements are satisfied.

## Problem and evidence

The installed composition currently supplies a synchronous HTTPS identity
observer to synchronous gate checks. Those checks run on the unified Hub event
loop, including nested queue policy checks and human acceptance. Bounded network
I/O is still blocking I/O.

Source review counted one fresh observation for broker snapshot, four for a
successful acceptance and two for decline. Independent synthetic fixture
instrumentation counted one for runtime `admission_allowed`, two for an attached
bridge `_runtime_admission`, and 25 before the first remote POST in a successful
`run_once` (33 cumulatively before its second POST). These are measured paths,
not a universal maximum: candidate scans can run eligibility checks for multiple
rows. The default exchange ceiling is two seconds. Slow successful observations
can therefore cause a long uninterrupted event-loop stall; a failed observation
normally seals admission earlier. Local filesystem and scheduling work remain
cooperatively bounded, not forcibly interruptible.

Lowering the exchange ceiling to 100 ms is availability tuning, not a nonblocking
solution. It retains the aggregate stall and can reject legitimate TLS/Engine
inspection under load. Do not substitute cached identity, silently reduce
verification, or claim that such tuning implements this plan.

## Invariants

- Metadata binding, lifecycle status and eligibility previews never grant effect
  authority or claim fresh remote readiness.
- Every semantic effect-admission boundary obtains a new nonce/signature-verified
  observation using independently retained pins and the same captured TLS and
  credential material. No response chooses its own trust authority.
- No observation network I/O occurs inside a SQLite transaction, audit section,
  broker mutex or gate mutex.
- Prepared admission is private, single-use, in memory and scoped to one exact
  operation. ContextVar possession alone is insufficient: child tasks inherit it.
- Bind each preparation to the exact gate instance and generation, PID, asyncio
  loop and task, owner object and epoch, executor generation/artifact, job/run,
  phase/action, target, manifest and canonical operation subject.
- Its deadline is the minimum of verified observation expiry, original report
  expiry, original launch approval ceiling, local action deadline and applicable
  queue lease ceiling. Do not renew any ceiling after an await, clock reversal,
  retry or process reconstruction.
- There is **no yield until the consumed local admission segment completes**.
  Canonical effect admission is then complete; the original bounded remote
  operation may await and must drain even if admission subsequently closes.
- Cancellation, ambiguous transport results and lease expiry never prove that an
  unknown remote effect stopped. Preserve original IDs, evidence and fences.

## 1. Verified observation and gate APIs

Affected paths:

- `src/engineering_hub/current_executor_observation.py`
- `src/engineering_hub/startup_reconciliation.py`
- `src/engineering_hub/lifecycle.py` (reuse its existing owned-drain contract)

Extend the observer with a private verified observation result carrying exact
identity, validated nonce/observation ID, verified local wall-clock baseline,
absolute signed expiry and a conservative local monotonic expiry, all clipped
to original launch approval. Missing absolute bounds deny async admission;
they are never inferred from proof arrival. Do not infer a new validity window from the
time an already-old response finishes. Retain current signature, replay, PID,
clock, original-approval and sticky-denial checks.

The conservative proof ceiling is
`min(nonce_start_monotonic + (signed_expires_ms - signed_observed_ms) / 1000,
nonce_start_monotonic + (signed_expires_ms - local_nonce_wall_ms) / 1000,
verification_monotonic + remaining_absolute_validity,
original_launch_monotonic_ceiling)`, further clipped by gate/report/action/lease
ceilings at preparation. Nonce issuance causally precedes the issuer observation;
using that earlier local start consumes transit/verification delay rather than
minting a new lifetime at response finish. Permitted issuer clock offsets do not
extend this duration. Every consumed-scope check also samples nondecreasing local
wall time after local ownership/proof reads and rejects the earliest retained
signed/report/approval absolute expiry. A forward wall step during local audit
or completion validation therefore cannot be hidden by an unchanged monotonic
clock; a backward step cannot renew authority. Tests cover positive and negative
issuer offsets, delayed responses, original-approval clipping, transit expiry
and independent forward wall jumps during local admission work.

The async path runs only the bounded synchronous observation exchange through a
retained executor Future and `finish_owned`. The awaiting caller can detach only
after that owned I/O is terminal. Cancellation yields no admission preparation.
It must not create an untracked task or close the observer while I/O still runs.

The implemented gate surface uses a closed `AdmissionSubject` (including `kind`)
rather than separate caller-selected kind and arbitrary JSON arguments:

```python
assert_local_binding(owner, target_id, manifest_digest)
assert_local_permitted(owner, target_id, manifest_digest)

async prepare_admission(
    *, owner, subject, deadline
) -> PreparedAdmission

admission_scope(prepared, *, owner, subject)
assert_active_subject(*, owner, subject)
```

`subject` is a closed typed structure, not arbitrary caller JSON: exact job/run,
phase/action, target/manifest and canonical command or decision digest as
applicable. The prepared object is opaque. The gate must retain and compare its
object identity and generation; constructing an equivalent dataclass or copying
its IDs is not authority.

Preparation checks local binding, captures revision/close state, awaits fresh
observation without locks/transactions, then rechecks exact owner, epoch,
generation, identity, revision and all deadlines. Concurrent preparation must be
bounded and fail closed rather than introducing an unbounded waiting queue.

Entering `admission_scope` consumes the preparation once and installs an exact
task/loop-bound operation scope. Recursive local validators can check that same
scope; they cannot turn it into another admission. Exit revokes it. An inherited
ContextVar in a child task must fail explicit current-task comparison. Enforce
the no-yield segment contract, for example with a one-turn invalidation tripwire
plus operation-owner checks, and test it adversarially. A permit cannot stay valid
across an unrelated await merely because its wall/monotonic expiry is later.

### Installed versus legacy synchronous paths

Select installed async-admission mode explicitly in trusted Python composition;
never infer it from JSON flags, a returned identity, a caller-supplied callback or
a response. In this mode, old synchronous effect-admission methods must deny
without the exact active consumed scope. They must not fall back to a local-only
check, cached result or synchronous network observation.

This includes legacy gate-opening APIs, effect assertions and binding entry
points that previously invoked fresh verification. Only explicitly named local
metadata APIs omit observation, and their results cannot satisfy effect checks.

Existing synchronous in-process/unit observer paths may retain their original
fresh, bounded verification contract for separately explicit trusted composition.
Keep this mode distinction closed and test both directions. A test helper's
synthetic identity must never become an installed async-mode bypass. Preserve
existing synchronous public contracts until all genuine effect callers have been
audited; do not globally relabel `assert_binding` as permission.

## 2. Human acceptance remains same-loop atomic

Affected paths:

- `src/engineering_hub/session_acceptance.py`
- `src/engineering_hub/queued_session_acceptance.py`
- `src/engineering_hub/session_acceptance_audit.py`

Broker GET/snapshot uses local binding and reports only pending/closed/accepted
lifecycle, not current remote readiness. Decline closes admission and audits that
decision without needing a remote identity observation.

The async decision entry point is `decide_async`. Authenticate the exact
enrolled human actor and capture request ID, CAS revision and report digest.
Reserve a bounded preparing state before the await so simultaneous decisions
cannot both proceed. This reservation is not acceptance.

Await a fresh `operator_accept` preparation that explicitly permits the pending
gate state. Afterwards recheck actor, CAS, report, owner, epoch, close state and
original expiry. Consume the preparation and perform:

1. Durable intent audit.
2. Local gate opening under the exact consumed scope.
3. Durable result audit.

There is no await or observation I/O between these steps. Recheck local freshness
after each step. One successful `operator_accept` scope permits exactly the
pending generation 0 to accepted generation 1 transition. The gate internally
records that this exact consumed scope performed that transition, so its result
audit can validate generation 1 without allowing arbitrary generation mismatch.
Other scopes, stale preparations and replayed acceptance cannot use that exception.
Recheck local freshness
after potentially blocking audit callbacks. If the intent audit consumes the
preparation's validity, do not open; record the existing failed-result outcome
where possible and seal. Result-audit uncertainty closes future admission but
does not pretend to roll back a previously accepted effect. Closing/cancellation
during preparation vetoes acceptance. Old audit history never opens a new gate.

Do not use `to_thread(broker.decide)`: that removes the same-loop admission/audit
barrier and requires a different cross-thread synchronization design.

The owned audit sink commits one sanitized result pointer in the existing Hub
outbox in the same transaction as its result row. Intent alone emits nothing.
This lets the existing native feed wake an already-queued pending session and
invalidate UI snapshots after acceptance, without direct runtime mutation,
polling or a second submitted job. The pointer carries no report, actor or pins
and is not persisted admission authority; a new process still starts pending.

## 3. Queue selection is metadata; exact claim is admission

Affected paths:

- `src/engineering_hub/work_queue.py`
- `src/engineering_hub/work_queue_api.py`
- `src/engineering_hub/queued_pilot.py`
- `src/engineering_hub/queued_pilot_runtime.py`

Constructor checks, queue submission, status and candidate scanning use explicit
local metadata/policy checks. They still enforce task ownership/review, target,
manifest, pause and configured action restrictions. They do not acquire a machine
or authorize an effect. Pending manual acceptance performs neither claims nor
periodic remote probes.

Introduce a read-only exact candidate plan and a narrowed claim operation:

```python
plan_claim(worker, action_keys) -> ClaimPlan | None
claim_exact(plan, *, worker, lease_seconds)
```

The plan is not a capability. After fresh observation, `claim_exact` consumes an
exact claim scope and transactionally rechecks candidate ID/revision, review,
target, worker, pause, machine generation and production-first precedence. If
selection changed, claim nothing. Never fall through to another job using the
preparation. Preserve existing unknown-production-readiness precedence rules.

Keep generic queue callers' existing policy behavior separate. Only the private
pilot path gets the new exact-plan composition; there is no request flag to skip
claim authority. Recursive pilot callbacks validate the active local operation
scope and existing private job context, rather than making repeated network
calls under the queue transaction.

After a claim, derive a conservative local lease ceiling once; combine it with
the durable lease checks. Do not let a backwards wall-clock step extend it.
Local intent/run creation may be part of a precisely defined claim/create
operation segment, but cannot authorize a later remote phase.

## 4. Fresh preparation for every new remote phase

Affected paths:

- `src/engineering_hub/container_pilot.py`
- `src/engineering_hub/queued_pilot.py`
- `src/engineering_hub/pilot_recovery.py`

Immediately before each new command, construct its canonical original ID and
payload, then await fresh preparation bound to the exact job, run, action,
command digest, fence and owner epoch outside any transaction. Inside its consumed
no-yield segment:

1. Recheck owner, queue lease, review, pause, target, manifest and command state.
2. Durably record that original command as unknown.
3. Admit exactly that bounded remote phase.

Then await the original client operation. Subsequent admission closure does not
abandon an already admitted operation; drain it and retain its outcome. A new
phase always requires another fresh preparation. Existing terminal/original
command records remain idempotent evidence, not permission to invent a new ID.

Cancellation/expiry before phase admission sends no POST. If a claimed job cannot
proceed, retain or hold its reservation according to existing safe policy; do not
claim that cancellation itself proves host exclusion. A possibly transmitted
POST remains unknown/held under its original identity and is never replayed with
a new command ID.

Recovery GETs can retain historical original-command evidence while admission is
closed. Releasing a held reservation requires a separate fresh preparation bound
to the exact conclusive evidence, original job/run and fence. Revalidate those
facts synchronously before reconciliation; uncertainty leaves capacity held.

## 5. Composition, limits and shutdown

`trusted_queued_session.py` supplies this mechanism from the same captured
profile, CA, credential, independent pins and original report. It must not supply
a cached identity callback. `trusted_session_startup.py` remains explicit opt-in;
ordinary Hub startup, endpoints, tokens and Compose remain unchanged.

Add a bounded local pre-effect preparation/action deadline, not a renewable
per-helper budget. Clip it to verified observation, original report/launch and
applicable queue lease ceilings. Keep remote command deadlines separate: expiry
vetoes new phases but cannot justify force-cancelling an accepted unknown effect.
Final values require measured real-host acceptance, not a weakened timeout that
only makes synthetic tests pass.

Shutdown order remains: close new admission; drain pending observation Futures
and accepted phases; drain runtime/listeners; close observer/audit/client; release
owner last. No daemon, polling loop, subscription, automatic launcher or live
configuration change is part of this refactor.

## Acceptance and adversarial tests

- Real isolated TLS plus signed fixture issuer delayed for valid responses: run
  human acceptance and a complete first-POST path alongside Hub health/MCP ping.
  Target observation-induced event-loop gaps and concurrent response p95 below
  250 ms despite a two-second observation exchange. State the measurement scope;
  this is not real NAS/Engine acceptance.
- Actor/CAS, owner/epoch/generation, target/manifest, review, pause, fence, queue
  candidate and production-priority changes during observation all veto action.
- Original report/launch/lease expiry, backwards clocks and late successful I/O
  cannot mint or extend admission.
- Close and repeated cancellation during preparation drain owned I/O, return no
  preparation and send no POST. Close after admission drains the original phase
  but prevents the next phase and unsafe capacity release.
- Preparations cannot be replayed, transferred to another gate/process/loop/task,
  used by a child inheriting ContextVar state, or retained across a yield.
- A slow intent audit exhausts freshness without opening the gate. Audit errors,
  reentry and uncertain commits preserve existing fail-closed semantics.
- Verify the same-loop no-await intent/open/result ordering and prohibit network
  I/O inside all SQLite transactions and audit callbacks.
- Queue plans never authorize a different row after reordering. Metadata APIs,
  status, submission, constructors and pending runtime cannot grant effects.
- Installed async mode rejects legacy scope-free effect calls; explicit legacy
  synchronous unit/in-process mode still performs its original fresh checks.
- Unknown POST outcomes retain original requests, evidence and reservations;
  historical GET evidence alone cannot release a machine.
- Preserve shared UI/MCP behavior, independent review, immutable deployment,
  original-command reconciliation, rollback and lifecycle suites. Run a complete
  immutable tracked-source backend export after integration, separately report
  real-proxy tests, then complete actual host/NAS acceptance before activation.
