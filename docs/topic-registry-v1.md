# Canonical topic registry V1 source contract

Status: source-only contract for review. Base ce50f67df86ccab78dda2a0bbc5d4672a4a3073a; separate branch work/topic-registry-v1-hermes. This does not activate a provider, issue credentials, establish human OAuth, mutate live bindings or change existing bus ports/endpoints/tokens. Existing transport-only topic presence is not selected-chat readiness. Hub owns its adapter/UI and remains a read-only projection, not another registry.

## Trust and default state

Ordinary authenticated Hermes/Codex/Claude tokens may read only operator-configured topic membership. They cannot select a conversation, issue owner intent, register a trusted observer, manufacture native attachment or promote readiness. Unknown/unconfigured authority fails closed, not a synthetic empty/unlinked inventory.

Source integration accepts separate trusted owner/provider authenticators and native/runtime evidence verifiers by explicit constructor injection. Defaults do not provide them; protected writes return503 authority_unavailable. Existing agent tokens must still be rejected for protected writes even if a verifier is configured. Test-only principals/verifiers are fixtures, not a production issuance or actual client acceptance claim. No env variable automatically enables this slice or provisions a key.

An owner authorizer must return a scope-bound grant from an independently authenticated human path, not infer human authority from admin/agent credentials, a caller role field, knowledge content or a peer's quotation. The grant binds its single-use intent ID, exact operation/topic/agent/provider/private session reference, expected binding revision and expiry. Consume the intent transactionally only with a successful CAS mutation. Actual issuer integration is deferred; unavailable issuance must be visible.

A provider authenticator yields an enrolled provider identity and its fixed agent scope. Its events alone cannot assert verified native attachment/runtime acceptance. Independent server integration verifies the exact current topic/native attachment and the selected private runtime session/queue. Default missing verifier cannot produce reachable or busy_queued. The API accepts no native_verified/runtime_verified/ready=true flag.

## Persistent/private model

Add only registry-prefixed tables to the existing local bus SQLite path when this optional source is explicitly initialized. Do not change the bus schema version or auto-initialize via normal startup. Registry tables carry a separate schema version1:

- registry metadata: schema version, independently allocated public snapshot UUID, private active server incarnation, durable outbox sequence.
- topics: validated slug and explicit inventory-complete flag. Completion comes from trusted bootstrap/import, not ordinary read or lack of rows.
- binding history and heads: topic+agent, immutable random binding revision UUID, monotonically increasing private generation, selected provider and private session reference, provenance owner_verified/legacy_unverified, linked/unlinked tombstone state. Keep history across unlink/relink; never reuse generations/revisions or derive public values from session IDs/hashes.
- consumed owner intents: grant ID and bound canonical request digest, unique and transactional with selection.
- provider attachments/observations: private provider incarnation and current server epoch, binding revision, independently allocated public attachment revision, monotonic sequence, server-observed timestamp, event and server-derived evidence level. Old incarnations never revive green state.
- registry outbox: AUTOINCREMENT seq, topic, snapshot_revision and safe event kind only, inserted in the same transaction as each material state mutation. No message bodies, private session references, hashes or provider secrets.

Acquire exclusive local process ownership BEFORE initializing/recovering active authority or changing its epoch. A second authority must refuse rather than invalidate a genuinely live owner. Read-only repository objects do not start/recover authority. Keep SQLite and locks on private local storage, never SMB/NFS. Restart retains selected bindings/history but invalidates all prior live attachments/observations until fresh authenticated attachment/runtime evidence arrives. Do not recreate the executor's fixed constructor-adopts-live-work defect.

Legacy import is explicit trusted bootstrap, marks legacy_unverified, never authorizes readiness, and does not silently treat MCP topic notes as owner proof. An authoritative complete inventory may report no binding as unlinked; an incomplete/unconfigured inventory reports unknown_stale. Source tests must distinguish these.

## Draft optional HTTP namespace and strict shapes

Routes are proposed `/api/topic-registry/v1/...`, not changes to existing message endpoints. Use bounded JSON, duplicate/unknown field rejection and safe machine-readable errors. Agent write attempts fail403; unauthenticated calls401; missing protected authority503; malformed data400; CAS/stale generation/conflicting duplicate409. Topic reads are membership checked; unrelated topics are denied without returning their contents.

1. POST bindings/select, human-authorized only:
   `{topic, agent, provider_id, private_session_ref, expected_binding_revision, owner_intent}`.
   Session reference is a bounded private opaque string, preserved literally; do not normalize IDs. Expected revision is explicit null for initial selection or the exact current public UUID. A successful selection issues a fresh server revision/generation and clears live evidence atomically. Owner grant must match the entire operation; replayed intent cannot mutate again.
2. POST bindings/unlink, human-authorized only:
   `{topic, agent, expected_binding_revision, owner_intent}`.
   Create a tombstone/new revision and invalidate evidence, without deleting historical generations or granting ordinary agents ownership.
3. POST attachments, trusted provider only:
   `{topic, agent, binding_revision}`.
   Authenticate provider scope and selected provider; independently validate the native attachment for that topic. Return server-issued opaque attachment_revision and server incarnation token to this provider only. Superseding attachment invalidates old evidence. This is not an arbitrary chat selection or public stream/session handle.
4. POST observations, trusted provider only:
   `{topic, agent, binding_revision, attachment_revision, sequence, event}`.
   Event is ready|busy_queued|disconnected|offline|stream_connected. All readiness proof derives from the configured verifier against the selected private session and current native attachment, never caller assertions. Server assigns observed_at and bounded validity. Same sequence+identical event is idempotent; same sequence with different event409, older sequence409. Replaced binding/attachment/server epoch409. ready requires verified idle admission, busy_queued verified queue admission. stream_connected alone is linked_unverified. Disconnect invalidates readiness immediately. Old callbacks cannot clear or restore a replacement.
5. GET topics/{topic}/snapshot, authenticated bounded read:
   `{topic, registry_epoch, snapshot_revision, as_of, complete, source_state, peers}`.
   Public epoch/revision are fresh opaque UUIDs, not private incarnation IDs. At most the fixed three agents. Each peer has `{agent,status,reason,link_state,transport_state,availability_state,evidence_level,binding_revision,attachment_revision,last_evidence_at,valid_until}`. No private session/provider transport IDs or hashes. Status is unlinked|linked_unverified|reachable|busy_queued|offline|unknown_stale. Missing/legacy/untrusted/mismatched/expired evidence cannot be green. Evaluate expiry at actual server read time; do not refresh evidence timestamps on GET. A degraded native source produces unknown_stale, not online. Presence/preflight is advisory, not delivery assurance.
6. GET topics/{topic}/events?after=N, authenticated bounded durable cursor catch-up on connection/reconnection or actual native notification only:
   safe event pointers plus durable head, next cursor, has_more and public epoch. Validate future/invalid cursors against durable head in the same read snapshot, preserve legal pruning gaps, bound the page. No timer, inbox/model polling or new worker.

Per-message accepted/queue-delivered/turn-processing/task-completion is deliberately NOT inferred from registry state or ACK. That separate source contract is in MCP agent-bus/engineering-hub/drafts/container-pilot-and-message-evidence-v1.md. No inbox aggregation is introduced here.

## Native invalidation and Hub bridge

Provide a registry outbox cursor reader compatible with the existing SQLiteEventSource commit barrier. Combine registry and message invalidation in the existing bus observer when explicitly integrated; do not install a second OS watcher/model poller. A read-only registry repository is not itself a live observer.

An optional source-only bridge may return namespaced registry-topic invalidations to a separate callback, never interpret topic names as message recipients. Keep independent durable message and registry cursors; do not compare/sum unrelated sequence spaces. Startup snapshots both heads under the existing committed snapshot/barrier. External committed writers must notify; uncommitted WAL writes must not emit authoritative pointers. Idle subscriptions perform no SQLite reads. Source failure/stop invalidates availability; bounded catch-up and a full snapshot on epoch changes recover after reconnect.

Hub consumes only the authenticated redacted snapshot and native revision/cursor invalidations, with source identity pinned. It must not construct fake private session IDs to satisfy the old internal TopicBinding model. Either adapt its pure projection to public revision/evidence subjects or consume the canonical safe projection with server-time expiry checks. No writable Hub registry, frozen cached green deadlines or client-asserted observer provenance. API names remain draft until the two component owners agree; source tests can exercise the optional factory without wiring normal production startup.

## Required source acceptance and limits

Test owner/ordinary-agent/provider trust separation, exact grant scope and single use, CAS races, unlink/relink ABA, literal private IDs, incomplete inventory, legacy binding, wrong topic/provider, conflicting/old sequences, stale binding/attachment/restart callbacks, real clock expiry, disconnect/preflight race, redaction, independent per-topic status and unavailable verifiers. Add actual subprocess ownership/restart exclusion and real Linux native external-commit/uncommitted-WAL/idle/shutdown checks. Test all existing baseline bus/Hub behavior remains unchanged and new factories do not activate by default.

Publish a reviewable source commit with exact test evidence. Distinguish working registry source/API/native fixture tests from authenticated live provider issuance, human selection UX, actual Codex/Claude/Hermes adapter acceptance, Root adapter integration and deployed activation. All latter gates remain unfulfilled until separately exercised.
