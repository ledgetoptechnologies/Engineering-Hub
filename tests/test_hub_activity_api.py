import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from engineering_hub.activity_api import project_activity, attach_activity_routes
from engineering_hub.repository import HubRepository, HubError


def test_real_history_sequence_pagination_same_timestamp_and_project_scope(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    task = repo.create_task('human:42', 'treedar', 'Original title', 'first')
    repo.create_task('human:42', 'engineering-hub', 'PRIVATE OTHER PROJECT', 'other')
    # Simulate another persisted audit revision, not a current-state timestamp.
    with repo._transaction() as db:
        entity = dict(task, title='Current context')
        repo._save(db, 'codex', 'task.proposed', 'task', entity)
        db.execute("UPDATE hub_audit SET created_at='2026-10-22T00:00:00Z'")
    page = project_activity(repo, project='treedar', limit=1)
    assert page['source'] == 'hub_audit'
    assert page['items'][0]['action'] == 'task.proposed'
    assert page['items'][0]['current_title'] == 'Current context'
    second = project_activity(repo, project='treedar', limit=1, before=page['next_before'])
    assert second['items'][0]['action'] == 'task.created'
    assert second['next_before'] is None
    assert second['items'][0]['sequence'] < page['items'][0]['sequence']
    assert 'PRIVATE OTHER PROJECT' not in json.dumps([page, second])
    # Deleting audit history must not fabricate replacement current-state rows.
    with repo._transaction() as db:
        db.execute('DELETE FROM hub_audit')
    assert project_activity(repo, project='treedar')['items'] == []


@pytest.mark.parametrize('kwargs', [{'project': 'missing'}, {'project': 'treedar', 'limit': True},
    {'project': 'treedar', 'before': 0}, {'project': 'treedar', 'before': 2**63}])
def test_activity_bounds(tmp_path, kwargs):
    with pytest.raises(HubError):
        project_activity(HubRepository(tmp_path / 'hub.sqlite3'), **kwargs)


def test_activity_auth_and_api_bounds(tmp_path):
    app = FastAPI()
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    def actor(request):
        if request.headers.get('x-fixture-session') != 'yes':
            raise HTTPException(401)
        return 'human:42'
    attach_activity_routes(app, repository=repo, actor=actor)
    with TestClient(app) as client:
        assert client.get('/api/hub/v1/activity?project=treedar').status_code == 401
        headers = {'x-fixture-session': 'yes'}
        assert client.get('/api/hub/v1/activity?project=treedar', headers=headers).json()['source'] == 'hub_audit'
        assert client.get('/api/hub/v1/activity?project=treedar&limit=101', headers=headers).status_code == 422
        assert client.get('/api/hub/v1/activity?project=treedar&before=-1', headers=headers).status_code == 422
