# Project component catalog

A component describes a product, application, service or package within a
project. It is separate from tasks and subprojects. The catalog uses the existing
Hub ledger and starts empty; it never seeds a fixed product list.

Authenticated humans and the existing Codex, Claude and Hermes agents can
create, edit, retire and restore components inside existing active projects.
HTTP uses the existing authenticated actor; human mutations still require the
session's Origin and CSRF checks. MCP tools derive the actor from the agent
bearer. Neither interface accepts a caller-selected actor or provenance.
Project creation, editing, archival and deletion remain human-only; component
metadata supplies no project-management or execution authority.

Each record contains `id` (Hub-issued UUID), `project`, immutable project-scoped
`slug`, `name`, `description`, `kind`, `status`, optional `repository_url`,
`package_url` and `legacy_project_id`, plus `created_by`, `created_at`,
`updated_at` and `revision`. Kinds are `web_app`, `desktop_app`, `mobile_app`,
`service`, `package`, `library`, `tool` and `other`. Statuses are `active`,
`planned`, `paused` and `retired`. Names are limited to100 characters and
descriptions to4096. HTTPS links are validated and displayed as inert links;
the Hub does not fetch them or use them as runtime/proxy destinations.

Examples a human or agent might add are 3D Viewer, a thumbnail generator and a
Windows application under Operations; TreeDAR under a processing project; and Community,
Enterprise and iOS applications under Project Alpha. These are examples, not
startup configuration. An optional legacy-project link can point to the existing
Viewer or TreeDAR project to retain context; it does not move or delete notes,
tasks, files or history.

## HTTP contract

- `GET /api/hub/v1/projects/{project}/components` returns `{items,next_after}`.
  Filters: `include_retired=false`, optional `kind`/`status`, optional UUID
  `after`, and `limit` from1 to100. Retired rows require `include_retired=true`.
- `GET /api/hub/v1/components/{uuid}` returns the record, including retired rows.
- `POST /api/hub/v1/projects/{project}/components` accepts `slug`, `name`,
  `client_request_id` and optional metadata fields. Kind defaults to`other` and
  status to`planned`. Actor plus request key is idempotent; conflicting reuse
  returns409. Slugs are unique within their project and remain reserved after
  retirement.
- `PATCH /api/hub/v1/components/{uuid}` accepts `expected_revision` and the
  metadata fields to change. Project, UUID, slug and provenance are immutable.
- `POST /api/hub/v1/components/{uuid}/retire` accepts `expected_revision`.
  Retirement preserves the row and history. A human or authenticated agent may
  restore a retired item using an explicit status edit; this changes metadata only.

## MCP contract

The existing `hub_component_list` and `hub_component_get` tools remain available.
Three mutation tools accept a single closed, typed `payload` object:

- `hub_component_create`: `project` plus the HTTP create fields, including
  `client_request_id`. Retries are scoped to the authenticated actor.
- `hub_component_update`: `component_id`, `expected_revision` and the metadata
  fields to change. Set `status` explicitly to restore a retired component.
- `hub_component_retire`: `component_id` and `expected_revision`.

Payloads reject unknown fields, including `actor`, `identity`, project moves,
slug changes and provenance edits. MCP mutations use the same repository
validation, guarded transactions, revisions, idempotency and audit as HTTP.
Malformed or duplicate bearer headers cannot select a human-cookie fallback.

Mutations require an active registered project and compare-and-swap revision.
Archived-project catalog history remains readable. Legacy links may refer to an
archived registered project. Each real change appends audit/outbox records in
the same guarded transaction. A retry with the current revision and unchanged
fields produces no audit churn. There is no delete endpoint.

Creating a component grants no project access, staging recipe, executor,
deployment, public route, repository access or preview capability. Existing
authorization, worker admission and project ownership rules remain independent.
