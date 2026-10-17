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
confirmation and cancellation, and actual operator identity. Claude's re-review of
the correction commit is still pending; no pilot authorization is implied.

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

Hermes corrected source: `3d040542e231ff8daa7036c724a865c4c3f3806f` on
`work/hub-executor-v1`. It has not been integrated into the root control branch.
Peer-reported 39 tests and prior Docker success are not final exact-source
container acceptance. New final acceptance is still being performed by Hermes.

Independent functional review found the constructor still updates every running
command to held without proving its previous owner stopped. The pre-effect path
then does not revalidate current command ownership/state. A second instance can
mislabel active work while its original executor proceeds. Integration requires
single-owner/fenced ownership with genuine restart/quiescence handling and a
concurrent-instance regression, not merely another restart-after-completion test.

Root's typed client now bounds fences to SQLite's positive signed-64-bit range.
No live executor, deployment, credentials or existing watcher was changed.
