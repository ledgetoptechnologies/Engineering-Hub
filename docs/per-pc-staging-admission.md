# Per-PC installation admission to staging, pilot and CI (draft)

Status: design for review by Codex. Not implemented; changes no live service, grant or credential.

Source: Codex worktree `agent-bootstrap-download` (uncommitted, read 2026-11-03). Paths are under `src/`. Read-only: nothing was run.

## 1. Current behaviour

**Identity.** The bridge sets a request-local `HubSessionPrincipal(installation_id, session_id, family, expires_at, authorization_revision)`. Its actor is `installation:<id>` (`agent_message_bus/session_hub_context.py:10-20`). The bridge sets the ContextVar in `admit` (`engineering_hub/session_hub_bridge.py:369-383`) and `recover_claim` (`:263-267`). The helpers `actor_allowed`, `family` and `claim_fields` return a value only while that context exists (`session_hub_context.py:26-70`). Without it, `family(actor)` returns the actor string.

**Grants.** `session_hub_grants` has one row per installation: projects, tool capabilities, expiry, revision and `revoked_at` (`session_hub_bridge.py:116-120`). It has no environment field. Its capabilities cover only MCP tools (`:29-40`), so none grants staging, pilot or CI. `_live` re-validates the session, installation, grant and exact revision only through a session secret or session id (`:223-243`).

**Family on tasks.**
- Claim stores `owner_family` and `owner_session_id` (`repository.py:388`).
- A candidate copies `candidate_family` from `owner_family` (`:413-414`).
- An approving review stores `review.family` only for installations (`:432-434`).
- A legacy row has no family field. Readers fall back to the actor string (`:527-528`).

**Fail-closed gates for installations:**
- `WorkQueue.submit` requires `actor in AGENTS` (`work_queue.py:312`).
- `submit_reviewed_work` requires `review.actor in AGENTS` (`work_queue_api.py:115`).
- `StagingController.submit` requires `actor in AGENTS` (`staging_controller.py:337`). `StagingResourceStore` grants are keyed by the actor string (`staging_resources.py:133-139`).
- `create_run` requires `actor in AGENTS` and `review.actor in AGENTS` (`repository.py:621, 661`).
- `_reviewed_task_data_matches` requires `review.actor in AGENTS` (`:525`). It also compares `session_context.family(actor)` with the candidate family (`:531`). A background worker has no context, so this returns `installation:<id>` and never matches. Installations are therefore refused twice.
- `QueuedContainerPilot._review` (`queued_pilot.py:269-276`) and `QueuedDevelopmentCI._review` (`development_ci.py:179-184`) repeat the `AGENTS` checks.

**Existing recheck hooks to reuse:**
- `WorkQueue.live_claim` (`work_queue.py:574-594`) and `_eligible_target` at claim (`:368-382`) call `target_verifier(row['actor'], …)`.
- The pilot's `authorize('command')` calls `_bound`, which calls `_review`, before every host command (`queued_pilot.py:379-434, 323-363`).
- CI's `admission_guard` calls `_bound`, which calls `live_claim` and `_review` (`development_ci.py:225-241, 301-304`).
- Staging calls `live_claim` once, before the single dispatch POST (`staging_controller.py:628-633`).

**Legacy provenance (fixed in the candidate since this draft was read).** `_reviewed_task_data_matches` now also requires `reviewer_family == review.actor` (`repository.py:530`), so a legacy row with `actor:'claude', family:'codex'` is rejected. As a side effect, every installation reviewer (`installation:<id>`, family `codex`/`claude`) fails that equality, which is a third fail-closed gate for installations. Section 4 replaces it with a per-kind rule.

## 2. Principal model

Add the table `hub_work_principals(job_id PK → hub_work_jobs.id, installation_id, family, submit_session_id, grant_id, grant_revision, project, environment, capability, created_at)`. Write it in the same Hub transaction as the job insert. `hub_work_jobs.actor` stays `installation:<id>`, so idempotency and audit are unchanged. Copy the principal into `hub_queued_pilot_jobs`, `hub_development_ci_attempts` and the staging submission `data`, so each binding check compares exactly.

Add the scope table `session_hub_execution_grants(installation_id, project, environment ∈ {staging}, capabilities ⊆ {work.submit, staging.build, pilot.run, ci.run}, revision, expires_at, revoked_at, approved_by)`. The owner edits it with the same compare-and-set as tool grants. Production is not representable in this table.

Add `InstallationAuthority.verify(principal_row, *, checkpoint)` as a pure function over the authoritative session database. It takes no secret and no ContextVar. It requires all of the following:
1. The installation state is `active`, so not revoked and not expired (`agent_sessions.py:195-200`).
2. `installation.family == principal.family`. Family is never updated after enrollment (`:270, 293, 314`).
3. An execution grant exists that is not revoked, has not expired, and covers the project, the environment and the capability. A changed revision is accepted only if coverage still holds. This is an open question (§7).
4. `submit_session_id` has `revoked_at IS NULL`. A session that merely expired is fine, because sessions last 15 minutes and jobs can run longer.

**Process topology and journal mode (checked in source, not on a live host).**
- The session bridge lives in the Hub app process (`app.py:522`) and holds an OS lock on `<sessions>.hub-bridge.lock` (`session_hub_bridge.py:105-114`), so no second process can run a bridge.
- The pilot worker is built in a separate service process (`queued_pilot_service.py:141`, `unified_queued_pilot_service.py:162`, `serve_owned_queued_pilot` at `queued_pilot_service.py:49`). It cannot call the bridge and must read authority itself.
- The Hub database sets WAL (`repository.py:64`). `agent_sessions.py` sets no journal mode (`_transaction`, `:141-152`), so the session database uses SQLite's default rollback journal unless something else changes it. Check the live file before implementation.
- They are separate files. Reading one and writing the other gives no atomicity, and lock ordering alone does not make a revoke and an effect atomic. A host effect (an HTTP request to the worker) is outside every database transaction anyway.

**Revocation fence: fence first, then revoke.** Revocation must fail closed across processes, including when the bridge dies part-way. Writing the session database first and the Hub fence second leaves a window in which a separately running worker can still dispatch, and startup reconciliation does not close it. So the order is reversed:

1. **One revoke path.** `revoke_with_fence(kind, id, expected_revision, reason)` in the Hub process is the only way to revoke an installation, session or execution grant, or to narrow a grant. Today there are three routes: `agent_sessions_api.py:200` (installation) and `:208` (session), which call the session repository directly, and the bridge's `grant_revoke` (`session_hub_bridge.py:212`, route `:465`). All three are rerouted through `revoke_with_fence`. The session repository's `revoke_installation` (`agent_sessions.py:328`) and `revoke_session` (`:379`) take a required `fence_id` and refuse without one, so a new route cannot bypass the fence by accident.
2. **Fence first.** In one Hub `BEGIN IMMEDIATE` transaction, insert `hub_principal_fences(id, installation_id, session_id NULL, grant_id NULL, authority_epoch, state='pending', reason, created_at)`. Commit.
3. **Then revoke.** Revoke in the session database, passing `fence_id`. Then mark the fence `state='complete'`.
4. **Owner result.** The API reports success only when the fence is `complete`. A `pending` fence is shown to the owner as "revocation not finished: retry". Retrying is idempotent: it reuses the pending fence and finishes the session-database step.
5. **Workers honour any fence row, pending or complete.** A fence that exists blocks execution whether or not the session-database step happened.

**Interleavings and crashes:**

| Case | Result |
|---|---|
| Bridge or Hub dies before the fence commits | Nothing revoked, nothing fenced. The owner sees failure and retries. Workers continue under the old authority, which is correct. |
| Dies after the fence commits, before the session revoke | Execution is blocked at once. The installation keeps MCP tool access until retry or the periodic reconciler completes the revoke: over-revocation of execution, never under. |
| Dies after the session revoke, before `complete` | Execution blocked by the fence, tools blocked by the session revoke. The reconciler marks it complete. |
| Fence committed before a worker's intent transaction | The intent transaction sees the fence and refuses. No intent, no effect. |
| Intent committed, then fence, then dispatch | Dispatch re-reads the fence immediately before sending and refuses. The intent is recorded as `never_sent`. |
| Intent committed, dispatch re-check passed, then fence, then send | **The residual window.** The one already-checked send proceeds. |
| Worker process with no bridge running at all | Unaffected: it reads fences from the Hub database it already writes. |
| Natural expiry, with no revoke write | Jobs pin the installation and grant `expires_at` in their principal row. Every checkpoint compares the Hub clock to the pinned expiry inside its transaction. An earlier-expiry edit is a narrowing edit and goes through `revoke_with_fence`. |

**The residual window is not yet closed.** It lies between the dispatch re-check and the effect on the staging host. Carrying an epoch in each command does not close it, because an executor that has not learned of the newest fence cannot tell that the command is stale. Deduplication does not close it either: refusing a repeated command says nothing about revocation. Closing it needs enforcement at the executor. Two options:

| | A. Online admission at effect time | B. Short-lived signed lease |
|---|---|---|
| Mechanism | Just before the host effect, the executor calls an authenticated Hub endpoint `admit(intent_id, command_id)`. In one Hub transaction the Hub checks fences, pinned expiry and the principal, then marks the intent `consumed`. | At dispatch the Hub signs `(intent_id, command_id, installation_id, authority_epoch, not_after = now + T)` with an enrolled Hub key. The executor verifies the signature and `not_after` against its own clock, and records `command_id` as used before the effect. |
| Revocation guarantee | No effect whose admission check commits after the fence. The remaining exposure is the effect's own duration after a successful check. | Exposure after a fence is at most T plus the clock skew S between the Hub and the executor. |
| Replay | The intent is single-use in the Hub; a second `admit` is refused. | A durable used-ID store on the executor, written before the effect, plus `not_after`. |
| Disconnect | Hub unreachable: the executor refuses. Fail closed, at an availability cost. | The executor works offline until `not_after`; with no new lease it refuses. |
| Crash | Executor crashes after `admit` and before the effect: the intent is consumed and recovery inspects. It never redoes the step without a new intent. | Executor crashes after recording the ID: the ID stays used. A crash before recording sends no effect. |
| Assumptions | An authenticated executor-to-Hub channel, which does not exist today. | Bounded clock skew S, monitored; the executor refuses when unsynchronised. Key enrollment and rotation. |

Recommendation: A for the staging pilot. It is low volume on the same private network, the availability cost is acceptable, and it gives a revocation point backed by a recorded Hub decision. Use B only if the executor cannot reach the Hub, and state T + S as the exposure. Neither option is claimed until its executor-side tests pass. Until then, the design claims only that no new intent is admitted after a fence.

**Pending-fence reconciliation.** Runs at startup (before queue admission) and on every owner retry or on-demand request. It never runs silently in the background. A fence that cannot be completed stays `pending`, is shown to the owner with its error, and keeps blocking execution.

**Grant edits.** Each execution grant carries an `authority_epoch` that increments only on a narrowing edit: revoke, an earlier expiry, or removing a project, environment or capability. A harmless edit (a label, a later expiry, an added unrelated project) changes `revision` but not `authority_epoch`. Jobs pin the epoch at submit. Admission requires the exact current revision; later checkpoints require the same epoch. A narrowing edit goes through `revoke_with_fence` like a revoke.

## 3. Checkpoints and revocation

| Checkpoint | Check | On failure |
|---|---|---|
| submit (`WorkQueue.submit`, `StagingController.submit`) | live session via the bridge, plus `verify` | reject, nothing queued |
| claim (`_eligible_target`) | `verify` | job not claimable, left `queued` and visible as `principal_revoked` |
| before each effect (`live_claim`, pilot `authorize('command')`, CI `admission_guard`) | `verify` | raise; the existing path holds the job (`_hold`) |
| `create_run` | `verify` plus family check against the principal row | `forbidden` |
| completion (`complete`) | none for releasing the machine; terminal proof only | true terminal outcome recorded with `authority='revoked_after_admission'`; an unknown effect stays `unknown`/held; the revoked principal advances nothing (no task status change, no promotion, no follow-on job) |
| recovery and reconcile (`_bound(recovery=True)`, `recover`, `reconcile`) | **skip** `verify` | inspection and reconciliation stay available |

**Session revocation.** Revoking a session stops new effects for every job that session submitted (fence row with `session_id`). Other sessions of the same active installation are unaffected. Read-only reconciliation and terminal evidence stay available.

**Mid-run revocation.** No further effect is admitted, and an uncertain command is never redispatched. The existing guards (`staging_controller.py:631` "Never retry POST", `queued_pilot.py:404`, and the CI unique intent) already stop redispatch. The machine stays held until terminal proof (`work_queue.py:303-309, 630-653`).

**Required change in `_bound`.** `queued_pilot.py:353` runs `_review` in recovery mode too. If `verify` is placed inside `_review`, revocation would block reconciliation. Place `verify` beside `_review`, and only on the path where `recovery` is false.

## 4. Independence and reviewer provenance

Replace `session_context.family(actor)` inside `_reviewed_task_data_matches` (`repository.py:531`) with an explicit argument, `candidate_principal_family`, read from the job's principal row. `create_run`, the pilot's `_review` and CI's `_review` already call this function, so all three get the change. Replace each `review.actor in AGENTS` with `_review_family(review, sessions_db)`:
- **Legacy reviewer** (`actor in AGENTS`): the family is `review.actor`. If `review.family` is present and differs, reject with `review_provenance_invalid`.
- **Installation reviewer** (`installation:<id>`): look up the family in `session_installations` by id. `review.family` must equal it. The `hub_session_provenance` row for the `task.reviewed` audit sequence must name the same installation and family. Never trust `review.family` from the row on its own.
- **Anything else**: reject.

Apply the same rule to the candidate. A legacy owner's `candidate_family` must equal the owner if present. An installation's `candidate_family` must equal its installation record. Independence requires reviewer family ≠ candidate family and reviewer actor ≠ owner. Legacy rows without family fields evaluate exactly as they do today.

## 5. Compatibility

- Legacy `AGENTS` actors keep their current path. They have no principal row, and `verify` is not called for them.
- Additive migration: two new tables, created if absent. Existing jobs have no principal row and stay legacy.
- Enrollment, approval, the `internal` classification (`agent_installation_scope.py:11`) and tool grants confer no execution. An installation also needs an explicit execution grant for the project and environment. `StagingResourceStore` grants must also name `installation:<id>` explicitly.
- Rollback to an older build fails closed, because `actor in AGENTS` still rejects installation jobs.

## 6. Tests (new `tests/test_installation_execution_admission.py`)

- `test_enrollment_and_tool_grant_alone_cannot_submit`: `work_submit_forbidden`, and no job row.
- `test_scoped_installation_submits_and_persists_principal`: the principal row has the installation id, family, session, grant revision, project and `environment='staging'`.
- `test_revoked_between_submit_and_claim_is_not_claimable`: `claim()` returns None, the job stays `queued`, and status shows `principal_revoked`.
- `test_grant_project_removed_between_submit_and_claim`: same result as above.
- `test_revoked_between_deploy_and_smoke_holds_without_redispatch`: the fake host records exactly one deploy and no smoke, the job is `held`, and the machine `job_id` is still set.
- `test_recovery_after_revocation_reconciles_with_terminal_proof`: `recover()` inspects the original command and releases the machine. No new POST is sent.
- `test_session_revoked_installation_active_blocks_further_effects`: no effect after the revoke.
- `test_session_expired_installation_active_continues`: a job outlives the 15-minute session.
- `test_staging_dispatch_denied_after_revoke_before_post`: `never_sent`, and the client receives no submit.
- `test_ci_admission_guard_denies_after_revoke`: CI stops at the next stage and holds the job.
- `test_worker_without_request_context_validates_from_store`: assert `session_context.current.get() is None` throughout and that admission succeeds.
- `test_same_family_installation_review_rejected`: a codex installation reviewer of a codex installation candidate gets `independent_review_required`.
- `test_legacy_review_family_must_match_actor`: `review={'actor':'claude','family':'codex',…}` makes `_reviewed_task_data_matches`, `create_run`, the pilot's `_review` and CI's `_review` all reject.
- `test_installation_review_family_checked_against_installation_record`: a tampered `review.family` or a missing provenance row is rejected.
- `test_legacy_agents_flow_unchanged`: the existing end-to-end results are identical.
- `test_execution_grant_cannot_name_production`: 422.
- `test_fence_before_intent_sends_nothing`: the fence is committed, then a checkpoint runs; no intent row and no client call.
- `test_fence_between_intent_and_dispatch_recheck_records_never_sent`: no client call, intent marked `never_sent`.
- `test_pending_fence_blocks_execution_before_session_revoke`: crash injected after the fence commits; worker refuses; session tools still work; retry completes the revoke.
- `test_crash_after_session_revoke_before_complete_is_reconciled`: the fence stays `pending`, execution is blocked, and the reconciler marks it `complete`.
- `test_every_revoke_route_goes_through_revoke_with_fence`: enumerate app routes and owner API methods that revoke or narrow; each one writes a fence first. A direct `revoke_session` call without `fence_id` raises.
- `test_worker_honours_fence_with_bridge_process_dead`: the worker runs with no bridge object and refuses on a fence row.
- `test_pinned_expiry_stops_new_effects_without_any_write`.
Acceptance tests for executor enforcement (option A unless noted):
- **Race:** `test_fence_commits_between_dispatch_and_admit_refuses_effect` (the executor calls `admit` after the fence; refused, no effect) and `test_admit_commits_before_fence_effect_runs_and_is_recorded`.
- **Replay:** `test_second_admit_for_same_intent_refused`, `test_replayed_command_after_restart_refused`; for B, `test_expired_or_reused_lease_refused`.
- **Disconnect:** `test_hub_unreachable_executor_refuses_and_reports`; for B, `test_lease_valid_offline_until_not_after_then_refused`.
- **Crash:** `test_executor_crash_after_admit_before_effect_not_redone`, `test_hub_crash_during_admit_transaction_leaves_intent_unconsumed`, `test_pending_fence_reconciled_on_startup_and_retry_and_visible_on_failure`.
- `test_harmless_grant_edit_keeps_job_narrowing_edit_stops_new_effects`: an added unrelated project leaves the epoch unchanged and the job continues; removing the project bumps the epoch, writes a fence and refuses the next effect.
- `test_session_revoke_stops_only_that_sessions_jobs`.
- `test_revoked_completion_records_truthful_outcome_without_advancing_task`: terminal success after revocation is recorded, the task status is unchanged, and no follow-on work is admitted.
- `test_hub_database_is_wal_and_session_database_mode_is_asserted`: pins the journal modes this design relies on.

## 7. Decisions (Codex, 2026-11-03) and open questions

Decided: revoking a session stops new effects for that session's jobs and keeps reconciliation and evidence. Execution grants are explicit and narrow. Admission pins the exact revision, and harmless edits are distinguished from narrowing ones (`authority_epoch`). Cross-database atomicity is not claimed; a Hub-database fence written before the session revoke replaces it, and no bound on the residual window is claimed until the executor enforces one. Terminal outcomes are recorded truthfully without granting revoked authority.

Decided (second round): use a dedicated execution grant, one fail-closed revoke path that writes the fence first, a dispatch-side re-check, and recovery kept available.

Open:
1. Executor enforcement: option A (online admission; recommended) or option B (signed lease)? Either needs executor work and a new channel or key that does not exist today.
2. For option B, the values of T and the accepted skew S.

**Not verified:** the live session database's journal mode (the source sets none); how `app.py` would wire the queue and controller for installations; whether a remote staging cancel API exists; and whether a stored row with a forged legacy `review.family` exists today. No code was run.

## 8. Online admission interface (option A)

Paths are relative to Codex's `agent-bootstrap-download` worktree, uncommitted changes included. The executor package is the top-level `hub_container_executor/`.

### 8.1 Existing protocols

| Item | Today |
|---|---|
| Direction | **The Hub always initiates.** `ContainerPilotClient` uses POST/GET `/v1/container-pilot/commands[/{id}[/reconcile]]` and POST `/current-observation` (`asgi.py:22-23, 58-133`). `StagingClient` uses `/v1/jobs`, `/v1/operations`, … (`staging_client.py:309-358`). Neither the executor nor the staging service contains a Hub client; the only outbound HTTP is to Docker (`daemon_backend.py:21`). **No executor→Hub channel exists.** |
| Hub→executor authentication | A single static bearer, compared with `hmac.compare_digest` (`asgi.py:65-68`; staging `__main__.py:80-84`, plus a socket-peer allowlist). Transport: an HTTPS CA pinned by the caller, or a private UDS (`container_pilot_client.py:143-206`). |
| Executor identity proof | Fresh observations signed with Ed25519 by the root launch supervisor, bound to a Hub nonce, the enrolled `issuer_id`/`public_key`, release and launch pins, and `CLOCK_SKEW_MS=5000` (`current_executor_observation.py:26-48, 107-133, 243-312`; `launch_transport.py:171-237`). Commit 3d558fd ("bounded executor readiness before receipt handshake") only bounds cold start. The Hub consumes one observation per command in `prepare_admission`/`admission_scope` (`startup_reconciliation.py:305-390`, `queued_pilot.py:671-680`, `container_pilot.py:323-372`). |
| Command IDs | Pilot: `uuid5(run_id, 'container_pilot_v1:'+action)` (`container_pilot.py:279-284`). Executor operation: `uuid5(command_id, kind)` (`ledger.py:430`). Staging: `uuid4`, persisted at submission (`staging_controller.py:413`). |
| Hub persistence | `hub_container_pilot_commands` row `state='unknown'` before the POST (`container_pilot.py:298-306, 343-346`). The response is stored afterwards. An existing row is never re-POSTed. Staging persists `phase='dispatching'` before the POST: "Never retry POST" (`staging_controller.py:631-632`). |
| Executor persistence | `ledger.admit` writes `commands`/`runs`/`targets`/`phases` and checks the fence generation (`ledger.py:309-384`). Per effect, `begin_operation` inserts an `operations` row `'intent'` with UNIQUE(command_id, kind) and checks pause and fence (`:413-440`). Then comes the callback, then `complete_operation` with a MAC receipt (`adapter.py:405-435`, `ledger.py:448`). On restart, `running` commands become `held` (`ledger.py:296-308`). Staging: `operations` `'accepted'`, then a thread, then `'running'` (`core.py:784-795, 831-836`). |
| Release evidence | `protocol_verified` and `journal_verified` are read-only echo and state checks (`pilot_release_evidence.py:65-77`, uncommitted). |

### 8.2 Minimal interface

**Granularity.** Admission happens per executor operation, not per command: one `deploy_container` performs state_copy, create and start (`adapter.py:220-232, 457-486`). Admitting per operation bounds the exposure to one operation. The staging service admits once, before `_execute_operation` (`core.py:831`).

**Endpoint.** `POST /v1/executor/admissions` on a new private Hub listener that writes the Hub database (WAL, `repository.py:64`).
- Request: `{intent_ref:{run_id,action}|{job_id}, command_id, operation_id, kind, request_digest, observation}`.
  - `request_digest` is sha256 of the canonical first four fields.
  - `observation` is a launch-supervisor observation whose `nonce == request_digest`.
- Response: `{decision: admitted|refused, reason, admission_id, decided_at_ms, max_effect_age_ms, replayed}`.

**Authentication (gap named).**
- **Executor to Hub.** Reuse the enrolled Ed25519 issuer key: the signed observation binds this exact request to the pinned live launch. The current verifier generates its own nonce (`:254`), so it needs a variant that accepts a supplied nonce. Its other checks are unchanged. Add a **new, distinct** admission bearer, so a stolen Hub→executor token cannot admit.
- **Hub to executor.** The executor needs a pinned-CA TLS client to the Hub, modelled on `for_verified_https`. None exists.
- The Hub has no signing key, so decisions are authenticated only by TLS.

**Hub transaction.** One `BEGIN IMMEDIATE`, in this order:
1. If a row already exists in `hub_execution_admissions(operation_id PK, command_id, intent_ref, request_digest, decision, reason, observation_id UNIQUE, decided_at_ms)`: with an equal digest, return it with `replayed=true`; with a different digest, refuse with `identity_conflict`.
2. Check that `command_id` equals the uuid5 rederived from the stored intent, and that `operation_id = uuid5(command_id, kind)`. Check that the intent is `unknown`, the job is claimed and the `hub_work_machines` fence matches, and nothing is paused.
3. Refuse if any `hub_principal_fences` row exists for the principal. Refuse if the pinned expiry is earlier than Hub now. Refuse if `authority_epoch` has changed.
4. Insert the decision. **Refusals are sticky.**

States:
- Hub: intent `unknown` → admission `admitted` (consumed) or `refused`.
- Executor `operations.status`: `admit_requested` → `admitted` | `refused` | `admission_expired` → `completed` | `terminal_failed`.

**Executor state machine** inside `_effect`, after `begin_operation`:
1. Persist `admit_requested`.
2. Call the Hub. The anchor `t0` is the monotonic time of the first send.
3. Persist the decision.
4. Immediately before the callback, require `monotonic() - t0 ≤ A`, with A = 10 s and no later than the observation expiry. Otherwise persist `admission_expired` and hold.
5. Run the effect.
6. Write the receipt.

**Retries and timeouts.** A retry resends the identical request; the Hub returns the stored answer and never consumes a second time. Each call has a 2 s deadline, all attempts must fit within A, and there is no retry after A. If the Hub is unreachable, the executor holds with `admission_unavailable` and fails closed.

### 8.3 Scenarios and acceptance tests (`tests/test_executor_online_admission.py`)

| Scenario | Outcome | Test and assertion |
|---|---|---|
| a. Stall after `admitted`, then a fence | If the stall comes before the age check, more than A means no effect. If it comes after the check (inside the syscall or effect), it cannot be stopped. **Exposure ≤ A + operation timeout** (`create_ms`/`start_ms`/`total_command_ms`). | `test_admitted_stall_beyond_max_age_refuses_effect`: backend call count 0, op `admission_expired`, command `held`. `test_fence_after_admit_is_audited`: effect runs, evidence `revoked_after_admission`. |
| b. Revocation before admit | Refused. No effect. | `test_fence_before_admit_refuses`: decision `refused`, callback never invoked, run held. |
| c. Revocation after admit | Already-admitted operation proceeds. The next operation is refused. | `test_fence_between_operations_stops_next_op`: create runs, start is refused, the container start count is 0. |
| d1. Crash before admit | `admit_requested` proves no effect. Held. | `test_crash_admit_requested_is_no_effect`: reconcile reports no effect and never calls admit. |
| d2. Crash after `admitted`, before effect | Indistinguishable from mid-effect, so `inspect_required`. Never re-admitted. | `test_crash_after_admitted_holds_without_readmit`: a restart issues 0 admit calls and inspects by operation_id only. |
| d3. Crash after effect, before receipt | The existing `inspect_operation` path. | `test_crash_after_effect_reconciles_by_operation_id`. |
| e. Hub crash mid-admit | Rolled back: no row, and the retry is re-evaluated. Crash after commit: same as f. | `test_hub_crash_before_commit_reevaluates_with_new_fence`: a retry after a fence is refused. |
| f. Response lost | The retry gets the identical stored decision. If the age already exceeds A, there is no effect. | `test_lost_response_retry_same_decision_single_consumption`: one row, `replayed=true`. `test_late_replay_beyond_age_refuses`. `test_digest_mismatch_identity_conflict`. |
| g. Recovery and inspection | GET, `/reconcile` (`adapter.py:526-600`), `inspect` (`container_pilot.py:468`), `recover` (`queued_pilot.py:682`) and staging terminal evidence never call admit. | `test_recovery_paths_work_with_admission_down_and_fenced`: the admit mock raises if called, and every path returns its evidence. |

Also: `test_observation_nonce_must_equal_request_digest`, `test_replayed_observation_id_rejected`, `test_hub_bearer_cannot_call_admission`.

### 8.4 Not verified

- Whether the staging host can reach the Hub, and which Hub process would host the listener.
- Whether the installed supervisor serves observations for arbitrary nonces.
- Executor clock synchronisation.
- `hub_work_principals` and `hub_principal_fences` are only proposals.
- Staging effect steps after `core.py:849`.
- Uncommitted files not named above.
- No code was run.

### 8.x Channel binding and the age bound (revision after Codex review)

**Gap.** The HTTPS observer identity is not bound to the separate command Unix socket peer. A shared bearer plus a nonce-signed observation proves that *a* supervisor-launched executor answered *an* observation request. It does not prove that the process behind the command socket, or the one sending an admission, is that same executor. A relay can also forward between them. No `SO_PEERCRED` check exists in the executor or `container_pilot_client.py` today.

**Binding: a supervisor-attested channel key.**
1. At each launch the executor generates an ephemeral Ed25519 key pair in memory, never written to disk.
2. The root launch supervisor, which already holds the enrolled key and starts the executor, signs an attestation over the following:
   - the executor's public key;
   - its pid and process start time (from `/proc/<pid>/stat`), plus the host `boot_id`;
   - the command socket's `(st_dev, st_ino)` and owner, as created by the supervisor;
   - a supervisor launch counter, monotonic and persisted;
   - the existing release and launch pins.
3. The executor signs with that key every message that matters: command responses and receipts, observation responses and admission requests. The signature covers the request digest and the Hub's nonce or the command ID.
4. The Hub verifies the chain (enrolled supervisor key → attestation → message signature). It accepts only the attestation with the highest launch counter it has seen for that machine.
5. Where the Hub-side client and the socket share a host, the client also checks `SO_PEERCRED` on the command socket: the peer pid must equal the attested pid, with the same start time and socket inode. Where they don't share a host, the signed responses carry the binding on their own.

**What this defeats:**
- **Same-UID socket substitution.** A different process at the socket path has a different pid, start time or inode, and has no attested key, so its responses fail verification.
- **Relay.** A relay can forward messages but cannot alter one or produce a signature. A relayed command reaching a different executor produces a response signed by the wrong key, or none, and is refused. Relaying the real executor's own admission request changes nothing: the decision belongs to that executor's intent.
- **Reconnect or restart.** A new launch means a new key and a higher counter. Messages under the old key are refused once the new attestation has been seen.

It does not defend against root on the staging host, as before.

**Age bound: what can and cannot be asserted.** The executor uses `time.monotonic()` for deadlines (`adapter.py:182,274`, `asgi.py:137-184`). On Linux that is `CLOCK_MONOTONIC`, which **does not advance during system suspend**. Measured that way, an admission's age would undercount a suspend. Requirements:
- **Clock.** Measure the admit-to-effect age with `CLOCK_BOOTTIME` (`time.clock_gettime(time.CLOCK_BOOTTIME)`), which counts suspend. Start it before the admission request is sent, so network delay counts too. The Hub's clock and wall-clock jumps play no part in the executor's check. Wall clock is used only for audit, with the existing `CLOCK_SKEW_MS=5000` noted.
- **Restart.** Admissions live in memory and are tied to the launch counter and `boot_id`. After a restart, any admitted-but-not-effected operation is treated as expired and the command is `held`, as with the existing never-repeat intent. It is never redone without a new intent.
- **Process suspension (SIGSTOP, scheduler stall) between the final age check and the Docker request.** User space cannot prevent this, so the bound cannot be asserted across it. Mitigations: do the check immediately before the Docker call; record the `CLOCK_BOOTTIME` at effect start in the signed receipt; have the Hub flag any receipt whose effect started later than the bound after its admission as `bound_exceeded`, visible to the owner. **The residual stall window is unbounded** unless host enforcement establishes otherwise (see 8.y). The signed receipt only detects a stall after the fact.

**Threat and acceptance tests:**
- `test_same_uid_socket_substitution_refused`: a fake server at the socket path with a different pid and inode; the Hub refuses its responses and records no effect.
- `test_relay_cannot_alter_or_redirect`: a relay that alters the payload fails the signature check; a relay to a second executor fails the attested-key check.
- `test_reconnect_after_executor_restart_requires_new_attestation`: old-key messages are refused after a higher counter; a lower counter is never accepted.
- `test_peer_pid_mismatch_on_shared_host_refused`.
- `test_admission_age_counts_suspend`: a fake `CLOCK_BOOTTIME` advanced past the bound while `CLOCK_MONOTONIC` is frozen; the effect is refused.
- `test_admission_age_includes_network_delay`: admit response delayed past the bound; refused.
- `test_restart_with_admitted_unapplied_operation_holds_without_redo`.
- `test_wall_clock_jump_does_not_change_admission_age`.
- `test_stall_after_final_check_flagged_bound_exceeded_from_receipt`.

**Still unverified:** that the installed supervisor can sign arbitrary attestation fields and nonces; whether the Hub-side client shares a host with the command socket in the pilot layout; staging-host reachability to the Hub.

### 8.y Supervisor-owned broker compared with the attested key (second review)

Codex's review: signed responses do not defeat a *transparent* same-UID relay. Such a relay forwards genuine signed messages unchanged, but it can observe, delay, drop or reorder them, and delay alone widens the stall window. A same-UID process may also read an in-memory key unless the executor is protected from ptrace.

| | Attested ephemeral key (8.x) | Supervisor-owned fixed-route broker |
|---|---|---|
| Path | The Hub connects to a filesystem socket the executor's UID can replace or relay | The Hub connects only to the root supervisor's endpoint. The supervisor reaches the executor over a **socketpair created at launch and inherited by the executor**, with no filesystem path to substitute. |
| Peer identity | Signatures over messages, plus an optional `SO_PEERCRED` check when co-located | The supervisor verifies its own child: a pidfd from the launch, the start time and `boot_id`, the inherited fd. Re-verified on every Hub connection, and bound to the verified launch record. |
| Transparent relay | Not defeated: it can delay, drop or observe | Defeated for the Hub-to-executor path: no same-UID path exists |
| Key custody | Private key in executor memory. Same UID can read it unless `PR_SET_DUMPABLE=0` and Yama ptrace scope are enforced, and that is unverified on the host. | No executor secret needed for routing. Supervisor keys stay root-owned. Signed receipts may stay as evidence, not as the routing defence. |
| PID namespaces | `SO_PEERCRED` pids are translated into the receiver's namespace; a containerised executor reports namespace-local pids and the attestation must match the supervisor's view | The supervisor runs in the host namespace and holds a pidfd, so translation does not matter; it never trusts a pid reported by the executor |
| Disconnect and restart | New key and a higher launch counter; old-key messages refused | Executor exit closes the inherited fd. The broker marks the launch ended and refuses until a new verified launch. A Hub reconnect is re-verified against the current launch. In-flight admissions die with the launch: held, never redone. |
| Cost | Executor and Hub signing code | Supervisor gains a small routing component: more root code to review |

**Recommendation: the broker.** It removes the substitutable path instead of trying to detect it. Signed receipts remain as audit evidence. The attestation design is kept only as a fallback if the supervisor cannot own the route.

**Stall window with host enforcement.** The window can be bounded only if the component that makes the effect enforces the age. If the supervisor broker also mediates the executor's Docker API access, it can check admission age on `CLOCK_BOOTTIME` at the moment it forwards each mutating Docker request, and refuse expired ones. The residual then becomes a stall inside the root broker itself, which the design treats as trusted but still states. Without broker-mediated Docker access, the window remains **unbounded** and is only detected after the fact.

**Added threat tests:**
- `test_broker_refuses_hub_connection_when_launch_ended`
- `test_broker_reverifies_pidfd_starttime_per_connection`
- `test_no_filesystem_command_socket_exists_for_executor_uid`
- `test_transparent_relay_has_no_path_in_broker_layout`
- `test_executor_exit_closes_route_and_inflight_admissions_hold`
- `test_pid_namespace_executor_identity_from_pidfd_not_reported_pid`
- `test_broker_refuses_expired_admission_at_docker_forward` (only if Docker mediation is adopted)

**Unverified:** whether the supervisor can hold a pidfd (kernel 5.3 or later, plus a Python API); whether it can mediate the Docker API without changing the rootless engine setup; and the host's ptrace and dumpable policy.

### 8.z Launch-plumbing feasibility (checked in source, not on the host)

**How the executor starts.** The root supervisor creates and starts the executor as a Docker container through the private engine API (`hub_container_executor/launch_supervisor.py:871` create, `:880` start). The container has `NetworkMode: none`, `CapDrop: ALL`, a read-only root filesystem and `no-new-privileges` (`:769-781`). It receives host paths only through bind mounts, including `issuer_socket` and `engine_socket` (`:51-52`, `:669-673`, `:776`).

**An inherited socketpair is not feasible with this launch.** The container's process is spawned by the engine's runtime (containerd-shim and runc), not forked from the supervisor. The Docker create/start API has no way to pass a file descriptor, so a socketpair created by the supervisor never reaches the executor. Getting real inheritance would mean launching the executor as a direct supervisor child instead of a container, which gives up the existing hardened container profile and its inspection checks (`_inspect`, `:785-841`).

**A feasible alternative that reuses existing code: the executor dials the supervisor.** The supervisor already has the pattern needed:
- It binds a root-owned socket through a pinned parent directory, `chown root:<consumer gid>`, mode `0660`, listen backlog 1 (`:745-763`). The socket is bind-mounted into the container as `issuer_socket`.
- On every accept it requires `SO_PEERCRED == (consumer.pid, consumer_uid, consumer_gid)`, where `consumer.pid` is the container init pid read from engine inspect (`:882`, `:1012-1013`).
- `_validate_live` re-checks against a held **pidfd** (`:184-188`, `:891-897`): the process is still alive, the init pid still equals the engine's `State.Pid`, it is in a distinct pid namespace, and its kernel profile and uid map match.

Proposed route: the executor makes one outbound connection to a second supervisor-owned socket, `command_route`, mounted the same way. The supervisor verifies that connection with the same `SO_PEERCRED`, pidfd and inspect checks, then holds it as the only path for Hub commands. The Hub talks only to the supervisor's root-owned host-side endpoint. The executor listens on nothing.

**What this gives:**
- **No substitutable path.** The executor owns no listening socket. A same-UID process on the host can connect to the mounted `command_route` path, but its pid is not the pinned container init pid, so it is refused. Any other process inside the container has a different pid and is refused too.
- **PID namespaces.** `SO_PEERCRED` reports the pid in the supervisor's (host) namespace, and the existing code already compares it with the engine's host-side `State.Pid` and holds a pidfd. It never trusts a pid the executor reports.
- **Rootless engine user namespace.** The existing check compares the uid as seen from the host (`consumer_uid`) and checks `uid_map` (`:904-905`). The same check carries over.
- **Disconnect and restart.** If the executor exits or drops the route, the pidfd becomes readable and the route closes, so the supervisor marks the launch ended and refuses Hub commands. A reconnect is accepted only from the same live init pid of the same launch. A new launch needs the full launch verification again. In-flight admissions are held, never redone.
- **Relay.** A transparent relay would have to be the container init process itself, which is the executor.

**Docker mediation, for host enforcement of the stall window.** Today `engine_socket` is bind-mounted straight from the private engine socket (`:673`). Bounding the stall would mean changing that mount's source to a supervisor proxy socket that forwards only allow-listed mutating calls and checks admission age on `CLOCK_BOOTTIME` at forward time. The mechanism is feasible because it is the same bind-mount pattern, but it changes the executor's engine access path. It needs its own review and must not be done as part of this change.

**Not verified:**
- Whether the executor image's init process can perform the outbound connect itself. The connecting process must be the init pid, or the supervisor must accept a documented single child with a pidfd check.
- Behaviour under the real rootless engine on the staging host.
- Whether a backlog of 1 with a single held connection suits the command rate.

**Tests:**
- `test_route_accepts_only_container_init_pid`, covering a host same-UID connector and a second in-container process.
- `test_route_closed_when_init_exits_and_launch_marked_ended`.
- `test_reconnect_from_same_live_init_accepted_other_refused`.
- `test_executor_listens_on_no_socket`.
- `test_hub_commands_refused_without_verified_route`.

### 8.w No-delegation and coordinator ingress (third review)

**Problem.** `SO_PEERCRED` records the credentials of the process that called `connect()`. It does not identify who later reads or writes the socket. A connected socket can reach another process in two ways: inheritance across `fork` (and across `exec` unless `FD_CLOEXEC` is set), or `SCM_RIGHTS` over another Unix socket. The verified route therefore holds only while no other process can come to hold that descriptor.

**Constraints found in source:**
- Executor code starts no processes. `hub_container_executor/*.py`, excluding the supervisor, has no `subprocess`, `fork`, `multiprocessing` or `exec`. That is a code-review fact, not an enforcement.
- The container is `NetworkMode: none`, `ReadonlyRootfs`, `CapDrop: ALL` and `no-new-privileges`, with tmpfs `/tmp` mounted `noexec` (`launch_supervisor.py:769-781`). `PidsLimit` is 16 to 128 (`:254`), and threads count against it, so it cannot be set to 1.
- `SecurityOpt` is only `no-new-privileges:true` (`launch_supervisor.py:770`, `daemon_backend.py:956,1210`), so the engine's default seccomp profile applies. That profile allows `fork`, `clone` and `execve`.
- The only Unix sockets reachable from inside the container are the supervisor's (`issuer_socket` and the proposed `command_route`, both with a root peer) and `engine_socket`, whose peer is dockerd. Docker's HTTP API ignores passed descriptors.

**Enforcement proposal: no new processes in the executor container.**
1. A custom seccomp profile passed via `SecurityOpt: seccomp=<pinned profile>`. It denies `fork`, `vfork`, `execve` and `execveat`, denies `clone` unless `CLONE_THREAD` is set (seccomp can inspect clone flags), and returns `ENOSYS` for `clone3` so that libc falls back to `clone`. Threads still work. No second process can exist in the container.
2. With no second process, an `SCM_RIGHTS` transfer has no attacker-controlled recipient. The remaining peers are root-owned (the supervisor) or ignore descriptors (dockerd).
3. The executor opens the route `SOCK_CLOEXEC`, as defence in depth.
4. **A check the supervisor can enforce now, independently of seccomp.** The supervisor is root in the host namespace, so it can list `/proc/<pid>/fd/*` for every pid in the container's pid namespace (pids found through `/proc/<init>/task` and the cgroup's `cgroup.procs`). It confirms that the route socket's inode (`socket:[ino]`) appears only in the init process's descriptor table. It does this at accept and before forwarding each Hub command. A hit anywhere else is treated as launch compromise: the route closes, the launch is held, and nothing is redone.
5. **Scope of these claims (after Codex review).** A seccomp profile that blocks new processes, together with periodic descriptor scans, does **not** prove that the descriptor cannot reach an existing peer the executor can already talk to. Scans also race short-lived use. These measures narrow and detect; they are not confinement proof. The attacker boundary has to be validated against the real profile on the real engine before any confinement claim is made. Root on the host is out of scope.

The launch inspection must verify the seccomp profile. It should compare `HostConfig.SecurityOpt` and `/proc/<pid>/status` `Seccomp: 2` with the pinned profile digest, alongside the existing `_kernel_profile` check (`:904`). The daemon backend's equality check at `daemon_backend.py:1210` would need the same update.

**Coordinator ingress.** The Hub, or coordinator, reaches the supervisor's host-side endpoint, and that hop needs its own identity:
- **Transport.** mTLS with a pinned Hub client certificate, enrolled in the supervisor's existing root-owned enrollment file alongside the supervisor key. A private Unix socket is an option only if the Hub process runs on the staging host, in which case `SO_PEERCRED` plus pidfd apply the same way.
- **Per-command authorisation.** The Hub signs `(command_id, operation, digest, Hub nonce, admission decision id)` with an enrolled Hub key. The supervisor journals each `command_id` once and refuses replays. **Gap:** the Hub has no signing key today. Its enrollment, custody and rotation need their own design and Beau's approval, since this is a new credential.
- **Delegation on the Hub side.** The Hub process is trusted at host level. The same no-delegation reasoning applies only if the supervisor endpoint is a local Unix socket.

**Tests:**
- `test_fork_child_holding_route_fd_detected_by_fd_scan`: init forks and the child keeps the descriptor. The scan finds the inode outside init, and the launch is held.
- `test_scm_rights_to_sibling_detected_by_fd_scan`: an in-container socketpair passes the route descriptor to a sibling and is detected.
- `test_seccomp_profile_denies_fork_exec_and_nonthread_clone`: `os.fork`, `subprocess` and `clone3` all fail; `threading` works.
- `test_route_opened_cloexec`.
- `test_launch_refused_without_pinned_seccomp_profile`.
- `test_coordinator_without_enrolled_client_cert_refused`.
- `test_replayed_hub_command_signature_refused`.
- `test_fd_scan_runs_before_every_forwarded_command`.

**Not verified:** whether the rootless engine applies a custom seccomp profile as written; Python's `clone3` fallback behaviour on the host's glibc; whether the executor needs any subprocess at runtime (source says no); and the Hub's deployment location relative to the staging host.
