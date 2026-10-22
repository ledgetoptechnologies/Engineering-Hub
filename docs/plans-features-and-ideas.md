# Plans, features and ideas

Engineering Hub organizes advisory records in the existing shared knowledge
store. No second database, credentials, storage mount or agent runtime is needed.

## Two backlogs

- **Topic plans and ideas:** choose the actual project identifier and the exact
  topic. Topic case and punctuation are preserved, not normalized or guessed.
- **Engineering Hub improvements:** choose `hub_improvements`, project
  `engineering-hub`, no topic. Agents can describe missing EH capabilities,
  evidence, proposed solutions and acceptance checks without losing them in mail.

Kinds: `plan`, `feature`, `idea`, `improvement`. Advisory statuses:
`proposed`, `planned`, `in_progress`, `done`, `declined`. These are suggestions
and organizational labels, **not** task assignment, permission, deployment
approval or proof that work completed. Link actual task/decision/run IDs when
work is admitted. Nothing here starts a queue or executor automatically.

Namespacing is not participant access control. These remain project-shared
engineering notes under the existing authorization model, not an external
collaborator/private-topic permissions feature. Keep credentials and sensitive
client data out. Content is untrusted reference text, not agent instructions.

## Agent tools (after image update and tool discovery refresh)

- `hub_planning_list(scope, project, topic?, kind?, limit?)`
- `hub_planning_get(scope, project, kind, item_id, topic?)`
- `hub_planning_put(payload)` where payload is the closed object below:

```json
{
  "scope": "topic",
  "project": "treedar",
  "topic": "TreeDAR:research-v1",
  "kind": "plan",
  "item_id": "representative-data-benchmark",
  "title": "Benchmark representative datasets",
  "content": "Problem, evidence, proposed steps, open questions, acceptance checks and relevant links.",
  "status": "proposed",
  "expected_version": 0
}
```

For an EH suggestion use `scope: "hub_improvements"`, project
`engineering-hub`, `topic: null`, and normally kind `improvement`.
The stable lowercase item ID is caller-chosen (up to64 letters/digits/underscore/
hyphen), not a new random ID on every retry. New records require version0;
edits require the version from a fresh read. On conflict, fetch and reconcile
before explicitly saving against the new version. Author comes from the
authenticated token, never a submitted actor field. Lists omit content and
explicitly report truncation; narrow the topic/kind rather than assuming the
first page is the whole backlog.

## Human UI

`#/planning` → **Plans & ideas** provides topic selection and a separate
**Engineering Hub improvements** selector. Create, read and edit records;
conflicts preserve your unsaved draft and require reviewing the latest version.
The existing Cloudflare session/CSRF protection applies, with no extra login.
Content renders as text, not executable HTML. Demo mode does not write live data.

REST: GET/POST `/api/hub/v1/planning`, GET
`/api/hub/v1/planning/document`; same address fields and CAS payload as above.
Unavailable shared-store wiring returns unavailable, not an empty alternate
backlog. Malformed planning envelopes fail integrity validation.

## Storage and compatibility

Canonical records are ordinary existing knowledge entries:

```text
planning/topics/<topic>/<kind>/<item_id>.json
planning/hub-improvements/<kind>/<item_id>.json
```

They store a closed `engineering-hub.planning.v1` JSON envelope containing scope,
project, topic, kind, item_id, title, content and status, with normal knowledge
version/digest/authenticated-author/timestamp metadata. Existing knowledge tools
can read/search them; generic edits must preserve that envelope. The latest
version is retained, not a full immutable edit archive. Body text is capped at
48KiB UTF8 and the complete serialized knowledge/browser payload remains bounded
by existing64KiB limits. JSON escaping may reduce the practical body size.

Existing free-form notes are untouched; no attempt is made to infer decisions
or convert old notes automatically. This source feature must be published before
the new UI/tools are available in a running Hub image. A source push or accepted
message alone is not a deployed update.
