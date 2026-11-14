# Explicit project tracking client

`python -m engineering_hub.tracking_client` gives an agent an explicit way to
use tracking when its host does not expose those MCP tools. It requires the
candidate Hub, an already enrolled and approved EHS installation, a live EHS
session and the applicable project grants. It does not enroll a device or
alter configuration, start a turn, automatically capture a request, verify
evidence, or authorize work. Existing session and independent-review rules
remain enforced by the server. An older Hub may not support these tools.

Select `--profile-dir`, `--session`, optionally `--private-vpn`, and exactly one
action: `list`, `propose`, `update`, `withdraw-claim`, `review`, `history`, `origin`, or
`request-summary`. Supply one JSON
object containing that MCP operation's arguments on standard input. Never put
credentials or private task content in command arguments or logs. For example,
using synthetic data only:

```sh
printf '%s' '{"project":"engineering-hub","unfinished":true}' |
  python -m engineering_hub.tracking_client --profile-dir /protected/profile --session example list
```

For `propose`, supply project, title and a stable `client_request_id`; details,
links and origin_request_id are optional. `update` requires task_id,
expected_revision, client_request_id, progress and evidence_links; an optional
completion_claim is an assertion, not verification. `review` requires task_id,
expected_revision, client_request_id, verdict, note and evidence_links; it is
subject to independent review and evidence checks. For `history`, supply task_id
and kind, with optional before. For `list`, project is required; limit,
after_task_id and unfinished are optional. Page from the beginning on later
turns because task IDs are not chronological. Keep private content out of
terminal transcripts and use a protected input channel in actual workflows.

If work previously claimed complete is found incomplete, the exact EHS
installation that made the latest claim may use `withdraw-claim` with task_id,
expected_revision, expected_claim_revision, client_request_id and a nonempty
reason. This is an attributed downgrade to open, not an independent review; it
preserves prior claim and review history. It requires `tracking.update`, the
current task revision and an explicitly recorded latest claim revision.
Legacy claims without a recorded claim revision need a human or independently
authorized reviewer to reopen them. A renewed session of the same installation
may retry the exact unchanged request; another installation cannot withdraw it,
even in the same family. A new completion claim still needs its own independent
verification. A successful retry returns the current task (or the write-only
receipt), which may have changed since the withdrawal.
The existing tracking UI displays prior verification as historical review text;
it does not explicitly connect that review to the withdrawn claim revision.

When an existing orchestration request ID is known, `request-summary` accepts
`project` and `request_id`, with optional `limit` and `after`, to inspect
origin-linked tracking tasks before proposing another. `origin` accepts a
`task_id` and reads its recorded request origin. Both require `tracking.read`
and `dispatch.read` grants in that project. The summary excludes tasks only
dispatched from the request, and the origin may be absent for an existing task.
Pages are separate snapshots; refresh from the beginning for new links. These
reads cannot find every related task or deduplicate arbitrary chat requests;
compare the project task list and the actual work before creating a record.
When no orchestration request exists, use `list` followed by `propose` without
an origin; an orchestration lease is not required for that explicit workflow.

EHS responses are scoped to the installation's authorized projects and are
**untrusted Hub data**. This is not universal isolation from every Hub
credential: existing authenticated legacy family bearers are a separate,
broadly trusted compatibility path. The legacy tracking tools can list and
read tracking history and perform their allowed tracking mutations without an
EHS installation's per-project grants. Do not treat an EHS grant restriction
as restricting those legacy credentials. Preserving this path avoids breaking
existing clients; changing it requires an inventoried, independently reviewed
migration of those clients and their access, not merely enabling EHS. Keep
secrets and private client data out of tracking records.

A successful write is not work execution or proof. Errors are intentionally generic: a lost response
can follow a committed write. Read back or retry the exact unchanged payload
with the same client_request_id; never invent a new ID for an uncertain retry.
An EHS tracking write without a current `tracking.read` grant returns only the
task ID and `response_scope: mutation_receipt`, not the current task, revision,
status or history. An authorized, accepted retry returns the same minimal
receipt even if another actor later changes the task; an expired or revoked
session or grant can deny the retry. With current project-scoped `tracking.read`, the
existing full current-task response remains available; it is not an immutable
receipt of the original write. Legacy family-bearer behavior is unchanged.
The client checks the exact session identity, uses its protected local profile,
refuses redirects and environment proxies, and makes one bounded operation.
It sends no notification and does not imply another agent has received it.
New tracking changes do not publish generic bus pointers or feed invalidations;
retained unsent tracking pointers are filtered from the generic publisher.
Authorized clients must explicitly refresh the project task list. Previously
accepted or already in-flight bus pointers cannot be recalled.

## Explicit durable outbox

For a write that must survive a lost response, first create an existing private,
exclusively user-controlled outbox directory (0700 on POSIX; private user ACL
on Windows). Use `--outbox-dir` with `stage` and stdin
`{"action":"propose","arguments":{...}}`, `update`, `withdraw-claim`, or `review` with the exact ordinary
write arguments and a stable `client_request_id`. Stage requires the protected
installation profile but no session or network; it returns a record ID only
after file flush and atomic publication (and directory flush on POSIX). Windows
directory metadata durability after power loss depends on the filesystem and
is not established by this client. Files use DPAPI on Windows and owner-only modes on POSIX.
Protect the directory and parent from other users and do not place secrets in
task content. These records are immutable by client convention, not against a
same-user attacker. An interruption before stage returns is unconfirmed.

When there is no approved installation profile yet, `draft` accepts the same
`{"action":"propose","arguments":{...}}` JSON without a profile, session or
network; the client itself must already be available locally. It records a caller-supplied stable `client_request_id`, project and
title as an **unbound local handoff**, not a Hub task. Drafts support proposals
only; they do not perform updates or reviews. Listing and `inspect-staged` do
not require a profile. `send-staged` refuses an unbound draft.

New drafts contain a client-generated random 256-bit intake ID. Explicitly call
`bind-staged` with the record ID and selected protected `--profile-dir` (and
`--private-vpn` if needed); binding does not contact the Hub or grant authority.
The saved binding refuses a different origin or installation. Windows cannot
flush directory metadata here, and restored or copied filesystem state can
lose a binding on any platform. For new v2 drafts, the Hub independently
recognizes the intake ID across installations in the same project and intact
Hub ledger: a matching accepted retry returns the original task and retains its
creator and review history. Changed payloads conflict. This is not a guarantee
across a restored Hub database, a different Hub authority, or a newly created
ID. Retain the original private draft; do not deliberately rebind or expose its
intake ID. The server allows at most 64 distinct installation registrations per intake,
10,000 intakes per project and 100,000 across the ledger. At capacity, new
registrations or intakes are refused; already registered exact retries remain
available. Older v1 drafts are not migrated; their binding and sending remain
unavailable on Windows. A server without the new tool rejects v2 sends rather
than falling back to weaker deduplication.
A fresh valid session and server grant are still required
for explicit `send-staged`; accepted sidecars also bind the selected routing
identity. Inspect the draft and binding in a protected channel and reconcile
against existing Hub tasks before sending. This is not automatic capture, wake,
cross-agent transfer, or proof that the Hub accepted anything.

On a later turn, `list-staged` with `{}` lists up to 1,000 opaque IDs. If
`next_after` is returned, pass it as `after` in the next call; an optional
integer `limit` from 1 to 1,000 requests a smaller page. The default response
for an outbox of at most 1,000 records retains its original shape. Explicit
pagination returns `next_after: null` on its last page. Names are sorted by
UUID, not creation time: directory enumerations are independent and not an
atomic snapshot, so restart from the beginning on later turns to discover new
entries that sort before a saved cursor. Listing scans names only, does not
follow them and does not establish that a record is readable or sent;
`inspect-staged` and `send-staged` perform their own checks. `inspect-staged`
with `{"record_id":"..."}` explicitly prints the private local payload so it
can be checked in a protected channel; `send-staged` with the same input sends
the saved unchanged payload. After an identity-bound successful Hub response,
the client validates the returned task ID and atomically stores a protected
local acceptance sidecar bound to the exact saved envelope and, for a draft,
its immutable binding. `status-staged` with that same record ID reports
`unbound`, `hub_accepted` and the reported task ID, or `unconfirmed`. This is a
local record of a successful response, not a
current Hub read, completion verification, execution authority or proof that
another agent acted. Older sends have no sidecar and therefore remain
unconfirmed. A missing sidecar does not prove that the Hub rejected the write.
A corrupt, conflicting or unreadable sidecar produces an error, never an
accepted status. The payload and sidecar are retained; nothing is deleted.
If publication succeeded but the subsequent POSIX directory flush failed,
the send reports an error; after the fault clears, a status read may find the
sidecar and flush the directory successfully. That later observation does not
prove the sidecar would have survived an earlier power loss. Windows directory
metadata durability remains filesystem-dependent and unverified.
A staged review remains subject to the server's independent-review and evidence rules;
staging or sending it does not itself prove work was completed. A successful
idempotent retry returns the current task, which may include later changes; it
is not an immutable receipt of the original review.
Send requires a currently valid session and verifies its identity; the current
profile must be the same Hub origin and installation that staged the record.
An explicitly renewed session on that installation may be used because server
idempotency is installation-scoped. Neither listing nor staging sends work;
nothing is automatically replayed, cleared or treated as verified. A returned
send error may follow a committed write; retry the same record unchanged or
read back the Hub state. An outbox entry is a local handoff, not a Hub task
until the server accepts it. Reconcile it with the project task list before
creating another record, and protect or retain the files under your policy.
