# Experimental cross-project shared requests

Status: source-only, separately **default off**, not a deployed or complete
cross-project coordination system. The explicit programmatic setting
`cross_requests_enabled=True` requires the experimental seat foundation; no
environment or deployment switch, credentials, grants or live migration is
provided. This records requests and shared conversation only. It sends no
message, wakes no session, creates no target task, starts no work and conveys
no execution authority. EHS seat identity is not proof of a native chat or
listener attachment.

An owner-authenticated CSRF-protected route policy explicitly shares a target
project/topic with a source project. It identifies up to 16 exact target seats
and one target triager. The required `share_with_source_project` boolean is an
explicit disclosure choice: false limits source-side request access to the
originating seat; true permits other current seats in that source project with
their own `cross.request.read` or `cross.request.message` grants. No target
private data or general target-project grant is implied. Participant selection,
triager and the sharing choice apply from the **current** policy to existing
records. Revoking the route or removing a participant denies subsequent access
while retaining data; re-enabling current policy can restore access. Archived
source or target projects deny agent access. Owners can inspect retained
history, including on revoked routes, using the separate owner-only API.

Each operation requires an authenticated current seat, matching generation,
project grant and its separate capability. Source may submit, permitted target
participants may read and append shared conversation, and only the selected
current triager may change `open` to `accepted` or `declined`, or `accepted` to
`done`. The human owner may perform the same transitions or explicitly expire
an open/accepted request; these are audited CAS actions with idempotency keys. `accepted`
is triage, `done` is a report, and neither starts work nor proves completion.
A generated conversation ID identifies a bounded append-only shared thread;
no legacy bus correlation or message body grants access. Mutation receipts
contain no current state or private notes. Existing accepted idempotent retries
are checked before rate or capacity limits, after current authorization.

Records are retained and never automatically evicted. Deliberate caps are 4,096
requests globally, 512 per source and target project, 256 per route and 128 per
installation; messages are capped at 8,192 globally, 1,024 per source/target
project, 512 per route, 256 per installation and 256 per request. New writes
are limited to 20 requests or 60 messages per installation per 60 seconds.
These bounds can block new work and require a separately designed retention
policy before live use. The local finite clock rejects observed backwards
movement across committed writes; it is not an independent time authority and
cannot prove absence of forward clock jumps or database rollback. Do not claim
rate, idempotency or retention guarantees across a restored ledger.

This is not the full approved workflow: authoritative native topic/chat
attachment, scoped event-driven fanout, installation mailbox move/recovery,
exact-session delivery and wake verification, owner UI and external receiver
fencing are still absent. A local successful synthetic test is not installed
staging acceptance. See `cross-project-seat-authority-design.md` for unresolved
contracts and release gates.
