# Source-intent handoff for an operator

`GET /api/hub/v1/staging/source-intent-handoffs` is an owner-admin, read-only
inventory of locally recorded `deposit_unknown` source intents. It is available
only through the existing configured modular-staging controller and a live human
session; the human session is checked again at the Hub transaction boundary.
It performs no worker request, task creation, message, policy change, source
retry, job submission, reconciliation, or reservation release.

Results are bounded by `limit` (1–100) and ordered by submission ID. The cursor
advances over all submissions, including unrelated phases. An empty page can
therefore still have `next_cursor`; continue until it is null. Each valid item
contains the request, source and reserved command IDs as locally recorded, historical task
review identity, source/Git binding and reviewed-profile digest. It contains no
source archive, command argv, credential, host path, or worker response. Records
with inconsistent IDs, malformed binding or mismatched canonical fingerprint are
reported only as `unreadable`; no untrusted row content is returned.

The handoff shows original local intent, not a verified current review or proof
that a source POST did or did not occur. The reserved command ID is not evidence
that a job was sent. This implementation has no independently admitted worker
ledger identity, host writer fence or rollback authority, and explicitly reports
those limits. The source and reserved command IDs are observed from the local
journal only: the request fingerprint does not cover those generated IDs, so a
valid replacement UUID cannot be detected by that fingerprint. The response
labels this `source_identity_assurance: journal_only`. An operator must preserve the original intent and obtain a fresh,
independently reviewed host/worker admission and original receiver evidence
before any separately authorized recovery. A missing worker row, an old TLS
certificate, the task's historical review, or this handoff never authorizes
replay, adoption, cleanup, dispatch or release. The canonical fingerprint is an
internal consistency check, not a signature or protection from a coherent
database rewrite.
