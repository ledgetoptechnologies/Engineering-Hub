# Experimental cross-seat foundation

This source-only, explicitly default-off substrate is not the cross-project
request feature or a general seat authority. It adds no automatic routing,
mailbox, wake, receiver fence, lease, stale takeover, external effects or
execution grant. The test-only programmatic `Settings(seat_foundation_enabled=True)`
requires an EHS Hub bridge; no environment/deployment switch, live migration,
credential issuance or grant is supplied. With the default flag off, no new seat indexes, HTTP routes, MCP tools or
capabilities are registered; previously stored seat records and indexes remain
on disk and are not migrated or deleted.

When explicitly composed, the human owner can create a private project/topic
and seat and explicitly bind an already active EHS session. Binding requires
current project scope and both `cross.seat.read` and `cross.seat.handoff`
capabilities, is compare-and-swap, increments the generation and enforces one
seat per session. The owner can explicitly transfer using the expected seat
revision; there is no automatic takeover or claim that a handoff happened. An
unresolvable prior session blocks transfer; the owner can explicitly unbind
the seat without assigning new authority. Unbinding also advances generation.
A session's
current seat is derived by the server; the new handoff operation checks its
session, project, generation and revision in the same Hub transaction, and
session/grant revocation and expiry under the session writer. Owner actions
require the normal human session and CSRF protection. Mutations suppress generic
bus publication.

This identity proves only an EHS session, not a particular native chat or
verified topic listener. Existing unseated/legacy grants and routes are
unchanged and are **not seat fenced**; this substrate must not be used as proof
that an old holder cannot perform other existing actions. It does not solve
receiver-fence advancement, in-flight external effects, the separate knowledge
DB, cross-database mailbox moves, actual delivery or anti-rollback. See
`cross-project-seat-authority-design.md` for the unaccepted full contract and
required review and acceptance gates. No live enabling is authorized here.
