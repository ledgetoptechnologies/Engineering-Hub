# Tracking task request origin

An EHS session with `tracking.propose` and `dispatch.read` may pass
`origin_request_id` to `hub_tracking_task_propose`. The request must exist in
the same active, granted project. The request origin is immutable, stored in
the same transaction as the inert task, and bound to the client request ID for
safe retries. Omitting it preserves the existing proposal behavior.

Use `hub_tracking_task_origin(task_id)` to read provenance. The session needs
`tracking.read` and `dispatch.read` in the task's active project. A successful
lookup returns null for a legacy task without an origin. A task outside the
session's scope is indistinguishable from a missing task. Ordinary tracking
records and their events deliberately omit the private request linkage.
`hub_orchestration_request_tasks(project, request_id)` provides the reverse
lookup, including tasks not dispatched yet, using the same two grants. Results
are ordered by task ID and paginated by `next_after`; restart from the beginning
to discover newly added tasks. The owner-only browser endpoint
`GET /api/hub/v1/projects/{project}/orchestration-requests/{request_id}/tracking-tasks`
provides the same bounded IDs. The existing request view is unchanged; the
dashboard must explicitly integrate this endpoint.

The field records provenance; it neither creates a dispatch nor authorizes
execution. It does not automatically capture a chat request, wake an agent,
or verify task completion.
