# Cross-project requests and seat authority: implementation design

Status: **unaccepted design; not implemented, not enabled, and not authority to
change a live Hub, credentials, routing or host policy.** This is a proposed
replacement for the unresolved portions of `designs/seat-registry-contract-v0.md`
r3, not a modification or approval of Claude's note. The first release must be
explicitly default off, coexist with legacy installations, and prove its stated
boundaries before it is enabled. This document does not claim that messages
wake an agent or that a fence already exists.

The approved cross-project request requirements are in Hub knowledge
`planning/hub-improvements/improvement/cross-project-requests.json` v5
SHA-256 `7b0d7456f76b913d84f56c4fab985547af3d085c0f7eaea2d80b247070ddb81c`.
The Project > Topic > Seat model is in `planning/hub-improvements/improvement/agent-seats-and-routing.json`
v4 SHA-256 `2ac1f2b2e15aa282e4f08200d83fb60de5330240d9b27f9ec3606844caf30966`.
Those notes describe goals, not authorization to change a live routing or
credential policy. Existing `RoleRepository` is coordination-only, the current
session bridge grants a project-by-capability cross product, and `TopicBinding`
in `reachability.py` is advisory, not an authoritative registry.

## Authority and data model

* Keep authoritative topic, seat, seat-session binding, lease, handoff,
  generation, takeover decision, request, discussion, audit, receiver fence
  state and outbox intents in the **same Hub SQLite database**. A topic belongs
  to exactly one project. A stable random seat ID identifies one topic and role;
  its generation increases on every ownership transfer or vacancy. A current
  binding is `(seat_id, generation, installation_id, session_id, lease_expiry)`.
  Never return the private handle or bearer in logs or list APIs.
* Human-owned route policy explicitly marks the target project/topic as
  requestable to a specified source scope, and separately names eligible
  target triage roles. Existing access to a project never implies permission to
  discover another project, route into its topic, or accept for it. Missing,
  private, archived and unauthorized target IDs produce the same unavailable
  response. A finite owner-controlled destination directory discloses only
  deliberately shared destinations; validate its scope before pagination.
* A server-owned session classification determines which routes require a
  current seat. Once a session is classified seat-bound for those routes, a
  caller cannot drop a header/handle and fall back to legacy grants. Derive the
  identity from the authenticated session, not request-body topic or role.
  Define ambiguous/multiple-seat sessions explicitly: either prohibit them or
  require an explicit seat selector checked against the server binding; absence
  and ambiguity fail closed. Legacy/unseated sessions continue existing
  behavior only under their separately granted scope; rollout must not grant a
  seat-only capability to an unclassified principal. Agent EHS project grants
  and seat hold permission remain independent and both are required.
* Seat APIs and the new feature receive distinct versioned capabilities,
  default off. Enabling schema or registering tools must not grant access or
  silently migrate the legacy family bus. The existing target-wide
  `hub_target_fences` counter is not a seat fence and cannot be reused as if it
  proved seat exclusion.

## Transactions, admission and transfer

1. For seat-authenticated mutations, authenticate and acquire the sessions DB
   writer, then acquire the Hub DB writer (`session -> Hub -> knowledge` lock
   order). Recheck session, installation and current grant under the session
   guard. Inside the Hub write transaction check the exact session binding,
   seat generation, lease, topic/project, project active state and action
   capability, then perform the mutation, provenance and audit together.
   Recheck expiry and current grant before commit while holding both guards.
   An expired/revoked session or grant cannot commit a guarded mutation.
2. Human transfer/approval follows the same lock order even though the human
   answer is not an EHS operation. Validate the unanswered protected decision,
   its exact seat, prior generation, proposed assignee and expected revision in
   that transaction; atomically answer, change seat state/generation and write
   audit plus notification/effect intents. Competing transfers have one winner;
   stale answers do not apply to a later generation. A prior handle is fenced
   at the Hub commit, not by a preflight read. Never hold a DB writer across
   network calls or await an external receiver while holding either writer.
3. A live/quiet seat requires owner-approved transfer or holder release; a
   stale takeover and release follow the accepted policy and require a new
   handoff revision where applicable. Claim returns the latest handoff, open
   scoped work and unresolved effect intents; it is not permission to execute.
   Session revocation immediately prevents the next guarded request. A durable
   revocation intent updates visible vacancy; failure to apply the intent must
   not allow a revoked session to write.
4. Initial schema upgrade is additive and idempotent, with a feature-off
   migration test. Feature-off deployment must not create holders, default
   routes, inbox messages, extra grants, service listeners or external effects.
   On an unsupported/partially migrated schema, fail closed, preserve data and
   require an operator-reviewed upgrade or rollback. Preserve legacy read/write
   behavior and do not copy family credentials into seat identities.

## Receiver fences and external effects

A receiver storing only the highest generation it has *seen* is insufficient:
an old effect arriving after a Hub transfer but before the receiver learns the
new generation can still be accepted. Choose a **separate typed seat fence**,
not the existing target run generation. Every controlled receiver must key its
fence by `(project, topic, role/seat)` and check the exact generation atomically
with accepting the effect. An authority-bound receiver registration pins the
receiver identity, endpoint and fence protocol. Never send a token to an
unverified endpoint.

A transfer that affects a receiver enters `transfer_pending` and blocks new
seat effects. Record the old generation and each affected controlled receiver
in the Hub transaction. Outside DB locks, drive an idempotent, authenticated
receiver barrier that atomically advances the minimum generation and reports
all accepted/in-flight old-generation effects. The Hub records the exact
receiver/barrier receipt and reconciles unknown work; a missing, mismatched,
restarted, rolled-back or unreachable receiver remains held. Only after every
required receiver has durable matching barrier evidence and all old effects
are drained or explicitly reconciled may the Hub atomically activate the new
generation. On restart, resume inspection from durable intents; never blindly
resend a possibly accepted effect. A receiver restore, re-enrollment or lost
fence state requires a fresh barrier; origin or URL equality is not proof of
receiver identity or anti-rollback. A barrier cannot undo an effect already
accepted: preserve it as in-flight/unknown until resolved. A fresh barrier
works only after a restore is known. Simultaneous or silent rollback of Hub and
receiver cannot be detected from those restored stores alone: an independent
monotonic authority, or an operator-controlled restore detection procedure
that holds all affected admission until re-enrollment and reconciliation, is
a separate prerequisite for any anti-rollback guarantee. Without it the system
cannot prove that rollback did not occur.

For GitHub, human actions and other receivers outside Hub control, record an
immutable in-flight intent **before** any permitted effect, including seat,
generation, exact target and idempotency identity where supported. Transfer
must surface unresolved intents to the new holder and owner. Reconcile by
observed outcome and evidence; block conflicting replacement effects on the
same logical object until those intents are reconciled and never automatically
replay an unknown effect.
Do not assert all effects are fenced until every enabled route, including
queue claim variants, executor submit, direct legacy routes and owner actions,
has a proven receiver contract or is explicitly disabled for seat-bound use.

## Knowledge database boundary

Planning/knowledge is a separate SQLite database today. It is not made atomic
by moving seats into the Hub DB. The first seat-authority release must deny
seat-bound planning writes until one of these independently reviewed designs
is implemented: (a) move the authoritative planning write into the same Hub
transaction with a migration preserving identities/revisions, or (b) a
transactional intent and generation-aware knowledge receiver that atomically
checks an already advanced fence with its own write and safely handles
recovery. A reverse read before commit is not sufficient. Read-only knowledge
access may continue under existing grants, and legacy/unseated behavior remains
separate. Do not silently remove this restriction by accepting a body field.

## Request, discussion and projections

A request is an inert record with a server-generated opaque ID, schema version,
source project/topic/seat/session attribution, approved target route and
project, bounded title/ask/reason/links, timestamps, owner role, revision and
status. The server derives authenticated attribution; it never trusts supplied
session, family, topic, role or ownership labels. Creation uses a bounded
actor-scoped idempotency key and full normalized fingerprint in the same
transaction as the record and audit. A repeated exact accepted request returns
an authorized projection; a different payload conflicts. Enforce finite
per-source/target/global counts and rate limits; accepted retries remain
available at capacity. Requests confer no execution authority.

* `open -> accepted | declined | expired`; `accepted -> done | expired`.
  `accepted` means triage/discussion only, not permission or proof that work
  started. `done` is the target's attributed report, not independent completion
  verification. Define expiry as an explicit audited transition with a trusted
  clock and documented actor; do not infer it from a UI timer.
* Only a current authorized target owner/role holder or the human owner may
  transition with expected revision; a source cannot accept, decline, finish,
  create a target task, or gain target repository permissions. A source may
  read only its explicitly shared ask and shared status, not target-private
  content. Target-side private notes, participant identities and decisions are
  separate scoped projections. Mutation-only capability receives a minimal
  receipt, not the current row. Grant revocation and project archival are
  rechecked at commit and before response; inaccessible and missing IDs have
  the same public error.
* Each request owns one immutable opaque discussion ID and bounded, attributed
  append-only messages. A shared message is visible only to authorized members
  of the two designated scopes; no generic project membership, legacy
  `correlation_id` or message body establishes membership. A target-private
  note must never enter the shared projection. Validate and render text safely;
  no HTML/code execution, secret injection or task execution from messages.
  Cursor-based list/get/thread APIs filter authorized records **before**
  limit/cursor calculation; audit views and the owner UI honor the same scope.
* The backend supplies explicit source/target lists and status transitions for
  Claude-owned UI: global Conversations, each project's inbound/outbound list,
  scoped Activity, and owner escalation/Needs you. UI labels unverified
  delivery and reported done honestly; no work or wake is triggered by viewing
  or accepting a request. Separate UI implementation/review remains owned by
  the established UI owner; this document does not edit their work.

## Cross-database notifications and delivery

Writing a request or seat transition atomically writes a bounded Hub-side
notification intent with an immutable operation ID, target seat/generation and
project/topic scope. The sessions DB is a different transaction: an explicitly
owned applier reads an intent and writes a uniquely keyed mailbox envelope plus
its applied receipt. Crash before/after either commit is recovered by the same
idempotency identity, not by guessing delivery. Target resolution must verify
the authoritative current seat generation; a vacant topic stays `unrouted`,
not routed to a different session or the Engineering Hub. A transfer records
an intent to move only eligible unclaimed messages and invalidate prior
claims; the sessions-DB mutation is atomic and keyed by source operation and
seat generation. Acknowledged messages stay historical. Stale or revoked claim
and ack attempts cannot change current work. Bounded quotas, dead-letter/held
states, retention and manual reconciliation must be specified before shipping.

Expose separately: intent committed, mailbox accepted, exact session claimed,
application delivered, and verified wake. None implies the next. Enqueue,
subscription, message acknowledgement or bus connection does not prove exact
chat delivery or wake. Exact-session proof requires an authenticated native
host adapter, correct installation/chat/session identity, a fresh addressed
reply and independent observation; busy/idle/reconnect behavior and same-agent
different-seat routing need real acceptance. No scheduled AI inbox polling,
automatic host launch, live routing switch or credentials are introduced by
this design.

## Failure and concurrency acceptance matrix

| Event | Required result |
| --- | --- |
| Revoke or grant expiry wins before a seat mutation commit | Deny; no mutation, receipt, audit or outbox partial commit. |
| Mutation commits before revoke | Preserve exact attributed audit; next request denied. |
| Two seat claims/transfers or target CAS writes race | One valid winner; loser conflicts; generation never decreases. |
| Old holder omits seat selector after transfer | Server classification rejects; no legacy fallback for that seat-bound route. |
| Human answers stale takeover after a newer generation | No transfer; response cannot disclose another project's private seat. |
| Old effect arrives before receiver learns new generation | New generation stays held until barrier advances/drains receiver; no claim of immediate fence. |
| Effect/receiver outcome or controller response is lost | Preserve in-flight/unknown; do not retry automatically or release a hold. |
| Hub or receiver restored to older data | Do not claim anti-rollback; fresh attestation/barrier and reconciliation required. |
| Knowledge write races transfer | Deny while unsupported, or prove receiver fence and transactional check. |
| Crash after Hub intent before sessions apply | Replay identical intent; one mailbox envelope and receipt. |
| Crash after sessions apply before Hub acknowledgement | Recovery sees sessions receipt; no duplicate or false delivery. |
| Old holder claims/acks during transfer | Old generation cannot acknowledge current work; preserve acknowledged history. |
| Target offline, vacant or has no main topic | Record scoped unrouted/pending; never route to an unrelated chat. |
| Grant/project revoked or archived during read/list | Fail closed, filter before pagination; missing and unauthorized are indistinguishable at direct lookup. |
| Request create/retry or CAS transition races | Exactly one idempotent create and one revision winner; no unauthorized data in write receipts. |

Required evidence includes synthetic source/target/third-project deny tests;
separate-process session/Hub/receiver race tests; injected failures at every
commit and response boundary; quota and pagination tests; stale and revoked
actor tests; same-family distinct installation isolation; tests for every
seat-enabled effect route and receiver; knowledge boundary tests; client/real
MCP and UI tests; and independent source/security review. Installed staging
acceptance must use the exact build and explicitly approved profiles; two real
roles must demonstrate request, target triage, scoped discussion and observed
exact-chat delivery, with no production or host-switch claim inferred from
synthetic tests.

## Phases and ownership

1. **Contract and adversarial review:** Codex backend/security owns a complete
   operation/receiver inventory and the server-bound session classification;
   Claude reviews the seat/topic semantics and user-facing contract. Resolve
   receiver barriers, external-effect reconciliation and knowledge boundary
   before implementation. This proposal is not that approval.
2. **Default-off substrate:** additive schema, authoritative seats and scoped
   requests/discussion, atomic audit, idempotency, CAS, owner policy, deny and
   race tests. A disabled/unwired substrate is not a delivered cross-project
   workflow and must not be called complete.
3. **Controlled receivers and messaging:** implement and prove barrier and
   outbox/applier protocols, exact target seat/session resolution and recovery;
   keep unsupported effect paths disabled for seat-bound principals.
4. **UI and integration:** Claude-owned UI consumes the reviewed API contract;
   a separate reviewer checks source/target privacy, readable state and device
   behavior. Do not overwrite another owner's edits.
5. **Staging and release:** owner-controlled admission and real exact-session
   delivery tests, including restart/expiry/revocation; retain rollback evidence.
   Only then consider an explicitly approved live migration. Never equate a
   built artifact, proposed design or acknowledgement with installed acceptance.
