# Task progress (coordination only)

The session-scoped progress tools store a bounded checklist and status separate
from the task. Progress has its own revision; writing it does not alter the task,
its evidence, reviews, queue reservation, or execution authority. A computed
100% is not a claim or verification of completion. A blank checklist has no
percentage. ETA is a bounded estimate from a stable server-observed checklist
completion rate; it is null without such evidence or after resets, unchecks,
clock anomalies, or an extended interval. `agent_eta_guess` is a separate,
explicitly labelled guess in seconds.

An authenticated Hub owner can read one task's progress via the read-only
`GET /api/hub/v1/task-progress/{task_id}` endpoint. No progress dashboard is
wired yet; this endpoint does not expose progress to non-owner browser sessions.

`hub_task_progress_get` requires `progress.read` and the underlying task's
`task.read` or `tracking.read` capability and project grant. Updating requires
`progress.write`, the same underlying read grant, and either the exact existing
task owner session or a current role lease with a matching coordination claim,
plus current `role.lease` and `role.claim` grants. The expected progress revision
is independent of task revision. Supply an idempotency key for a write: an
identical retry returns the same current revision, while a later change returns
`progress_changed` and requires an authorized readback. Role or task state can
also change before a retry, in which case authority is rechecked first.

Progress changes are audited but not broadcast to the unscoped generic event
bus. Consumers must explicitly reread; no automatic wake is promised. No
installation, runtime activation, or staging access follows from this feature.
