# Role dispatch foundation (coordination only)

An active `orchestrator` role holder with explicit dispatch and role grants can
record a project-scoped request and dispatch an existing task to an existing
role. The target must claim coordination for the task and hold its current role
lease before reporting. These records never start work, replace executable task
ownership, grant deployment authority, or prove completion. A reported `done`
is not an independent review or verification. Duplicate open dispatches for
the same task and role are denied transactionally, including concurrent calls.
If the underlying task is withdrawn or verified, historical dispatch records
remain readable but further reports and stale checkpoints are denied. No
pending operation or reservation is released by that denial.

Use `hub_orchestration_request_list` to recover request IDs after a later turn.
Requests and dispatches are paged in UUID order, not time order: restart from
the beginning to discover new entries which may sort before a saved cursor.
Role/task/project/decision grants are rechecked; a cursor is not authorization.
The owner has separate authenticated read-only request and history endpoints.

Reports are retained as immutable, bounded records. Read them with
`hub_dispatch_reports`, ordered by increasing dispatch revision. Ask reports
atomically create one ordinary unanswered decision, and `resume` requires the
exact linked decision to have been answered plus the current target lease and
claim. `hub_dispatch_decisions` pages all prior decisions in increasing report
revision; each read additionally requires `decision.read`. After reaching a
page whose `next_after_revision` is null, retain the last returned revision for
any later incremental refresh. A request ID or decision is never approval to
execute. All writes use per-session admission and CAS/idempotency receipts;
when a record has changed after an earlier successful request, the retry returns
`dispatch_changed` and requires an authorized readback.

Individual records and responses are bounded, not total retention. Existing
database, storage and enrollment limits still apply; operators must manage
capacity without silently discarding reports or decisions.

`hub_dispatch_checkpoint` is an explicit, manual advisory report-timeout check.
It does not yet consider attributable progress or target lease expiry and is
not an automatic stale detector. Stale does not mean failed, stopped, safe to
reassign, or complete. Nothing releases a reservation or replays a command.
Dispatch records, reports and created decisions are audited but intentionally
do not enter the generic unscoped notification bus. No automatic wake, polling,
or delivery guarantee is provided. Consumers must explicitly reread.

This is an incomplete backend foundation. Project creation, automatic
request capture, orchestration runtime, UI wiring, attributable progress-aware
staleness, addressed delivery and real staging acceptance remain separate work.
