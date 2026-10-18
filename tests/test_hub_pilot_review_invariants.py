"""Run-boundary review checks, including legacy/malformed persisted task rows.

These are local controller/API fixtures, not Docker or deployment acceptance.
"""
import json

from fastapi.testclient import TestClient
import pytest

from engineering_hub.app import create_app
from engineering_hub.repository import HubError
from tests.test_hub_container_pilot import (
    COMMIT, IMAGE, reviewed_task, repo, target, ContainerPilotWorkflow, FakeClient,
)
from tests.test_hub_container_pilot_api import (
    CODEX, PILOT, prepare, run_payload, task_through_http,
)


INVALID_REVIEWS = [
    {'actor': 'codex', 'commit': COMMIT, 'verdict': 'approve'},
    {'actor': 'not-an-agent', 'commit': COMMIT, 'verdict': 'approve'},
    {'actor': 'human:1234', 'commit': COMMIT, 'verdict': 'approve'},
    {'actor': 'claude', 'commit': COMMIT, 'verdict': 'reject'},
    {'actor': 'claude', 'commit': 'f' * 40, 'verdict': 'approve'},
    {'commit': COMMIT, 'verdict': 'approve'},
    {'actor': 'claude', 'commit': COMMIT},
    [],
    'not-a-review',
    None,
]


def replace_review(repo, task, review):
    # Simulate data from a legacy DB or a malformed import. There is no public
    # task mutation endpoint that may mint an approved/self-reviewed record.
    changed = dict(task, review=review, status='verified')
    with repo._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?',
                   (json.dumps(changed), task['id']))
    return changed


@pytest.mark.parametrize('review', INVALID_REVIEWS)
def test_direct_pilot_run_requires_independent_current_approved_review(repo, target, review):
    workflow = ContainerPilotWorkflow(repo, FakeClient(), [target])
    task = replace_review(repo, reviewed_task(repo), review)
    with pytest.raises(HubError, match='review_required'):
        workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    assert repo.list_runs() == []
    assert workflow.client.submissions == []
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_target_fences').fetchone()[0] == 0


@pytest.mark.parametrize('review', INVALID_REVIEWS)
def test_http_pilot_run_rechecks_review_before_reservation(tmp_path, review):
    harness = prepare(tmp_path)
    app = create_app(harness.settings, repository=harness.repo,
                     container_pilot_workflow=harness.workflow)
    with TestClient(app, base_url='https://hub.test') as client:
        try:
            task = task_through_http(client, app)
            task = replace_review(harness.repo, task, review)
            client.cookies.clear()
            response = client.post(PILOT, headers=CODEX, json=run_payload(task))
            assert response.status_code == 409
            assert response.json() == {'detail': {'code': 'review_required'}}
            assert harness.repo.list_runs() == []
            assert harness.commands == []
        finally:
            client.portal.call(harness.adapter.aclose)
