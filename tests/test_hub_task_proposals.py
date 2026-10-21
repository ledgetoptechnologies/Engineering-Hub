"""Real local persistence/HTTP/MCP staging proposal boundaries; no executor."""
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker
from engineering_hub.work_queue_api import WorkSubmission, submit_reviewed_work


COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
BASE = '/api/hub/v1'


def verified(repo, task):
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    with pytest.raises(HubError, match='independent_review_required'):
        repo.review_task('codex', task['id'], task['revision'], COMMIT, 'approve')
    with pytest.raises(HubError, match='revision_conflict'):
        repo.review_task('claude', task['id'], 1, COMMIT, 'approve')
    return repo.review_task('claude', task['id'], task['revision'], COMMIT, 'approve')


def queue_for(repo):
    return WorkQueue(repo, {'hub.validate': WorkAction('staging-pc',
        frozenset({'staging', 'production'}), project='engineering-hub', requires_execution_proof=False)},
        target_verifier=lambda *args: True, approval_verifier=lambda *args: True)


def test_repository_proposal_idempotence_provenance_persistence_and_no_automatic_authority(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    item = repo.propose_staging_task('codex', 'engineering-hub', '  Pilot  ', 'key')
    assert item['scope'] == 'staging' and item['proposed_by'] == 'codex'
    assert item['owner'] is None and item['status'] == 'assigned'
    assert item['candidate_commit'] is item['review'] is None
    assert repo.propose_staging_task('codex', 'engineering-hub', 'Pilot', 'key') == item
    with pytest.raises(HubError, match='idempotency_conflict'):
        repo.propose_staging_task('codex', 'engineering-hub', 'Other', 'key')
    peer = repo.propose_staging_task('hermes', 'engineering-hub', 'Pilot', 'key')
    assert peer['id'] != item['id']
    assert len(repo.list_outbox()) == 2
    with repo._connection() as db:
        audit = db.execute('SELECT actor,action FROM hub_audit ORDER BY sequence').fetchall()
        assert [tuple(row) for row in audit] == [('codex', 'task.proposed'), ('hermes', 'task.proposed')]
    assert HubRepository(repo.path).get_task(item['id']) == item
    assert repo.list_runs() == []
    for actor in ['human:42', 'unknown', None]:
        with pytest.raises(HubError, match='forbidden'):
            repo.propose_staging_task(actor, 'engineering-hub', 'Pilot', 'key2')
    for value in ['', ' ' * 2, 'a' * 201]:
        with pytest.raises(HubError, match='invalid_input'):
            repo.propose_staging_task('codex', 'engineering-hub', value, 'key2')
    with pytest.raises(HubError, match='invalid_project'):
        repo.propose_staging_task('codex', 'unknown', 'Pilot', 'key2')


def test_reviewed_proposal_can_enter_staging_but_never_production_even_direct_queue(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    queue = queue_for(repo)
    item = verified(repo, repo.propose_staging_task('codex', 'engineering-hub', 'Pilot', 'key'))
    target = WorkTarget(item['id'], COMMIT, DIGEST)
    payload = WorkSubmission(request_id='staging', action_key='hub.validate', environment='staging',
                             target=dict(task_id=item['id'], source_commit=COMMIT, artifact_digest=DIGEST))
    job = submit_reviewed_work(repo, queue, 'codex', payload)
    assert job['state'] == 'queued' and job['environment'] == 'staging'
    for direct in [True, False]:
        with pytest.raises(HubError, match='work_target_not_verified'):
            if direct:
                queue.submit(actor='codex', request_id='production-direct', action_key='hub.validate',
                             environment='production', target=target, approval_id='approved')
            else:
                submit_reviewed_work(repo, queue, 'codex', payload.model_copy(update={
                    'request_id': 'production-http-mcp', 'environment': 'production', 'approval_id': 'approved'}))
    with pytest.raises(HubError, match='invalid_target'):
        repo.create_run('codex', item['id'], item['revision'], DIGEST, 'engineering-hub:production')
    # The scope persists across reopen and the actual claim/lease path consumes it.
    reopened = HubRepository(repo.path)
    reopened_queue = queue_for(reopened)
    assert reopened_queue.claim(worker=WorkWorker('codex', str(uuid4())))['id'] == job['id']
    # A legacy human-created task retains its original production-priority semantics.
    human = verified(repo, repo.create_task('human:42', 'engineering-hub', 'Human scope', 'human-key'))
    production = queue.submit(actor='codex', request_id='human-production', action_key='hub.validate',
        environment='production', target=WorkTarget(human['id'], COMMIT, DIGEST), approval_id='approved')
    assert production['environment'] == 'production'


def test_proposal_audit_outbox_and_receipt_rollback_together_then_concurrent_retry(tmp_path, monkeypatch):
    repo = HubRepository(tmp_path / 'hub.db')
    emit = repo._emit

    def interrupted(db, action, item):
        emit(db, action, item)
        raise RuntimeError('synthetic interrupted transaction')

    monkeypatch.setattr(repo, '_emit', interrupted)
    with pytest.raises(RuntimeError, match='interrupted transaction'):
        repo.propose_staging_task('codex', 'engineering-hub', 'Pilot', 'retry')
    assert repo.list_tasks() == repo.list_outbox() == []
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_requests').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM hub_audit').fetchone()[0] == 0
    monkeypatch.setattr(repo, '_emit', emit)
    with ThreadPoolExecutor(max_workers=6) as pool:
        tasks = list(pool.map(lambda _: repo.propose_staging_task(
            'codex', 'engineering-hub', 'Pilot', 'retry'), range(12)))
    assert len({item['id'] for item in tasks}) == 1
    assert len(repo.list_tasks()) == len(repo.list_outbox()) == 1


def test_scope_veto_rechecked_for_persisted_queued_rows_after_restart(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    item = verified(repo, repo.propose_staging_task('codex', 'engineering-hub', 'Pilot', 'key'))
    queue = queue_for(repo)
    job = queue.submit(actor='codex', request_id='stage', action_key='hub.validate', environment='staging',
                       target=WorkTarget(item['id'], COMMIT, DIGEST))
    # Simulates an older/invalid queued admission, not an allowed API mutation.
    with repo._transaction() as db:
        db.execute("UPDATE hub_work_jobs SET environment='production',approval_id='approved' WHERE id=?", (job['id'],))
    reopened = queue_for(HubRepository(repo.path))
    assert reopened.claim(worker=WorkWorker('codex', str(uuid4()))) is None
    assert reopened.get_job(job['id'])['state'] == 'queued'


def test_scope_veto_rechecked_immediately_before_owned_effect(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    item = verified(repo, repo.propose_staging_task('codex', 'engineering-hub', 'Pilot', 'key'))
    queue = queue_for(repo)
    job = queue.submit(actor='codex', request_id='stage', action_key='hub.validate', environment='staging',
                       target=WorkTarget(item['id'], COMMIT, DIGEST))
    worker = WorkWorker('codex', str(uuid4()))
    claim = queue.claim(worker=worker)
    with repo._transaction() as db:
        db.execute("UPDATE hub_work_jobs SET environment='production',approval_id='approved' WHERE id=?", (job['id'],))
    with pytest.raises(HubError, match='work_admission_changed'):
        queue.live_claim(job_id=job['id'], worker=worker, fence=claim['fence'])


@pytest.mark.parametrize('metadata', [{"proposed_by": "codex"}, {"scope": "production", "proposed_by": "codex"},
                                      {"scope": "staging", "proposed_by": "unknown"}])
def test_partial_or_invalid_proposal_metadata_never_expands_scope(tmp_path, metadata):
    repo = HubRepository(tmp_path / 'hub.db')
    item = repo.propose_staging_task('codex', 'engineering-hub', 'Pilot', 'key')
    item.pop('scope')
    item.pop('proposed_by')
    item.update(metadata)
    with repo._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), item['id']))
    for environment in ['staging', 'production']:
        assert repo.task_allows_environment(item['id'], environment) is False


def test_http_and_mcp_equivalence_and_existing_human_boundary(tmp_path):
    settings = Settings(tmp_path / 'hub.db', AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42), work_queue_enabled=True)
    app = create_app(settings)
    body = dict(project='engineering-hub', title='Agent pilot', client_request_id='same')
    headers = {'Authorization': 'Bearer test-codex'}
    with TestClient(app, base_url='https://hub.test') as client:
        assert client.post(BASE + '/staging/tasks', json=body).status_code == 401
        session = app.state.sessions.create_session(42)
        client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
        assert client.post(BASE + '/staging/tasks', headers={
            'Origin': settings.auth_config.origin, 'X-Hub-CSRF': session['csrf_token']}, json=body).status_code == 401
        assert client.post(BASE + '/tasks', headers=headers, json=body).status_code == 403
        for forbidden in ['actor', 'owner', 'environment', 'scope', 'proposed_by', 'review']:
            assert client.post(BASE + '/staging/tasks', headers=headers, json={**body, forbidden: 'production'}).status_code == 422
        response = client.post(BASE + '/staging/tasks', headers=headers, json=body)
        assert response.status_code == 201, response.text
        item = response.json()
        assert item['scope'] == 'staging' and item['proposed_by'] == 'codex' and item['owner'] is None
        rpc_headers = {**headers, 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}
        result = client.post('/mcp/', headers=rpc_headers, json={'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call', 'params': {'name': 'hub_staging_task_propose', 'arguments': body}}).json()['result']
        assert not result.get('isError')
        result_body = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        assert result_body == item
        overview = client.get(BASE + '/overview', headers=headers).json()
        assert overview['tasks'][0] == dict(item, state_since=item['updated_at'])
        schema = client.get('/openapi.json').json()['paths'][BASE + '/staging/tasks']['post']
        assert 'staging-only' in schema['summary']
        # Complete the proposal -> claim -> candidate -> independent review ->
        # coordination queue pipeline without a human-created task or executor.
        path = BASE + '/tasks/' + item['id']
        claimed = client.post(path + '/claim', headers=headers, json={'expected_revision': 1})
        assert claimed.status_code == 200
        candidate = client.post(path + '/candidate', headers=headers,
            json={'expected_revision': 2, 'commit': COMMIT})
        assert candidate.status_code == 200
        review = {'expected_revision': 3, 'commit': COMMIT, 'verdict': 'approve'}
        assert client.post(path + '/review', headers=headers, json=review).status_code == 403
        assert client.post(path + '/review', headers={'Authorization': 'Bearer test-claude'}, json=review).status_code == 200
        payload = dict(request_id='pipeline', action_key='engineering-hub.validate', environment='staging',
                       target=dict(task_id=item['id'], source_commit=COMMIT, artifact_digest=DIGEST))
        queued = client.post(BASE + '/work/jobs', headers=headers, json=payload)
        assert queued.status_code == 201 and queued.json()['state'] == 'queued'
        production = client.post(BASE + '/work/jobs', headers=headers,
            json={**payload, 'request_id': 'production-pipeline', 'environment': 'production', 'approval_id': 'not-authority'})
        assert production.status_code == 403
        # Source coordination must not impersonate installed executor admission.
        claim = client.post(BASE + '/work/claim', headers=headers, json={'instance_id': str(uuid4())})
        assert claim.status_code == 200 and claim.json() == {'job': None}
