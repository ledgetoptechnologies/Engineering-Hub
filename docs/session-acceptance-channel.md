# Manual staging session acceptance channel — source only

`session_acceptance.py` provides an explicitly attached browser channel and one
process-local request. Nothing attaches it to the app, launches a worker, changes
Compose, provisions credentials or changes Cloudflare policy. Live use still needs
reviewed trusted composition and Beau's acceptance of the operator enrollment,
protected storage permissions, actual inventory and writer-exclusion procedure.

## Existing authentication, separately enrolled operator

The attachment uses existing `require_human`: the real verified Cloudflare-bound
session (or configured GitHub owner session), exact mutation Origin and CSRF.
Agent bearer credentials are rejected. Global human admission is **not** operator
authority: trusted composition supplies one exact independently enrolled operator
actor. Never choose the first visitor, an email from request JSON, all allowed
Cloudflare emails, or a client such as Thomas as the staging operator by default.
No additional login, credential, access policy or always-on service is introduced.

Trusted composition creates a `SessionAcceptanceBroker` with the exact existing
`StartupReconciliation`, its `PilotOwner`, target, immutable manifest, independently
enrolled executor identity, retained `OperatorReconciliation`, bounded evidence
text and a mandatory synchronous durable audit callback. The report digest must
be SHA256 of those exact UTF-8 evidence bytes. This binds what is displayed, but
does not prove report statements true. Evidence must not contain credentials.
The operator still checks present host inventory, unknown effects and exclusion
of previous/competing writers. A current identity observer is not a fact factory.

The finite request lifetime (1–3600 seconds, default 600) cannot renew the gate's
separate deadline. New process/reboot, owner, executor generation or report requires
a new challenge/request and independent acceptance. No persisted accepted bit or
answered decision is reloaded. A protected decision answer remains historical
human intent with `execution_authorization: false`, not a session opening.

## Narrow endpoint and audit semantics

Explicit attachment provides GET/POST
`/api/hub/v1/staging/session-reconciliation`. Only the enrolled operator can read
the bounded retained evidence or submit the opaque request ID, expected revision
and literal `accept`/`decline`. JSON cannot supply owner, report, executor, digest,
challenge, operator identity or attestation booleans. Duplicate/extra keys,
malformed data and bodies above 2048 bytes are rejected without reflecting input.
The body is capped before copying each chunk and has a two-second asynchronous
read deadline; an authenticated slow sender receives a sanitized 408 rather than
holding the listener/owner drain indefinitely. Success and broker-error responses
carry `Cache-Control: no-store`, including attachment outside the normal Hub app.
The route is not a generic MCP tool, report uploader or unauthenticated flag.

A confirmed durable intent acknowledgement precedes gate acceptance. A distinct
result record follows. The audit callback must commit synchronously and return
exactly `True`; that return is a trusted integration contract, not independently
verified durability. Async callbacks/acknowledgements are not adopted. Failed,
uncertain or reentrant audit handling closes the gate and does not retry an
uncertain write. A failed gate opening after confirmed intent attempts its first
result record as `failed`. If result audit itself fails, an earlier `accepted`
attempt must not be presented as a durable success; preserve evidence and report
the session closed/uncertain.

There is **no transaction** across an in-memory gate and durable audit storage.
The broker mutex serializes answers, not direct gate permits. After gate opening
and before result audit completes, another thread could accept work. Later audit
failure revokes future admission but cannot undo that effect. The explicit async
route performs its synchronous acceptance/audit sequence without an intervening
await: reviewed integration must run admissions on the same owned Hub event loop
or provide a real handover barrier for every other admission thread. Bounded
identity/audit operations are mandatory; elapsed-time checks cannot interrupt
blocking callbacks. Do not claim atomicity, cancellation or rollback of accepted
operations. Retain/drain those operations under the existing owner lifecycle.

Session acceptance is never a task execution grant. Independent task review,
immutable artifact/manifest constraints, current target identity, pause policy and
per-phase gate checks remain mandatory. Reject/replay does not reopen a session;
stale IDs/revisions do not revoke an otherwise accepted session. No task or command
is submitted by this component.

## Validation limits

Tests use real Linux ownership and existing FastAPI/Cloudflare signature/session/
CSRF code with synthetic identities, reports and audit acknowledgements. They
cover nonoperator allowed humans, bearer/CSRF rejection, narrow request parsing,
concurrent replay, expiry/identity loss and the audit/opening failure window.
They do not prove real operator enrollment, audit durability, NAS ACLs, host
inventory/exclusive writers, production integration, protected restore or reboot.
