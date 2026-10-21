# Shared notes and tracking

Engineering Hub uses the existing message bus knowledge store for durable notes.
The browser and agents must read and write the same project/path records, not
separate dashboard copies. Notes are reference material, not execution authority.

## Where information belongs

| Information | Record |
| --- | --- |
| Current plan, findings, setup instructions, handoffs | Knowledge note |
| Assigned work and candidate/review state | Hub task |
| A question or approval needed from Beau | Hub decision |
| Actual processing/execution progress and outcome | Run/job record |
| Agent-to-agent notification | Addressed message linking those records |

Do not treat message acknowledgment, an agent appearing online, or a checkbox in
a note as proof that a task or execution completed. Do not duplicate authoritative
task/decision state in notes; link its identifier and explain context instead.

## Suggested note organization

Use the actual project identifier, such as `engineering-hub` or `treedar`.
For cross-project coordination, use the `engineering-hub` project explicitly.
Paths are identifiers inside the knowledge store, not host filesystem paths.

- `overview.md`: current scope and links to important records.
- `plans/<topic>.md`: agreed approach, alternatives, open questions.
- `handoffs/<topic>.md`: last verified state, next step, owner, blockers.
- `runbooks/<operation>.md`: prerequisites, checks, recovery instructions.
- `findings/<topic>.md`: evidence, limitations, relevant commit/run IDs.

Each working note should state what is verified versus proposed, its owner or
contact, relevant task/decision IDs, and the next action. Use explicit dates and
source references. Keep credentials, private signing keys, tokens and sensitive
client datasets out of notes. These are shared engineering records, not a vault.

## Agent interface

The existing MCP tools are `knowledge_list`, `knowledge_search`, `knowledge_get`
and `knowledge_put`. A new note uses `expected_version=0`; editing requires the
version from a fresh read. On conflict, read the current document and reconcile
the change rather than retrying a blind overwrite. Search returns pointers;
fetch the complete note before relying on its contents.

The current store retains the latest document and its revision/author/timestamps,
not a full historical copy of every edit. Do not advertise it as a revision archive
or backup. Preserve historical evidence in distinct dated notes when needed and
include the durable bus database in operator-managed backups.

## Human interface contract

The authenticated dashboard notes API is backed by that same bus store:

- `GET /api/hub/v1/notes?project=...` lists note metadata.
- Optional `query`, `path_prefix` and bounded `limit` filter the list.
- `GET /api/hub/v1/notes/document?project=...&path=...` reads a document.
- `POST /api/hub/v1/notes` saves `{project,path,title,content,expected_version}`.

The list limit is 1–99; `query` and `path_prefix` are alternatives. A `truncated`
result means to narrow the search or prefix. The store accepts up to 64KiB of
UTF-8 content, but browser gateways cap the entire JSON request at 64KiB, so
browser edits must leave room for metadata and JSON escaping.

Browser writes require the existing authenticated human session, exact Origin and
CSRF protection. Agent identity is derived from authentication, not submitted
author fields. Missing shared-store wiring must return unavailable, never fall
back to a new empty notes database. UI content must render as text or sanitized
Markdown, never executable raw HTML. Notes never grant permissions or approve
jobs. Dashboard source/API availability does not mean a deployed container has
been updated.
