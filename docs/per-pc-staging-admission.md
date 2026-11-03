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

**Revocation fence (Hub-database tombstone).** Make revocation take effect inside the Hub database, the one the worker already writes effect intents to:
1. When the owner revokes an installation, session or execution grant, or narrows a grant, the bridge writes the session-database change and then, in the same bridge critical section, a Hub-database row `hub_principal_fences(installation_id, session_id NULL, grant_id NULL, authority_epoch, revoked_at, reason)`. The owner API reports success only after both commits.
2. Startup reconciliation in the Hub process copies any session-database revocation missing a fence row before the queue admits work. This covers a crash between the two commits.
3. Every effect checkpoint checks fences inside the same Hub `BEGIN IMMEDIATE` transaction that records the effect intent (pilot command intent, CI attempt intent, staging submission). Fence row present or epoch mismatch: no intent and no effect.
4. **Bounded race, stated plainly:** an intent that committed before the fence row may still be sent once, because sending happens after the commit. At most one such effect per job. It is recorded as `admitted_before_revocation`, never retried, and no later intent is admitted. Everything else is refused.

The worker also reads the session database directly (`InstallationAuthority.verify`) as defence in depth, but the fence is the authority for ordering.

**Grant edits.** Each execution grant carries an `authority_epoch` that increments only on a narrowing edit: revoke, an earlier expiry, or removing a project, environment or capability. A harmless edit (a label, a later expiry, an added unrelated project) changes `revision` but not `authority_epoch`. Jobs pin the epoch at submit. Admission requires the exact current revision; later checkpoints require the same epoch. A narrowing edit writes a fence row like a revoke.

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
- `test_fence_before_intent_sends_nothing`: fence committed, then a checkpoint; no intent row and no client call.
- `test_intent_before_fence_sends_once_and_records_admitted_before_revocation`: commit an intent, write the fence, let dispatch run; exactly one client call, the outcome tagged `admitted_before_revocation`, and the next step refused.
- `test_crash_between_session_revoke_and_fence_is_reconciled_before_admission`: session database revoked, no fence row, restart; startup writes the fence before `claim()` can return the job.
- `test_harmless_grant_edit_keeps_job_narrowing_edit_stops_new_effects`: an added unrelated project leaves the epoch unchanged and the job continues; removing the project bumps the epoch, writes a fence and refuses the next effect.
- `test_session_revoke_stops_only_that_sessions_jobs`.
- `test_revoked_completion_records_truthful_outcome_without_advancing_task`: terminal success after revocation is recorded, the task status is unchanged, and no follow-on work is admitted.
- `test_hub_database_is_wal_and_session_database_mode_is_asserted`: pins the journal modes this design relies on.

## 7. Decisions (Codex, 2026-11-03) and open questions

Decided: revoking a session stops new effects for that session's jobs and keeps reconciliation and evidence. Execution grants are explicit and narrow. Admission pins the exact revision, and harmless edits are distinguished from narrowing ones (`authority_epoch`). Cross-database atomicity is not claimed; the Hub-database fence plus a stated one-effect bound replaces it. Terminal outcomes are recorded truthfully without granting revoked authority.

Open:
1. Is the one-effect-per-job bound in §2 acceptable, or must dispatch itself re-check the fence after the intent commit (smaller window, but still not zero)?
2. Should the fence write and the session revoke share a single code path in the bridge, so no owner route can revoke without fencing?
3. Should a new `session_hub_execution_grants` table be used, or should `session_hub_grants` be extended?

**Not verified:** the live session database's journal mode (the source sets none); how `app.py` would wire the queue and controller for installations; whether a remote staging cancel API exists; and whether a stored row with a forged legacy `review.family` exists today. No code was run.
