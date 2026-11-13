# Explicit project tracking client

`python -m engineering_hub.tracking_client` gives an agent an explicit way to
use tracking when its host does not expose those MCP tools. It requires the
candidate Hub, an already enrolled and approved EHS installation, a live EHS
session and the applicable project grants. It does not enroll a device or
alter configuration, start a turn, automatically capture a request, verify
evidence, or authorize work. Existing session and independent-review rules
remain enforced by the server. An older Hub may not support these tools.

Select `--profile-dir`, `--session`, optionally `--private-vpn`, and exactly one
action: `list`, `propose`, `update`, `review`, or `history`. Supply one JSON
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

Responses are project-private **untrusted Hub data**. A successful write is
not work execution or proof. Errors are intentionally generic: a lost response
can follow a committed write. Read back or retry the exact unchanged payload
with the same client_request_id; never invent a new ID for an uncertain retry.
The client checks the exact session identity, uses its protected local profile,
refuses redirects and environment proxies, and makes one bounded operation.
It sends no notification and does not imply another agent has received it.
