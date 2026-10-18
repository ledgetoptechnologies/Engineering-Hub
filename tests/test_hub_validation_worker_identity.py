from uuid import uuid4

import pytest

from engineering_hub.repository import AGENTS, HubError
from engineering_hub.work_queue import SOFTWARE_VALIDATION_EXECUTOR, WorkWorker
from hub_validation_worker.core import ValidationJob


def test_fixed_private_executor_identity_does_not_add_an_agent_identity():
    identity = WorkWorker(SOFTWARE_VALIDATION_EXECUTOR, str(uuid4()))
    assert SOFTWARE_VALIDATION_EXECUTOR not in AGENTS
    assert set(AGENTS) == {'codex', 'hermes', 'claude'}
    job = ValidationJob.from_claim({
        'id': str(uuid4()), 'action_key': 'engineering-hub.validate',
        'project': 'engineering-hub', 'machine_id': 'staging-pc',
        'environment': 'staging', 'requires_execution_proof': True,
        'target': {'task_id': str(uuid4()), 'source_commit': 'a' * 40,
                   'artifact_digest': 'sha256:' + 'b' * 64},
        'worker': identity.encoded(), 'fence': 1})
    assert job.worker == identity
    assert job.target.source_commit == 'a' * 40


@pytest.mark.parametrize('name', ['executor:other', 'executor', 'admin',
                                 'executor:software-validation:other', 'Executor:software-validation'])
def test_no_arbitrary_private_role_can_be_constructed(name):
    with pytest.raises(HubError, match='invalid_work_worker'):
        WorkWorker(name, str(uuid4()))
