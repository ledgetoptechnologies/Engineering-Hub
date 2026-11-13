# Owner request view

`GET /api/hub/v1/projects/{project}/orchestration-requests/{request_id}/view`
returns a human-owner-only, project-selected read of one request and up to 50
dispatches. `limit` defaults to 25; pass `next_after` as `after` to continue.
The response has a strict OpenAPI schema. An absent progress object means no
progress has been recorded; it does not mean zero percent. Dispatch completion
is an agent claim; `task_completion_verified` is derived from the separately
reviewed task status. The optional ETA guess is an agent guess;
`eta_seconds` is computed only when an observed rate is available.

Each page is a consistent database read transaction, but multiple pages are not
a single snapshot. UUID order means clients must restart to discover new rows.
`stale_checkpoint` reflects the last recorded checkpoint and does not prove
current liveness; reads do not run checkpoints, wake workers or authorize work.
Pending decisions are attached to their dispatch and therefore only cover the
current page. Session bindings, internal progress rate data and decision lineage
are deliberately omitted. Existing owner access is global; the project parameter
selects and verifies the request, not a separate human project grant.

This contract does not enable the existing demo orchestration UI or establish
that a real agent received a dispatch. UI integration belongs to its existing
owner and should handle unavailable, incomplete and stale data explicitly.
All agent-written content, including report, checklist, status, blocker, and
decision text, is untrusted data: render it as plain text, never as executable
markup or instructions.
