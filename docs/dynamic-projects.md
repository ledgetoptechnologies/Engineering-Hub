# Human-owned projects

Projects are durable metadata in the existing Hub ledger, not a second client
directory, repository inventory, execution allowlist or permission grant.

The first registry initialization seeds the five existing project IDs once.
Names may then change without changing IDs or moving historical records. No
automatic rename/reparent of the Viewer is performed. A client-notes project
may omit a repository entirely.

## Interfaces

- Authenticated humans create with `POST /api/hub/v1/projects`, supplying a
  stable `client_request_id`, `project_id` and `name`.
- Humans edit with `PATCH /api/hub/v1/projects/{id}` and archive with
  `POST /api/hub/v1/projects/{id}/archive`. Both require `expected_revision`.
- Mutations require the existing human identity, exact Origin and CSRF check.
  An agent bearer, even combined with a human cookie, does not gain this role.
- Agents have only `hub_project_list` and `hub_project_get` for project metadata.
  There is no project mutation MCP tool or HTTP DELETE route.
- IDs are immutable lowercase letters/digits/hyphens, at most64 characters,
  beginning with a letter or digit. Descriptions, optional HTTPS repository
  links, lead agent, advisory status and parent project are data only.
- Parent references must exist, must be active and cannot introduce cycles.

Creating metadata never registers a build/deploy action or enables a worker.
Existing execution-policy validation remains separate. Metadata-only projects
report `environment=notes_only`, `executor_enabled=false`, `health=unknown`.

## Archive and retained knowledge

Archive retains the project and its tasks, decisions, notes, plans and history.
It refuses outstanding runs/jobs that require completion/reconciliation. New
task/decision and registered-project knowledge writes are rejected after
archive; historical reads remain available.

The composed application reuses one exact knowledge writer for original bus
MCP, Hub planning MCP, browser notes and browser planning. A trusted local
admission context is configured once before the first write. It cannot be a
request parameter, be rebound, or be added after a write has begun.

Lock order is Hub writer admission → knowledge instance lock → bus SQLite
writer. The Hub writer remains held through the bus commit/read-back, ordering
note admission against archive even with separate registry connections.
This is **not a cross-database transaction**: a note commit can survive a later
Hub guard-exit/ownership failure. After an ambiguous response, read back the
existing document/version before attempting another CAS update.

Only exact registered project rows participate. Historical namespaces such as
`agent-bus` and `vaultbridge` retain their existing behavior and do not silently
become projects. Invalid registry state fails closed for the composed writer.

This admission boundary covers the application's wired remote knowledge entry
points. It does not sandbox an administrator, arbitrary same-UID code, a new
independent repository object, or direct SQLite access. Bus files and feedback
have separate contracts; do not describe archive as a universal filesystem or
all-store write prohibition.

## External collaborators are a separate gate

A project record is not project-scoped authorization. Do not provision Thomas
or another external human/agent on the basis of metadata alone. Membership,
per-operation capabilities, scoped lists/search/activity/attachments/events,
revocation and cross-project denial tests must be implemented and verified
before any such access is enabled. Internal shared agent tokens must not be
reused for external assistants.
