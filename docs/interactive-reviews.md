# Interactive reviews, version1

An interactive review is a small declarative choice/text form in Engineering
Hub. An authorized agent supplies questions; a human can answer and complete
them on a phone or desktop. The Hub executes no supplied code, starts no service
and accepts no asset URL, host path or arbitrary proxy target.

`InteractiveReviewRepository(repo, components, authorize=..., asset_authorize=...)`
uses the existing Hub ledger. Its authorization callback takes
`(actor, project, action)` and must return exactly`True`. Actions are
`review_read`, `review_create` and `review_answer`; default is deny. Creating and
answering also require read permission because their responses contain review
state. The HTTP layer authenticates the current request separately; a stored
actor name or catalog entry is not permission.

Every interaction rechecks the active project, active component, exact component
revision and current grant. A changed or paused/retired component blocks the old
review rather than applying old answers to a new version. Stored reviews and
answer events remain intact; there is no destructive cleanup endpoint. List
results omit interactions whose bindings are no longer usable. A separate
history-access workflow is outside this first version.

## Wire contract

A review contains `id`, `project`, `component_id`, `component_revision`, `topic`,
`dataset_digest` (`sha256:` plus64 lowercase hexadecimal characters), immutable
`schema: "choice-text.v1"`, `title`, `items`, `state` (`open` or `completed`),
`answers`, `created_by`, timestamps and `revision`.

Each item has a stable string `id`, `title` (100 characters), `prompt` (4096
characters), optional `choices: [{id,label}]` (2–20 options), and optional
`asset_id` UUID. Items without choices accept short text; choices accept only an
option ID. A manifest has1–1000 items and at most1 MiB of normalized JSON.

Asset UUIDs are reserved references. An explicit asset validator must return
exactly`True` for `(actor, project, component_id, asset_id)` on every interaction;
without it, asset references are rejected. This module serves no asset bytes.

- `POST /api/hub/v1/interactive-reviews`: agent only, explicit create/read
  grants. Payload is the immutable binding fields above, title/items and
  `client_request_id`; the Hub issues the review UUID.
- `GET /api/hub/v1/interactive-reviews?project=...`: paginated summaries with
  optional `component_id`, `state`, `after` and `limit` from1 to100. Response is
  `{items,next_after}`. Summaries include `item_count` and `answered_count`,
  excluding full questions and answers.
- `GET /api/hub/v1/interactive-reviews/{id}`: full authorized detail/context.
- `GET /api/hub/v1/interactive-reviews/{id}/state`: summary plus current answers.
- `POST /api/hub/v1/interactive-reviews/{id}/answers`: human only, current
  session/CSRF and answer/read grants. Payload is `{expected_revision,
  client_request_id, answers:[{item_id,value}], complete:false}`. A batch has at
  most100 answers, each at most2000 characters. Phone clients should save small
  batches; they must not submit all1000 items at once. Completion requires every
  item answered. Completed reviews reject further edits.

Each answer records `value`, authenticated human`by` and ISO timestamp`at`.
The complete normalized saved review, including questions, answers and binding
metadata, is limited to4 MiB of serialized UTF-8 JSON. A batch that would exceed
this limit returns413 `review_state_too_large` and preserves all prior answers
and revision. Unicode JSON encoding counts toward this limit; the per-answer
character limit does not promise that1000 maximum-length answers will fit.
Offline enrollment admits this reviewed4 MiB TEXT bound only for
`hub_interactive_reviews.data`; other cells retain their1 MiB bound and the
overall logical snapshot remains limited to512 MiB.
Same-ledger answer events retain previous submissions and attribution. Revision
CAS prevents accidental overwrites. Actor plus request key supplies idempotency;
an identical retry returns current state without reapplying, while changed key
reuse conflicts. Revocation/component checks apply to retries too.

Global activity notifications contain only an inert review pointer (ID, project,
component ID, revision, state and timestamp). They never contain titles,
questions, dataset digests or answers. Full content exists only in the dedicated
review tables and requires the review grant when read through these APIs.

Optional MCP registration exposes agent creation plus authorized get/list tools.
Human answers remain in the session-protected Hub UI. This module starts no
worker, grants no staging capability and changes no public routing or policies.
