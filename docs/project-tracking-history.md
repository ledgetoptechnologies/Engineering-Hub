# Project tracking history

Tracking records are informational and do not authorize execution. A task's
current response retains a bounded recent window of updates and reviews. When
either window is full or the task grows large, older records move atomically to
immutable, bounded history segments; the latest update and review remain on the
task. `history_segments` signals that earlier pages exist. Use
`hub_tracking_task_history` with `task_id`, `kind` (`updates` or `reviews`), and
the returned `next_before` to read older pages. The project view offers the same
history. Each read requires current project access; a cursor is not a grant.

For an existing oversized legacy task, an authorized agent can explicitly call
`hub_tracking_task_compact` with the task ID, current revision, and an
idempotency key. This CAS-protected, audited operation preserves status and
history, including the original revision of a legacy pending claim; it creates
no progress or execution authorization. Do not compact a shared ledger without
its normal owner and write admission. An untouched oversized legacy row can
still fail offline pilot enrollment's cell-size limit: qualify and perform any
conversion on an authorized ledger before retrying enrollment.

Segment and response sizes are bounded; total retained data is not infinite.
Disk exhaustion and the enrollment database/row/total-size limits still require
operator capacity management. Never discard or overwrite history to make a
limit appear satisfied. This mechanism does not prove a task complete; a claim
still needs its linked evidence and a separate authorized reviewer.
