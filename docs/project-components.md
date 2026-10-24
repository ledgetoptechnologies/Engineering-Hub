# Project component catalog

A component describes a product, application, service or package within a
project. It is separate from tasks and subprojects. The catalog uses the existing
Hub ledger and starts empty; it never seeds a fixed product list.

Humans create, edit and retire components through authenticated routes using the
existing CSRF checks. Agents can read them through HTTP or the read-only
`hub_component_list` and `hub_component_get` tools. Version1 does not expose an
agent mutation or proposal endpoint. An agent can discuss an idea using the
existing project notes or planning workflow for a human to consider.

Each record contains `id` (Hub-issued UUID), `project`, immutable project-scoped
`slug`, `name`, `description`, `kind`, `status`, optional `repository_url`,
`package_url` and `legacy_project_id`, plus `created_by`, `created_at`,
`updated_at` and `revision`. Kinds are `web_app`, `desktop_app`, `mobile_app`,
`service`, `package`, `library`, `tool` and `other`. Statuses are `active`,
`planned`, `paused` and `retired`. Names are limited to100 characters and
descriptions to4096. HTTPS links are validated and displayed as inert links;
the Hub does not fetch them or use them as runtime/proxy destinations.

Examples a human might add are 3D Viewer, a thumbnail generator and a Windows
application under Operations; TreeDAR under a processing project; and Community,
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
  Retirement preserves the row and history. A human may restore a retired item
  using an explicit status edit; this changes metadata only.

Mutations require an active registered project and compare-and-swap revision.
Archived-project catalog history remains readable. Legacy links may refer to an
archived registered project. Each real change appends audit/outbox records in
the same guarded transaction. A retry with the current revision and unchanged
fields produces no audit churn. There is no delete endpoint.

Creating a component grants no project access, staging recipe, executor,
deployment, public route, repository access or preview capability. Existing
authorization, worker admission and project ownership rules remain independent.
