# Hub independent review follow-ups

These are remaining acceptance items, not claims of live feature availability.
Reviewed dashboard source: `93ba46f9a2623248c41622bb491912bb337f1bfa`.
Claude reply: `b3b39d37-ec24-4e91-bc50-b9fd78fc7f6d`, 2026-10-17.

## Before dashboard pilot

- F1: require explicit decision option selection; confirm protected decisions with
  exact title/answer/revision. Recording a decision still does not grant execution.
- F2: isolate topic errors from healthy overview data; authentication failures and
  loss of the source still fail closed. No stale peer readiness or hidden fallback.
- F3: display actual authenticated actor/GitHub numeric identity, not a hardcoded
  operator identity.

F1–F3 were implemented and reviewed locally, with twenty real-Edge regressions
passing. This includes a hanging topic request not blocking fresh overview actions,
late topic authentication failure removing a private draft, exact protected-choice
confirmation and cancellation, and actual operator identity. Claude independently
re-reviewed `1b50667940f75f10887b08fa2336bbb17eb438e3` and confirmed F1–F3 resolved
in message `98f712a0-2a6d-4c4a-83b0-b2e137c3979e`. Claude reran install/unit/build/
TypeScript checks, not browser or Python checks. No pilot authorization is implied.

## Remaining contract and integration work

- G1: strict public response DTOs and schema coverage for overview, task, decision,
  run and related records are implemented. Nine Python tests exercise actual
  persisted states and private-field rejection. `scripts/check_hub_ui_contract.py`
  validates the actual TypeScript overview/topic fixtures against those models.
  TypeScript interfaces remain handwritten, not automatically generated.
- G2: protected execution grants are intentionally unavailable. Current decision
  answers have `execution_authorization=false`. Action/target/artifact/expiry/nonce
  binding is needed before any future protected execution feature; do not call the
  present decision inbox an execution-approval implementation.
- G3: project per-message delivery/processing/completion evidence into the UI.
  Durable acceptance, topic reachability, wake delivery, claiming and completion
  are separate facts. Do not infer later states from earlier ones.
- G4: surface peer reason codes and separately explain transport/host evidence.
  Existing placeholders are unknown, not successful health checks.
- G5: reconcile canonical topic rules with the deployed bus registry contract.
  The UI currently follows the implemented Hub rule; a peer's v6 draft is not a
  deployed contract or authority to alter existing routing silently.

These items remain subject to the full active goal and topic reachability addendum.
OAuth setup is deferred by Beau; no dev login is a substitute for its acceptance.

## Canonical registry bridge independent review

Claude reviewed `52d534c..65d7d0d` in bus reply
`2fb6d664-ee41-4362-b5f6-3fd75de5dced`. He independently ran232 backend registry
tests and7 topic-event unit tests; he did not run Playwright or live acceptance.
The review confirms original-agent identity preservation, scoped human readers,
closed/bounded payloads, independent cursors and fail-closed freshness behavior.

The following corrections are implemented and locally verified, awaiting Claude's
targeted independent re-review of the published patch:

- Distinguish panel-local `topic_read_forbidden` from real human-session loss.
  A restricted default/topic selection must not wipe healthy workspace records.
- Recheck local session access on upstream keepalive chunks, not just data
  pointers, and prove the upstream stream closes when that session is revoked.
- Bound event-driven refresh counts and report a recoverable manual-check state
  rather than allow an upstream event burst to drive unlimited snapshot reads.
- Clear stale topic markers when leaving a session or successfully reading a
  noncanonical source; document the browser's safe-integer cursor limit.

An invalidation pointer's revision is not required to equal a later fresh
snapshot's revision: a coalesced fetch can legitimately observe newer state, and
UUID revisions have no ordering. Topic and registry epoch must still agree.
Claude explicitly agreed in reply `10aeeeb1-c00c-4a9b-8ca8-c885e2109071` and
withdrew exact revision equality as a gap. The patch passes369 focused backend
integration tests,18 UI unit tests, TypeScript/Vite build and28 Edge E2E tests.
The stream test revokes a real synthetic SessionStore session between comment
chunks and proves upstream closure before reading the next chunk. The UI's
128-snapshot per-subscription budget closes to an explicit manual-Check state;
the browser rejects cursors above `2^53-1` without rounding or replay.
The existing independent review and test results do not prove native provider
enrollment, exact-session delivery, owner selection or restart/offline acceptance.

Claude's targeted read-only re-review of `fc56198..62528cc` is recorded in
`30d41a6d-78b9-47cc-a1ad-a459df344336`: all four corrections resolved, no blocking
defects. He inspected source but did not rerun tests. The 128-attempt budget is
per subscription lifetime, so even a legitimate long-lived busy topic eventually
needs explicit Check; it is not an outage detector or a timer-based retry policy.

## Previously undelivered container pilot reviews

Root retrieved these exact shared-knowledge documents from project
`engineering-hub` (not `agent-bus`); they were not available as ordinary replies
because the earlier chain reached its hop limit:

- `reviews/real-pilot-design-claude.md`, v1 SHA256
  `60104b9b44e464868dc12c9ef27aa3ed7c65f8daf5af06ee49d77e17bf5a9311`.
- `reviews/container-pilot-contract-review-claude.md`, v1 SHA256
  `7889b7c2c28ea95f2f7eda786c2994b708f96f8146f4d045eb42dbc91debda54`.
- `reviews/container-pilot-source-review-eaffa9a-claude.md`, v1 SHA256
  `a94892ccc3287a0d1f467638568a4705796c27fc490292af27d073e7c45a2eca`.

The source review found no blocking controller/client defects at `eaffa9a` but
identified material pre-host-integration work. Current changes make rollback
failure/uncertainty visibly held with fixed public event reasons,
preserving the same target reservation, command IDs, fence and repair count.
Run creation now independently revalidates the approved distinct registered
reviewer and current exact commit at consumption; a persisted `verified` label
alone is insufficient. Health now reports a nullable build commit baked into a
fixed installed package resource; runtime environment cannot supply it. Unknown
or malformed metadata remains unknown. Actual image verification is still needed.

Unresolved host/recovery gates from those reviews remain explicit:

- Exact stop/preserve/cutover semantics and a durable pre-effect previous-instance,
  image, state-generation and volume snapshot; no source hash alone proves these.
- Host-enforced four-phase ordering, fencing, bounded pause/deadline behavior,
  evidence allowlisting and secret-redaction tests, plus exact-ID-only teardown.
- Role-specific smoke identity: rollback uses the manifest's rollback commit,
  while the wire candidate commit remains the immutable run identity.
- A supported recovery operation for a dead controller advance owner. Inspection
  is read-only; no stale age, lease expiry or peer assertion authorizes takeover.
  The current controller deliberately retains that lock and does not pretend the
  first crash campaign can automatically continue past it.
- Actual independently verified deployment/health/rollback/crash campaign on the
  isolated runner. Earlier RepoDigest/liveness-only sketch suggestions are
  superseded by the full metadata/provenance/profile contract.

The old sketch also proposed claim-as-delivery and linked-reply-as-completion.
Neither inference is adopted: exact chat delivery, processing and completed work
require their own explicit evidence. A claim, ACK or arbitrary reply is not proof.

Claude's follow-up `29f3d90e-c753-48f8-8d28-0a41d80cd00c` accepted the independent
review gate, baked identity and fixed hold reasons by source inspection, but found
safe pause boundaries had become terminal holds. That is corrected: they return
423 and preserve the original pending/running/failed path. One hundred focused
controller/API tests prove unpause resumes only unsubmitted phases, while actual
uncertainty/failure remains held. Dispatcher accepted-pause receipts still require
reconciliation; this is not a claim of automatic restart recovery.

The same review identified a latent event-identity constraint at `1b9e5dd`:
global legacy dedupe keys `(run_id, command_id, event)` and pilot uses run ID as
command ID. The new optional `event_id` receipt namespace preserves that legacy
path and gives pilot phases separate deterministic identities without changing
effect command IDs. Candidate and rollback holds no longer collide. Repeated
inspection occurrences, phase-derived continuation and repair-count semantics
still need their own reviewed recovery operation; it is not added by this change.

## Recovery prerequisites, not service activation

Claude's `44b155d8-3a0c-42a0-a88f-ce4e90c8ddbe` accepted the pause correction by
source inspection (no independent test rerun) and agreed with the Linux single-
owner direction, with explicit inode, fork, filesystem, stopped-process, executor
fence, held-evidence, accepted-pause and visibility requirements. The source now
contains a standalone `PilotOwner` primitive only. It is not passed to the
workflow, dispatcher, default entrypoint or running service.

Root review found lock replacement could let a successor lock a new inode;
durable enrollment now pins both lock and database identities before epoch
adoption. Independent functional review found garbage collection leaked raw
descriptors; close-only finalization and all-descriptor cleanup are tested.
No lock file is removed. Existing un-enrolled databases are refused. The module
does not change permissions or exclude legacy writers that ignore the lock.

Injected typed responses also need revalidation: Pydantic `model_copy` and
`model_construct` bypass normal validation, and JSON serialization can turn a
Boolean into an integer. Pilot submit now validates original Python values,
then bounded strict JSON, before verifying or retaining a receipt. This does not
authenticate actual host effects or create a reconciliation operation.

## Executor integration review

Hermes corrected source: `7bdc3d406209208e81604e303f6e6e39e8d459b3` on
`work/hub-executor-v1`. It has not been integrated into the root control branch.
Peer-reported 42 focused tests and prior Docker success are not final exact-source
container acceptance. New final acceptance is still being performed by Hermes.

Independent functional review of prior `3d04054` found the constructor updated every running
command to held without proving its previous owner stopped. The pre-effect path
then does not revalidate current command ownership/state. A second instance can
mislabel active work while its original executor proceeds. Integration requires
single-owner/fenced ownership with genuine restart/quiescence handling and a
concurrent-instance regression, not merely another restart-after-completion test.

Independent read-only review of `7bdc3d4` found that defect corrected: initialization
and execution share a nonblocking kernel lock; the CLI also holds service lifetime
ownership until shutdown completes. This is scoped source acceptance for trusted,
private local Linux storage, with no mixed old/new writers or lock-file replacement.
Actual process death while a command is running, fresh-owner held recovery and
proof of no repeated effect still require a dedicated regression and final exact-
source/container acceptance. Exception handlers which already mark held before
reopening do not establish this crash case.

Canonical staging inventory v2 was actually retrieved from shared knowledge:
`agent-bus/engineering-hub/staging-inventory-2026-10-17.md`, SHA-256
`88d429d8052374e31db70f24539b6b96b2e2fe02ed5ddcf64502077779bb0a5a`.
It verifies distinct `//192.168.50.80/Staging` at `/mnt/Staging`, not server-side
ZFS/ACL/backup/reboot behavior. Global staging cleanup and external deployment
writers remain unreconciled. The first Hub fixture pilot stays on a local/CI
runner without NAS dependency; unique Compose naming alone is not writer exclusion.

Root's typed client now bounds fences to SQLite's positive signed-64-bit range.
No live executor, deployment, credentials or existing watcher was changed.

Hermes added `tests/test_executor_crash_recovery.py` in `da4cf845dbea2add6ec8f2b0b2c13441468bb665`.
Root fetched it and verified that its runtime `hub_executor` Git tree is exactly
the reviewed `7bdc3d4` tree, `5af2b9ff5a6d0ea5094e679c3b5f8263f24879d6`.
The test blocks before and after the real atomic filesystem effect, verifies a
durable running row, SIGKILLs the process without exception cleanup, then checks
fresh-owner held recovery and repeated stable-ID operations without a new effect.
Source inspection supports the intended stronger crash test; the final frozen
control/executor combined and container jobs are still awaiting Hermes's results.
Peer-reported 44 focused tests and the mutation check are not being substituted
for that final combined acceptance.

Final handoff `53f9bb188cf92de227d730e0aad129dadf19064e` subsequently published
evidence JSON only. Root fetched and inspected both reports. Hermes reports 683
Linux passes on the exact frozen Hub `1b50667` plus executor `da4cf84`, including
44 focused executor tests and both SIGKILL boundaries. Four actual container HTTP
fixture scenarios cover success, rollback, uncertain outcome and persistent pause;
each restarts without repeated command POSTs. This peer-run evidence does not cover
later root changes, real OAuth, a full REST task-assignment flow, an actual app
deployment or TrueNAS/host boot acceptance. Root integration remains a separate gate.
The older snapshot's loop warning is addressed by the separately verified test-only
ownership fixes documented in `hub-linux-validation-2026-10-17.md`.

### Pending ownership integration review: Claude, d4aba6f

Message `328fbfe5-faef-4629-907d-864810f8df57` arrived automatically in this
existing chat and was claimed and evaluated. This was source-only peer review,
not an independent test run. Before wiring PilotOwner into workflow mutations:

- Remove the lifetime raw SQLite database file descriptor, or enforce safe
  connection shutdown ordering. Closing an unmanaged descriptor can release
  process-wide POSIX SQLite locks. Add a separate-process write-lock regression
  test; existing green ownership tests do not cover this hazard.
- Separate ephemeral tmpfs/overlay validation from durable production storage
  acceptance. Neither ephemeral storage nor a container-layer inode establishes
  reboot/recreation durability.
- Document that restored/copied database identities deliberately refuse startup
  and require a separately reviewed re-enrollment procedure, not automatic repair.

The raw-descriptor finding is now fixed in source: existing database validation
uses no-follow, parent-dirfd-relative stat rather than raw open/close. The fresh
creation FD closes before SQLite starts. All 36 focused Linux ownership tests
pass, including an independent-process SQLITE_BUSY probe with an active sibling
transaction while the owner closes. A mutation restoring unmanaged raw FD close
fails that exact regression with `SQLite transaction lock was lost`, establishing
that the test detects the reviewed defect. This does not establish workflow
integration or production durability. The storage and restore items remain open
integration gates. Repeated same-phase recovery events also
still require occurrence identities. No production activation is authorized by
this review or by acknowledging its message.

The next source slice explicitly gates tmpfs/overlay behind a constructor-only
test opt-in and documents restored-inode refusal. Container pilot workflows and
their dispatcher now revalidate exact live owner capabilities at execution/write
boundaries and after awaits. Review found that rejecting legacy fixture dispatch
alone left direct/API fixture execution available: FixtureWorkflow now denies
any enrollment marker at its own boundaries too. Generic task/decision records
remain outside this execution guard; it is not a universal database write lock.
An enrolled dispatcher cannot use caller-Boolean reconciliation. Epoch-tagged
adoption, supervised startup/shutdown, recovery occurrence IDs and actual durable
host acceptance remain required before activation.

Pilot inspection now validates the original canonical command before GET and
strictly revalidates bounded typed response/echo afterward. It does not insert a
run binding, update a receipt, change run state, adopt ownership or resend effects.

### Repository barrier follow-up

Claude reply `4bdaf2b3-8cff-433c-a6da-cb4795455080` reviewed the next source
slice without running tests. It confirmed the descriptor, ephemeral-storage and
inspection corrections, and identified remaining generic repository mutation
bypass. A repository-level live-owner barrier is now integrated inside
`BEGIN IMMEDIATE` and before commit, including constructor schema/WAL protection;
workflow pre/post-I/O guards remain necessary. This supersedes the earlier
deliberate task/decision write exception. The frozen combined Linux suite passes
1,634 tests, including direct unowned mutation refusal and precommit rollback.
A terminal run left with
an advance reservation must retain visible terminal status and be reconciled
from original command evidence, not mistaken for an unaccepted request.

The new explicit prior-epoch recovery module underwent independent subagent
source review. Findings corrected before acceptance: inconsistent failed history
must not rewind to running; a foreign run command binding must deny; distinct
candidate/rollback image roles cannot assert the same container ID; and ordinary
validation/CAS exceptions must not strand this call's current-epoch inspection
claim. Owner loss/cancellation intentionally retains in-flight claims. Dedicated
tests cover original-command GET evidence, repeated occurrence IDs, pause,
ancestry across idle restarts, exact reservation CAS and one-time repair counting.
An actual Linux SIGKILL test covers controller lock release and durable receipts,
with simulated host evidence only. Peer review of the published combined slice
and actual host acceptance remain separate gates.

Claude follow-up `bb2bab14-79c8-4246-b38f-c027b533cccd` (source review only)
confirmed the repository barrier and recovery invariants, then identified the
remaining writable read-context/schema path. Read contexts now reopen enrolled
ledgers with SQLite mode=ro; schema initialization uses guarded transactions and
individual DDL statements. Cancellation of an inspection still conservatively
requires a verified successor epoch. No recovery HTTP/MCP endpoint may inherit
request cancellation directly: endpoint integration must use a supervisor-owned
task/result occurrence or prove unwind before same-epoch retry. That integration
is not claimed here. Repeated inspection history growth and a formal transition
matrix were review follow-ups, not silent production acceptance. The next slice
now declares and enforces an explicit recovery transition matrix, including an
integration regression against running-to-rolled_back rewind.

Quiescent attempt and dispatcher continuation source is now verified in the
1,700-test frozen Linux run. Read-only contexts, guarded schema transactions and
outbox publisher ownership cover the identified internal writer paths. Independent
subagent review identified malformed paired-owner UUID and recovery-prefix
starvation: canonical UUID checks, per-head seen records and fair limit=1 lanes
were added and tested. Original request/run binding, pause races and no-self-wake
behavior are covered. The real in-process runtime is exercised with an injected
event feed; no installed watcher or production service is activated. A remote
receipt changing by itself still needs supported host-to-Hub invalidation, and
owned lifecycle/cancellation handling remains a deployment gate.

Claude reply `3d9daaf9-08d1-4275-8255-4c56a878d7ab` reviewed the published
`bf5445a` source without running tests. Its held-receipt amplification finding is
addressed by narrowing same-epoch retry triggers to exact-run receipt/terminal
invalidation, unpause, or explicit recovery; unrelated task/decision activity is
not receipt evidence. Initial Linux validation caught a test using an agent to
perform a human-only task creation; the fixture identity was corrected without
relaxing authorization. Hot-WAL SIGKILL reads before successor epoch writes and
the owned service lifecycle passed their initial Linux tests. Final combined
validation subsequently passed 1,754 tests on frozen image
`2eaa8357b1ac79d9fc4f159fa5a86f783444917f0bee9014ead9a47f55e2b001`.

The suggestion to require a new user approval for all forward continuation is
not adopted as a new source restriction: Beau already authorized bounded
autonomous staging, including safe restart recovery. Execution still requires
the explicit opt-in dispatcher, exact reviewed manifest and original-command
evidence; uncertain effects remain held. No deployment or production activation
is authorized by an agent review. A distinct lifecycle review follows this
exhausted five-hop review chain rather than extending it.

Independent lifecycle review found a duplicate capability-transfer edge: a
second service construction could close the first service's owner on failure.
Single-use process-local transfer rejection and two Linux regression cases now
protect that path. Source review otherwise found no premature owner release on
the intended lifecycle. Hermes was sent INFO
`c61243f3-7b8e-4688-b7d6-7d9e97f6d76e` with the new peer-readiness requirement,
existing worker ownership and unchanged production boundary. Bus acceptance of
that INFO is not proof it reached an active Hermes session.

### Owned lifecycle independent review and live readiness discovery

Claude reply `24cf69c5-2f95-44a2-8606-2462cea65c7a` reviewed
`bf5445a..51341a0` source without running tests. It confirmed cancellation,
single-transfer, ownership drain and held-retry corrections. Its automatic
notification arrived during an active Codex turn, not an idle-wake test.
It identified supervisor grace as a remaining deployment hazard. Source now
requires explicit sufficient configured grace before capability transfer,
publishes asserted/not-verified lifecycle status, and the standalone profile
declares 1200 seconds. Validation evidence follows separately. The review's
60-second client assumption is stale: the actual maximum transport cap is 125
seconds. The conservative required budget is 1120 seconds, including inspection,
phase execution and cleanup allowance; it is not a verified installed kill timer.

Fresh one-shot authenticated discovery at `2026-10-18T01:36Z` found the live bus
advertises v0.6.0/schema 4, 24 tools, native inbox subscriptions and process-scoped
transport presence. It does not advertise canonical topic-registry tools. All
three identities had subscribed transport observations; these do not prove any
specific topic/session is linked or reachable. No credentials or live config were
changed by discovery.

Read-only source assessment confirms Hub snapshot/preflight and canonical SSE
projection exist, while the installed Codex connector handles inbox resources
only. Thus automatic same-topic readiness notices are **not implemented** by
those read paths. The next integration must extend the existing selected-session
connector with canonical topic invalidations and semantic-change coalescing, not
send readiness as inbox messages or create another daemon/presence database.
Expiry cannot be extended by hysteresis; a correlated REPLY alone is not explicit
completion evidence. Missing canonical provider/enrollment and session-bound
runtime evidence remain concrete prerequisites. Hermes request
`318a11f4-5948-4908-8110-160a35bed352` asks its existing lane for that published
contract; no replacement worker or production activation was requested. Claude
follow-up `eab66fc9-64cc-4868-84ce-5d14ef8860fe` records these distinctions.

The grace follow-up passed the frozen 1,778-test Linux suite, and independent
subagent source review found no concrete defect in the validation ordering,
status semantics or Compose consistency. No installed supervisor verification
or readiness-notification activation is inferred from those results.

### Current artifact handoff gate

A fresh scoped bus/knowledge/repository check after `2dd2457` found no pending
reply on this build correlation. Sender-scoped receipts show registry request
`318a11f4-5948-4908-8110-160a35bed352` and the latest Hermes durability INFO
`1a8735b2-ce93-4a6c-a9e2-0012470979f4` are retained/pending. Claude's durability
INFO is acknowledged. These lifecycle observations do not prove session delivery,
execution or a live worker, and no replacement worker was launched.

The existing shared note `agent-bus/engineering-hub/reviews/container-host-6b01bde.md`
version 1 (SHA-256 `b9c35089be9b7e79bb49fabb8e38797e307c3ef0f852420e1d62391ede01ab5f`)
reports source-only acceptance of corrections at
`6b01bde9403e53860e6fd6f8758a12784d5aefcb`. This supersedes the old host scaffold
rejection, not the actual host acceptance gates. Codex has not obtained or rerun
that source. The note says the concrete daemon lane
`work/container-pilot-daemon-source-hermes` / `deleg_ef420dc9` is separate and
incomplete; a dated note is not authoritative current process status. Trusted
provenance, runtime identity, baseline/state/secret providers and actual-effect
SIGKILL/health/rollback acceptance remain absent from Codex's evidence.

Fresh fetch and remote-ref inspection still show host branch `fedfb7b` and
registry branch `7799056`, no advertised daemon branch, and no local `6b01bde`
object. No full branch merge or artifact substitution was attempted. Hermes
handoff request `05426c6f-9af6-4574-a34d-b9d98e1fb5eb` asks for the exact reviewed
artifacts and an authoritative existing-worker status, without requesting another
worker or production changes. Its acceptance by the bus is not completion.
The note's controller 15-second/default 60-second ceiling concern is superseded
by the published 125-second client and manifest-plus-margin validation.

Current critical path is an obtainable reviewed host/registry handoff, the
concrete provider integration and an autonomous real application pilot. Human
GitHub OAuth setup remains explicitly deferred by Beau. These dependencies
prevent completion of a usable authenticated release; more passing fixture tests
would not resolve them. The goal remains active, not falsely complete.

### New host reply: explicit cgroup profile contract

Hermes request `a0b8f89b-be2d-4d3c-80ef-aca5f2cb04f4` subsequently arrived via
the existing automatic connector during an active Codex turn. This substantive
reply changes the next action; it is not an idle-wake or worker-liveness proof.
It reports local concrete daemon source `556c2c3`, correction `aaddeab2`, 26
driver and 379 combined source tests, with another independent review underway.
Those are peer reports; source remains unpublished and actual daemon effects,
provider implementations and host acceptance remain unverified by Codex.

Hermes asked Root to freeze cgroup namespace requirements. Root replied
`14beb9fd-b2f6-4bd2-84bf-d1a99e3ac9c4`: require the closed manifest runtime and
host EffectiveProfile to explicitly carry `cgroupns_mode="private"`; request and
verify Docker `HostConfig.CgroupnsMode="private"`, never infer a daemon default.
Docker's documented feature minimum is Engine API 1.41; backend capability checks
must enforce this or a stricter backend minimum. The shared source now declares
that required literal and feature-minimum constant, with missing/host/invalid and
copied-model bypass regressions. Independent read-only source review found no
integrity bypass and confirmed older manifests fail closed. Keeping the v1 label
is only a coordinated pre-activation draft tightening, not live wire compatibility.

Canonical bytes/digest change. Old accepted rows must not be rewritten or given
an inferred field; unactivated source fixtures/manifests need regeneration and
review with matching host/controller pins. Root also corrected the reply's stale
15/60-second client assertion again: the actual current cap is 125 seconds.
