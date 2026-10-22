"""Real MCP Streamable HTTP queue parity, synthetic credentials and state."""
from contextlib import asynccontextmanager
import json
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.mcp import HubMCPAuthMiddleware, build_hub_mcp
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue
from engineering_hub.work_queue_api import attach_work_queue_routes


COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
TOOLS = {'hub_work_status', 'hub_work_jobs', 'hub_work_get', 'hub_work_submit',
         'hub_work_claim', 'hub_work_renew', 'hub_work_complete'}


@pytest.fixture
def rig(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    auth = AuthSettings('test-hermes', 'test-codex', claude_token='test-claude')
    actions = {'viewer.validate': WorkAction('staging-pc', frozenset({'staging', 'production'}), project='3d-viewer'),
               'viewer.verify': WorkAction('staging-pc', project='3d-viewer'),
               'tree.validate': WorkAction('staging-pc', project='treedar')}

    def target_verified(actor, action, environment, target):
        task = repo.get_task(target.task_id)
        review = task.get('review') or {}
        return (task['owner'] == actor and task['status'] == 'verified' and task['candidate_commit'] == target.source_commit
                and actions[action].project == task['project'] and review.get('commit') == target.source_commit
                and review.get('verdict') == 'approve' and review.get('actor') != actor)

    queue = WorkQueue(repo, actions, target_verifier=target_verified)

    def build(queue_instance=queue):
        mcp = build_hub_mcp(repo, auth, work_queue=queue_instance)
        child = mcp.streamable_http_app()

        @asynccontextmanager
        async def lifespan(app):
            async with mcp.session_manager.run():
                yield

        app = FastAPI(lifespan=lifespan)
        app.mount('/mcp', HubMCPAuthMiddleware(child, auth))
        @app.exception_handler(HubError)
        async def domain_error(request, error):
            return JSONResponse({'detail': {'code': error.code}}, status_code=error.status)
        def human(request, *, mutation=False):
            raise HTTPException(403)
        attach_work_queue_routes(app, repo=repo, queue=queue_instance, actor=auth.agent, agent=auth.agent, human=human)
        return TestClient(app, base_url='http://127.0.0.1:8000')

    def task(*, reviewed=True, owner='codex'):
        item = repo.create_task('human:42', '3d-viewer', 'Reviewed synthetic work', str(uuid4()))
        item = repo.claim_task(owner, item['id'], item['revision'])
        item = repo.set_candidate(owner, item['id'], item['revision'], COMMIT)
        if reviewed:
            item = repo.review_task('claude' if owner != 'claude' else 'hermes', item['id'], item['revision'], COMMIT, 'approve')
        return item

    def payload(item=None, **changes):
        item = task() if item is None else item
        body = dict(request_id=str(uuid4()), action_key='viewer.validate', environment='staging',
                    target=dict(task_id=item['id'], source_commit=COMMIT, artifact_digest=DIGEST))
        body.update(changes)
        return body

    with build() as client:
        yield client, repo, queue, build, task, payload


def rpc(client, method, params=None, token='test-codex'):
    headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    return client.post('/mcp/', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})


def call(client, tool, payload, token='test-codex'):
    response = rpc(client, 'tools/call', {'name': tool, 'arguments': {'payload': payload}}, token)
    assert response.status_code == 200, response.text
    result = response.json()['result']
    if not result.get('isError'):
        return result.get('structuredContent') or json.loads(result['content'][0]['text'])
    return result


def error(result, code):
    assert result.get('isError') is True
    assert code in str(result)


def test_mcp_filtered_claim_preserves_cross_action_machine_production_priority(rig):
    client, repo, queue, _, task, payload = rig
    queue.approval_verifier = lambda *args: True  # Synthetic approval, no executor.
    production = call(client, 'hub_work_submit', payload(
        environment='production', approval_id='synthetic-reviewed-production'))
    staging = call(client, 'hub_work_submit', payload(action_key='viewer.verify'))
    assert 'id' in production and 'id' in staging
    denied = call(client, 'hub_work_claim', {
        'instance_id': str(uuid4()), 'action_keys': ['viewer.verify']})
    assert denied['job'] is None
    assert queue.get_job(staging['id'])['state'] == 'queued'
    assert queue.get_job(production['id'])['state'] == 'queued'
    accepted = call(client, 'hub_work_claim', {
        'instance_id': str(uuid4()), 'action_keys': ['viewer.validate']}, token='test-hermes')
    assert accepted['job']['id'] == production['id']
    assert accepted['job']['machine_id'] == 'staging-pc'


def test_discovery_and_every_tool_authentication(rig):
    client = rig[0]
    tools = rpc(client, 'tools/list').json()['result']['tools']
    names = {tool['name'] for tool in tools}
    assert TOOLS <= names
    assert not {'hub_work_pause', 'hub_work_reconcile', 'hub_work_approve', 'hub_work_enroll'} & names
    for tool in tools:
        if tool['name'] in TOOLS:
            assert set(tool['inputSchema']['properties']) == {'payload'}
    for tool in TOOLS:
        for token in (None, 'invalid-token'):
            assert rpc(client, 'tools/call', {'name': tool, 'arguments': {'payload': {}}}, token).status_code == 401
    for token in ('test-codex', 'test-hermes', 'test-claude'):
        assert call(client, 'hub_work_jobs', {}, token) == {'items': []}


def test_unconfigured_mcp_queue_never_claims_readiness(rig):
    with rig[3](None) as client:
        names = {tool['name'] for tool in rpc(client, 'tools/list').json()['result']['tools']}
        assert len(names) == 13 and not TOOLS & names
        assert call(client, 'hub_work_status', {}).get('isError') is True
        response = client.get('/api/hub/v1/work/status', headers={'Authorization': 'Bearer test-codex'})
        assert response.status_code == 423
        assert response.json()['detail']['code'] == 'work_queue_not_configured'


def test_mcp_submission_claim_fence_and_proof_completion(rig):
    client, repo, queue, _, _, payload = rig
    body = payload()
    job = call(client, 'hub_work_submit', body)
    assert job['actor'] == 'codex' and job['target'] == body['target']
    assert call(client, 'hub_work_submit', body)['id'] == job['id']
    assert call(client, 'hub_work_get', {'job_id': job['id']})['id'] == job['id']
    path = '/api/hub/v1/work/jobs/' + job['id']
    assert client.get(path).status_code == 401
    assert client.get(path, headers={'Authorization': 'Bearer test-claude'}).json() == call(client, 'hub_work_get', {'job_id': job['id']})
    assert client.get('/api/hub/v1/work/jobs/' + str(uuid4()), headers={'Authorization': 'Bearer test-codex'}).status_code == 404
    instance = str(uuid4())
    claimed = call(client, 'hub_work_claim', {'instance_id': instance}, 'test-hermes')['job']
    receipt = dict(job_id=job['id'], instance_id=instance, fence=claimed['fence'])
    assert call(client, 'hub_work_renew', receipt, 'test-hermes')['worker'] == 'hermes:' + instance
    for token, bad in [('test-codex', receipt), ('test-hermes', {**receipt, 'instance_id': str(uuid4())}),
                       ('test-hermes', {**receipt, 'fence': claimed['fence'] + 1})]:
        error(call(client, 'hub_work_complete', {**bad, 'outcome': 'succeeded'}, token), 'work_ownership_lost')
    error(call(client, 'hub_work_complete', {**receipt, 'outcome': 'succeeded', 'evidence_id': 'untrusted'}, 'test-hermes'),
          'work_completion_proof_required')
    assert queue.get_job(job['id'])['state'] == 'claimed'
    queue.completion_verifier = lambda result, evidence, outcome: (
        result['id'] == job['id'] and result['target'] == body['target'] and result['fence'] == receipt['fence']
        and result['worker'] == 'hermes:' + instance and evidence == 'trusted-test-receipt' and outcome == 'succeeded')
    completed = call(client, 'hub_work_complete', {**receipt, 'outcome': 'succeeded', 'evidence_id': 'trusted-test-receipt'}, 'test-hermes')
    assert completed['state'] == 'succeeded'
    assert repo.get_task(body['target']['task_id'])['status'] == 'verified'
    assert repo.list_runs() == []


@pytest.mark.parametrize('case,code', [('unreviewed', 'review_required'), ('owner', 'forbidden'),
                                     ('project', 'work_project_mismatch'), ('commit', 'review_required')])
def test_mcp_same_shared_task_submission_guard(rig, case, code):
    client, _, _, _, task, payload = rig
    item = task(reviewed=case != 'unreviewed', owner='hermes' if case == 'owner' else 'codex')
    body = payload(item)
    if case == 'project':
        body['action_key'] = 'tree.validate'
    if case == 'commit':
        body['target']['source_commit'] = 'c' * 40
    error(call(client, 'hub_work_submit', body), code)


@pytest.mark.parametrize('tool,payload', [('hub_work_status', {'limit': True}), ('hub_work_jobs', {'limit': '50'}),
    ('hub_work_get', {'job_id': 'private-input'}), ('hub_work_claim', {'instance_id': str(uuid4()), 'actor': 'claude'}),
    ('hub_work_claim', {'instance_id': str(uuid4()), 'lease_seconds': True}),
    ('hub_work_complete', {'job_id': str(uuid4()), 'instance_id': str(uuid4()), 'fence': True, 'outcome': 'succeeded'}),
    ('hub_work_submit', {'command': 'private-input'})])
def test_mcp_closed_input_sanitizes_validation(tool, payload, rig):
    result = call(rig[0], tool, payload)
    error(result, 'invalid_input')
    assert 'private-input' not in str(result)


def test_production_remains_blocked_and_stale_review_not_claimable(rig):
    client, repo, _, _, _, payload = rig
    production = call(client, 'hub_work_submit', payload(environment='production', approval_id='ordinary-decision-id'))
    assert production['state'] == 'queued'
    assert call(client, 'hub_work_claim', {'instance_id': str(uuid4())})['job'] is None
    body = payload()
    call(client, 'hub_work_submit', body)
    item = repo.get_task(body['target']['task_id'])
    repo.set_candidate('codex', item['id'], item['revision'], 'c' * 40)
    assert call(client, 'hub_work_claim', {'instance_id': str(uuid4())})['job'] is None


@pytest.mark.parametrize('keys', [[], ['viewer.validate', 'viewer.validate'], 'viewer.validate',
                                [True], ['viewer.validate'] * 33])
def test_mcp_claim_filter_strict_shape_and_no_invented_capability(rig, keys):
    error(call(rig[0], 'hub_work_claim', {'instance_id': str(uuid4()), 'action_keys': keys}), 'invalid_input')


def test_mcp_claim_filter_narrows_but_cannot_grant_server_policy(rig):
    client, _, queue, _, _, payload = rig
    first = call(client, 'hub_work_submit', payload())
    second = call(client, 'hub_work_submit', payload(action_key='viewer.verify'))
    queue.claim_verifier = lambda worker, action, environment, machine, target: (
        worker.agent == 'hermes' and action == 'viewer.verify' and environment == 'staging' and machine == 'staging-pc'
        and target.task_id == second['target']['task_id'] and target.source_commit == COMMIT and target.artifact_digest == DIGEST)
    request = dict(instance_id=str(uuid4()), action_keys=['viewer.validate'])
    assert call(client, 'hub_work_claim', request, 'test-hermes')['job'] is None
    request['action_keys'] = ['viewer.verify']
    assert call(client, 'hub_work_claim', request, 'test-codex')['job'] is None
    result = call(client, 'hub_work_claim', request, 'test-hermes')['job']
    assert result['id'] == second['id']
    assert queue.get_job(first['id'])['state'] == 'queued'


def test_mcp_claim_filter_cannot_grant_unenrolled_artifact(rig):
    client, _, queue, _, _, payload = rig
    body = payload(action_key='viewer.verify')
    body['target']['artifact_digest'] = 'sha256:' + 'd' * 64
    job = call(client, 'hub_work_submit', body)
    observed = []
    def admission(worker, action, environment, machine, target):
        observed.append(target)
        return action == 'viewer.verify' and target.source_commit == COMMIT and target.artifact_digest == DIGEST
    queue.claim_verifier = admission
    assert call(client, 'hub_work_claim', {'instance_id': str(uuid4()), 'action_keys': ['viewer.verify']})['job'] is None
    assert observed and observed[-1].artifact_digest == body['target']['artifact_digest']
    assert queue.get_job(job['id'])['state'] == 'queued'
