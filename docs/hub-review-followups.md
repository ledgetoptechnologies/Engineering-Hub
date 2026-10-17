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
