# Hub to supervisor transport for reserve, challenge, dispatch and expiry

Status: design only. Nothing is implemented, enrolled or deployed. Builds on
`docs/supervisor-dispatch-claim-proof.md` (claim-proof branch, 6279574) and the
inspection transport (inspection v8, f1827e5).

## Principle: authority comes from signatures, not the channel

Every result the Hub acts on is already a signed envelope it verifies against a
separately pinned supervisor key: reservation receipts, dispatch challenges,
`expired_unconsumed` statements and inspection answers. The transport adds only
**who may ask** (least-privilege caller authentication), **who answers** (pinned
peer and machine identity), and **bounded cost** (framing, deadlines,
concurrency). A transport failure or unsigned reply never moves Hub state: it
leaves the attempt held, exactly as the inspection transport does today.

## Endpoints

One supervisor listener process on the supervisor host. It serves only these
routes and never exposes a Docker or engine endpoint. The executor socket stays
local to the supervisor host.

| Route | Calls | Credential scope | Response |
| --- | --- | --- | --- |
| `POST /v1/supervisor-route/reserve` | `reserve(command)` | `supervisor-route.reserve` | signed reservation receipt |
| `POST /v1/supervisor-route/challenge` | `issue_dispatch_challenge(command_id)` | `supervisor-route.challenge` | signed challenge |
| `POST /v1/supervisor-route/dispatch` | `dispatch(receipt, command, proof)` | `supervisor-route.dispatch` | unsigned outcome hint |
| `POST /v1/supervisor-route/expired-statement` | `expired_statement(nonce)` | `supervisor-route.expire` | signed `expired_unconsumed` statement |
| `POST /v1/supervisor-route/inspect` | inspection (existing) | `supervisor-route.inspect` | signed inspection answer |

- **Requests:** canonical JSON (the existing `_canonical` encoding) with exactly
  the fields of each call; receipt, challenge and proof travel as their exact
  signed bytes, base64url. No extra fields; unknown or missing fields are refused.
- **Dispatch reply is a hint only.** Its `RouteResult` is unsigned. The Hub records
  it as a hint and settles the attempt only from a signed inspection answer
  recorded in its own transaction. A lost or garbled dispatch reply is therefore
  harmless: the attempt stays held and inspection resolves it.
- **Idempotency:** reserve and expired-statement are idempotent (identical repeat
  returns identical bytes). Challenge issue isn't, but it's capped per executor.
  Dispatch is one-shot by the journal; repeating it is refused with zero bytes.

## Caller authentication (least privilege)

- One enrolled bearer per scope, each with its own prefix (`ehrr1.`, `ehrc1.`,
  `ehrd1.`, `ehre1.`, plus the existing `ehri1.` for inspect). The supervisor stores
  only a SHA-256 of each, compares in constant time, and a token for one scope is
  refused for every other scope and by the legacy launch bearer check.
- The Hub's dispatch path holds reserve, challenge and dispatch tokens; the
  resolver holds expire and inspect. No component needs all five.
- Tokens are an access-control and DoS layer only; they never substitute for the
  signed proof (dispatch still requires a valid Hub proof) or the signed answers.

## Machine identity and channel

Two deployment shapes; both pin identities explicitly and use no CA trust.

1. **Hub and supervisor on the same host:** a Unix socket. The Hub checks the
   supervisor peer with `SO_PEERCRED` uid/gid/pid plus pidfd start ticks
   (`SupervisorPeerPin`) before sending and again after, as inspection does. The
   supervisor checks the Hub peer's uid/gid against its enrolled Hub service
   identity.
2. **Different hosts:** TLS 1.3 with mutual authentication and **pinned SPKI
   hashes in both directions** (no CA, no hostname trust), on one dedicated
   listener bound to a specific interface, with the Hub host's pinned client key
   as its machine identity. The supervisor's TLS key is separate from its
   signing keys.

Either way, the supervisor binds the request to its own configured executor pin,
launch epoch and boot id; the Hub separately checks those inside the signed
envelopes. Nothing here relies on network location for authority.

## Bounds

- Strict HTTP framing and one total deadline per exchange, reusing the
  inspection transport's `_read_response` and its framing rules on both sides.
- Bounded, non-blocking concurrency per instance: a pre-auth pool and a post-auth
  pool per route group (dispatch path and resolver path separately), full pools
  return an unsigned 503 `busy` at once.
- Return-time deadline check before any signed reply (best-effort, as inspection).
- Body size caps per route.

## Failure semantics on the Hub

| Situation | Hub result |
| --- | --- |
| Peer pin or TLS pin mismatch | held, zero bytes sent |
| Timeout, framing error, 4xx/5xx, unsigned or unverifiable body | held |
| Reserve lost | repeat reserve with the identical command (idempotent) |
| Challenge lost | issue a new challenge (old one expires; cap applies) |
| Dispatch reply lost | held; resolve via inspection, or via `expired_statement` if the challenge was never consumed |
| Expired-statement lost | repeat (idempotent) |

## Out of scope here

Key and token enrollment and rotation, the Hub barrier and typed authority (Hub
side), wiring the executor gate into PilotASGI, rollback anchors, and any
staging or production deployment.

## Local fake-based test plan (resource-bounded)

All in one test file, run once in `eh-goal-validation --network none` (loopback
only, no load loops), with fixture keys, tokens and test certificates generated in
the test.

1. **Happy path over a Unix socket:** reserve, verify receipt, challenge, verify,
   sign a fixture Hub proof, dispatch to a fake executor (wrapping
   `DispatchCounterGate.run` around a marker-file effect), inspect, apply. Marker
   written exactly once.
2. **Same path over mTLS on loopback** with pinned SPKI both ways.
3. **Scope matrix:** each token against every route; only its own scope succeeds;
   the legacy launch bearer is refused everywhere.
4. **Identity:** wrong supervisor peer pin, wrong Hub uid, wrong server SPKI, wrong
   client SPKI: refused, zero application bytes.
5. **Dispatch reply lost** (fake drops the response): Hub held; inspection
   returns `launched`; applied once; marker written once.
6. **Crash before dispatch:** proof never sent; expired-statement over the
   transport retires the reservation; Hub frees the barrier; marker absent.
7. **Replay:** the same proof sent twice over the transport; second is refused
   (`challenge_used`), marker still once.
8. **Bounds:** oversize body, trickled and stalled responses, full pools, each
   held or busy within the deadline (test-owned barriers, no wall-clock slack).
9. **Unknown fields / non-canonical requests** refused.
