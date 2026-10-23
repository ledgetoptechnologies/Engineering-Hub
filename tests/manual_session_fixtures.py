"""Explicit synthetic operator acceptance for disposable tests, never production trust."""
from uuid import uuid4

from engineering_hub.startup_reconciliation import (
    ExecutorSessionIdentity, OperatorReconciliation, StartupReconciliation,
)


def synthetic_reconciliation(owner, target, *, accepted=True):
    """Real process/owner capability; synthetic identity and attestations only."""
    executor = ExecutorSessionIdentity(str(uuid4()), 'sha256:' + 'e' * 64)
    gate = StartupReconciliation(owner=owner, database=owner_database(owner, target),
        target_id=target.target_id, manifest_digest=target.manifest_digest,
        executor=executor, lifetime_seconds=3600, current_executor_identity=lambda: executor)
    if accepted:
        gate.accept_operator_reconciliation(owner=owner, report=OperatorReconciliation(
            gate.challenge, owner.epoch, target.target_id, target.manifest_digest, executor,
            'sha256:' + 'f' * 64, True, True, True))
    return gate


def owner_database(owner, target):
    # PilotOwner intentionally exposes no database-path property. Test callers
    # construct the real owner at a known path and pass it through this wrapper.
    # The private path is fixture plumbing only, not a production API dependency.
    return owner._path


def synthetic_workflow_reconciliation(workflow):
    return synthetic_reconciliation(workflow.pilot_owner, workflow.targets['engineering-hub:staging'])
