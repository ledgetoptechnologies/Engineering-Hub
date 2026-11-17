# Watcher binding and adapter contract (slice 1)

Task: `6b3d39bb-8e68-4da2-808c-63e6c3b81067`. This is an internal, pure
contract module; it starts no watcher and changes no existing listener.
The Hub architecture note `architecture/task-threads-and-workers.md` v2 is
the design source. The full task remains open after this slice.

## Exact routing

`TaskRoute` identifies canonical project ID, full canonical task UUID and role.
`TaskBinding` adds agent, host, session ID and a nonempty opaque session generation.
No prefix resolution is supplied. The future authenticated binding store must
enforce task ownership/reviewer grants and serialize binding replacement.
These models do not authenticate a peer assertion or confer registration rights.

The authenticated bus envelope provides message ID and recipient. Its body
provides topic, task ID and roles. The task correlation must be exactly
`<project>/task-<full UUID>`. A lead target is allowed only for an explicit
worker escalation. Ordinary `projects-lead` messages need a separate lead route;
they cannot enter this task-routing function. Project subscriptions are not
fallbacks for worker traffic.

General lead delivery belongs to a separate control route, never a per-task
binding or a project subscription. A per-task lead binding here represents only
that task's explicitly configured escalation destination. Held escalations
return `escalation_intent=False` to prevent recursive escalation. Same-role
self-wakes are invalid. A held escalation must still reach Lead through a
separate lead control route or durable alert; absence of a per-task lead binding
must never silently discard it. Delivery and alert reconciliation belong to the
future durable dispatcher, not this pure decision function. Before a live
dispatcher, the authenticated envelope
sender must be resolved against the authoritative task owner/reviewer and
matched to `from_role`; this slice has no sender-authority integration.

`plan_task_delivery` holds missing/duplicate bindings, recipient mismatches,
unsupported adapters, missing sessions and stale targets. A hold returns a
reason and escalation intent, without sending anything or discarding the message.
A ready result contains the exact immutable binding and message pointer. It is
preflight evidence, not a receipt or permission to use an unconditional send API.

## Adapter boundary and current capability limit

A qualifying `SessionAdapter` must obtain the generation from the authoritative
runtime and atomically compare the complete target when accepting delivery.
Separate observe-then-send calls are insufficient: the session could restart
between them. Session generation must identify the runtime incarnation, not a
thread title, PID, arbitrary locally generated nonce or persisted thread UUID.
Missing capabilities default to false. Do not substitute another/newer session.

The exposed Codex desktop `send_message_to_thread` schema inspected on
2026-11-16 takes threadId, hostId, prompt and optional model/thinking. It exposes
no expected-generation condition. `read_thread` reports identity/status and
turns but no authoritative runtime generation. The installed app-tools bundle
26.1111.21308 loads dynamic app tool schemas; the existing session connector
sends by thread ID. None establishes the required atomic conditional operation.
Consequently no production Codex adapter is implemented or claimed supported
by this PR. Claude/Hermes adapters remain unqualified as well.

The capability booleans are integration inputs, not evidence of atomicity.
Before any real adapter is admitted, a reusable conformance suite parameterized
over adapters must reject an observer that merely echoes the supplied target
and an adapter that sends by thread ID without the generation precondition.
It must exercise independently changed/missing sessions, the observation/send
race, and mismatched receipts against the actual runtime. The synthetic fixture
in this slice illustrates expected semantics only. Observation provenance must
come from an independently authenticated exact-host/agent/session runtime read,
not from message bodies, stored bindings, or a capability self-declaration.

Future adapters return a receipt echoing the exact request. Accepted means only
the app accepted the pointer, never that the model ran or completed the task.
Stale/missing/unsupported hold. Exceptions, timeouts, malformed/mismatched
receipts or lost replies are unknown and require reconciliation, not blind retry.
Adapters must neither claim nor acknowledge bus work nor create replacement chats.

## Remaining slices and acceptance

Before any adapter call, the watcher must durably reserve the attempt with its
exact binding revision and message ID. The caller must serialize rebindings with
dispatch and validate the exact echoed receipt. A persistent cursor/inbox/outbox,
deduplication, singleton per PC/agent, capped batches and wake budgets, durable
escalations, and restart reconciliation are still required. Cursor advancement,
receipt, task evidence and message disposal are different transitions. Messages
remain retained until their decisions/evidence are recorded on the Hub task.
Escalation intent must enter a durable deduplicated queue with a per-lead wake
budget; it is not a request to immediately send a new lead message on each hold.
The dispatcher must revalidate records at trust boundaries. This module validates
copy updates and revalidates inputs, but direct `model_construct` or Python object
mutation is not an authentication mechanism and must not be exposed to peers.

No daemon, logon registration, new subscription, polling, SDK runtime injection
or migration is included. Existing listeners remain in service. Contract tests
cover exact routing and a synthetic generation-change race. They do not prove
actual app delivery, singleton enforcement, persistence, reboot recovery or a
full working day. Those require separate integration and real-machine evidence
before replacement. Independent review must name the exact immutable PR head.
