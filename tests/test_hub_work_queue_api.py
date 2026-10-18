"""ASGI queue endpoints with real local repository/auth and synthetic tokens."""
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.auth import AuthConfig, SessionStore, require_human
from engineering_hub.repository import AGENTS, HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue
from engineering_hub.work_queue_api import attach_work_queue_routes


BASE = '/api/hub/v1/work'
COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def bearer(name='codex'):
    return {'Authorization': 'Bearer test-' + name}


@pytest.fixture
def rig(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    auth = AuthSettings('test-hermes', 'test-codex', claude_token='test-claude')
    config = AuthConfig('https://hub.test', beau_github_id=42)
    sessions = SessionStore(repo.path, repository=repo)
    actions = {'viewer.check': WorkAction('staging-pc', frozenset({'staging', 'production'}), project='3d-viewer', requires_execution_proof=False),
               'tree.check': WorkAction('staging-pc', project='treedar', requires_execution_proof=False),
               'viewer.execute': WorkAction('staging-pc', project='3d-viewer')}

    def target_verifier(principal, action, environment, target):
        task = repo.get_task(target.task_id)
        review = task.get('review')
        return (task['owner'] == principal and task['status'] == 'verified'
                and task['candidate_commit'] == target.source_commit
                and actions[action].project == task['project']
                and type(review) is dict and review.get('verdict') == 'approve'
                and review.get('actor') in AGENTS and review.get('actor') != principal
                and review.get('commit') == target.source_commit)

    queue = WorkQueue(repo, actions, target_verifier=target_verifier)

    def build(queue_instance=queue):
        app = FastAPI()

        @app.exception_handler(HubError)
        async def domain(request, error):
            return JSONResponse({'detail': {'code': error.code}}, status_code=error.status)

        @app.exception_handler(RequestValidationError)
        async def invalid(request, error):
            return JSONResponse({'detail': {'code': 'invalid_input'}}, status_code=422)

        def human(request, *, mutation=False):
            if request.headers.get('authorization'):
                raise HTTPException(403)
            return require_human(request, config, sessions, mutation=mutation)

        def actor(request, *, mutation=False):
            return auth.agent(request) if request.headers.get('authorization') else human(request, mutation=mutation)

        attach_work_queue_routes(app, repo=repo, queue=queue_instance, actor=actor, agent=auth.agent, human=human)
        return TestClient(app, base_url=config.origin)

    def task(*, reviewed=True, owner='codex', project='3d-viewer'):
        item = repo.create_task('human:42', project, 'Synthetic reviewed work', str(uuid4()))
        item = repo.claim_task(owner, item['id'], item['revision'])
        item = repo.set_candidate(owner, item['id'], item['revision'], COMMIT)
        if reviewed:
            item = repo.review_task('claude' if owner != 'claude' else 'hermes', item['id'], item['revision'], COMMIT, 'approve')
        return item

    def submission(item=None, **changes):
        item = task() if item is None else item
        result = dict(request_id=str(uuid4()), action_key='viewer.check', environment='staging',
                      target=dict(task_id=item['id'], source_commit=COMMIT, artifact_digest=DIGEST))
        result.update(changes)
        return result

    with build() as client:
        yield client, repo, queue, build, task, submission, sessions, config


def test_reads_require_identity_and_all_agents_can_read(rig):
    client = rig[0]
    for path in ('/status', '/jobs'):
        assert client.get(BASE + path).status_code == 401
        assert client.get(BASE + path, headers={'Authorization': 'Bearer invalid'}).status_code == 401
        for name in AGENTS:
            assert client.get(BASE + path, headers=bearer(name)).status_code == 200


def test_unconfigured_returns423_after_auth_for_all_routes(rig):
    _, _, _, build, _, submission, *_ = rig
    with build(None) as client:
        assert client.get(BASE + '/status').status_code == 401
        for path in ('/status', '/jobs'):
            response = client.get(BASE + path, headers=bearer())
            assert response.status_code == 423
            assert response.json()['detail']['code'] == 'work_queue_not_configured'
        assert client.post(BASE + '/jobs', headers=bearer(), json=submission()).status_code == 423
        assert client.post(BASE + '/claim', headers=bearer(), json={'instance_id': str(uuid4())}).status_code == 423


@pytest.mark.parametrize('header', [[('Authorization', 'Bearer test-codex'), ('Authorization', 'Bearer test-hermes')],
                                    {'Authorization': 'Bearer  test-codex'}, {'Authorization': 'Basic test-codex'},
                                    {'Authorization': 'Bearer test-codex, Bearer test-hermes'}])
def test_malformed_or_duplicate_authorization_never_falls_back(rig, header):
    client, *_, sessions, config = rig
    session = sessions.create_session(42)
    client.cookies.set(config.cookie_name, session['cookie_token'])
    assert client.get(BASE + '/status', headers=header).status_code == 401


def test_reviewed_submission_and_instance_fenced_lifecycle(rig):
    client, repo, _, _, _, submission, *_ = rig
    payload = submission()
    job = client.post(BASE + '/jobs', headers=bearer(), json=payload)
    assert job.status_code == 201, job.text
    job = job.json()
    assert job['target'] == payload['target']
    assert client.post(BASE + '/jobs', headers=bearer(), json=payload).json()['id'] == job['id']
    instance = str(uuid4())
    claimed = client.post(BASE + '/claim', headers=bearer('hermes'), json={'instance_id': instance}).json()['job']
    assert claimed['id'] == job['id']
    receipt = dict(instance_id=instance, fence=claimed['fence'])
    assert client.post(BASE + f"/jobs/{job['id']}/renew", headers=bearer('hermes'), json=receipt).status_code == 200
    for principal, bad in [('codex', receipt), ('hermes', {**receipt, 'instance_id': str(uuid4())}),
                           ('hermes', {**receipt, 'fence': claimed['fence'] + 1})]:
        response = client.post(BASE + f"/jobs/{job['id']}/complete", headers=bearer(principal), json={**bad, 'outcome': 'succeeded'})
        assert response.status_code == 409
    result = client.post(BASE + f"/jobs/{job['id']}/complete", headers=bearer('hermes'), json={**receipt, 'outcome': 'succeeded'})
    assert result.status_code == 200
    assert result.json()['state'] == 'succeeded'
    assert repo.get_task(payload['target']['task_id'])['status'] == 'verified'
    assert repo.list_runs() == []  # Worker status does not execute or promote.


@pytest.mark.parametrize('case', ['unreviewed', 'other_owner', 'project', 'commit', 'missing'])
def test_submit_binds_owned_reviewed_task_and_registered_project(rig, case):
    client, _, _, _, task, submission, *_ = rig
    item = task(reviewed=case != 'unreviewed', owner='hermes' if case == 'other_owner' else 'codex')
    payload = submission(item)
    if case == 'project':
        payload['action_key'] = 'tree.check'
    if case == 'commit':
        payload['target']['source_commit'] = 'c' * 40
    if case == 'missing':
        payload['target']['task_id'] = str(uuid4())
    response = client.post(BASE + '/jobs', headers=bearer(), json=payload)
    assert response.status_code in {403, 404, 409}
    assert client.get(BASE + '/jobs', headers=bearer()).json()['items'] == []


def test_production_priority_is_not_approval_and_claim_rechecks_review(rig):
    client, repo, _, _, _, submission, *_ = rig
    prod = submission(environment='production', approval_id='not-real-approval')
    assert client.post(BASE + '/jobs', headers=bearer(), json=prod).status_code == 201
    assert client.post(BASE + '/claim', headers=bearer(), json={'instance_id': str(uuid4())}).json()['job'] is None
    staging = submission()
    assert client.post(BASE + '/jobs', headers=bearer(), json=staging).status_code == 201
    current = repo.get_task(staging['target']['task_id'])
    repo.set_candidate('codex', current['id'], current['revision'], 'c' * 40)
    assert client.post(BASE + '/claim', headers=bearer(), json={'instance_id': str(uuid4())}).json()['job'] is None


@pytest.mark.parametrize('evidence', [None, 'untrusted-evidence'])
def test_executable_completion_without_verified_proof_keeps_machine_reserved(rig, evidence):
    client, _, queue, _, _, submission, *_ = rig
    created = client.post(BASE + '/jobs', headers=bearer(), json=submission(action_key='viewer.execute')).json()
    instance = str(uuid4())
    claimed = client.post(BASE + '/claim', headers=bearer(), json={'instance_id': instance}).json()['job']
    body = dict(instance_id=instance, fence=claimed['fence'], outcome='succeeded')
    if evidence is not None:
        body['evidence_id'] = evidence
    response = client.post(BASE + f"/jobs/{created['id']}/complete", headers=bearer(), json=body)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'work_completion_proof_required'
    assert queue.status()['jobs'][0]['state'] == 'claimed'
    client.post(BASE + '/jobs', headers=bearer(), json=submission())
    assert client.post(BASE + '/claim', headers=bearer('hermes'), json={'instance_id': str(uuid4())}).json()['job'] is None


def test_explicit_completion_verifier_binds_full_job_evidence_and_outcome(rig):
    client, _, queue, _, _, submission, *_ = rig
    payload = submission(action_key='viewer.execute')
    created = client.post(BASE + '/jobs', headers=bearer(), json=payload).json()
    instance = str(uuid4())
    claimed = client.post(BASE + '/claim', headers=bearer(), json={'instance_id': instance}).json()['job']
    observed = []

    def proof(job, evidence, outcome):
        observed.append((job, evidence, outcome))
        return (job['id'] == created['id'] and job['target'] == payload['target']
                and job['action_key'] == payload['action_key'] and job['environment'] == 'staging'
                and job['worker'] == 'codex:' + instance and job['fence'] == claimed['fence']
                and evidence == 'independently-enrolled-receipt' and outcome == 'succeeded')

    queue.completion_verifier = proof  # Explicit test enrollment, never an HTTP route.
    path = BASE + f"/jobs/{created['id']}/complete"
    body = dict(instance_id=instance, fence=claimed['fence'], outcome='succeeded', evidence_id='wrong')
    assert client.post(path, headers=bearer(), json=body).status_code == 409
    body['evidence_id'] = 'independently-enrolled-receipt'
    assert client.post(path, headers=bearer(), json=body).status_code == 200
    assert len(observed) == 2
    client.post(BASE + '/jobs', headers=bearer(), json=submission())
    assert client.post(BASE + '/claim', headers=bearer('hermes'), json={'instance_id': str(uuid4())}).json()['job'] is not None


def test_pause_requires_human_origin_csrf_not_agent(rig):
    client, _, _, _, _, _, sessions, config = rig
    assert client.post(BASE + '/pause', headers=bearer(), json={'paused': True}).status_code == 403
    session = sessions.create_session(42)
    client.cookies.set(config.cookie_name, session['cookie_token'])
    assert client.get(BASE + '/status').status_code == 200
    assert client.post(BASE + '/pause', json={'paused': True}).status_code == 403
    headers = {'Origin': config.origin, 'X-Hub-CSRF': session['csrf_token']}
    assert client.post(BASE + '/pause', headers=headers, json={'paused': True}).status_code == 200
    assert client.get(BASE + '/status').json()['paused'] is True
    assert client.post(BASE + '/claim', json={'instance_id': str(uuid4())}).status_code == 401


@pytest.mark.parametrize('change', [{'actor': 'hermes'}, {'command': 'docker run anything'},
                                   {'action_key': '/tmp/path'}, {'environment': 'dev'},
                                   {'approval_id': 'a' * 129}])
def test_closed_submission_schema_rejects_extra_authority(rig, change):
    payload = rig[5]()
    payload.update(change)
    assert rig[0].post(BASE + '/jobs', headers=bearer(), json=payload).status_code == 422


@pytest.mark.parametrize('body', [{'instance_id': str(uuid4()), 'worker': 'claude'},
                                 {'instance_id': 'A' * 36}, {'instance_id': str(uuid4()), 'lease_seconds': True},
                                 {'instance_id': str(uuid4()), 'lease_seconds': 601}])
def test_closed_claim_schema_does_not_accept_worker_identity(rig, body):
    assert rig[0].post(BASE + '/claim', headers=bearer(), json=body).status_code == 422


@pytest.mark.parametrize('keys', [[], ['viewer.check', 'viewer.check'], 'viewer.check',
                                [True], ['viewer.check'] * 33, ['/private/command']])
def test_claim_capability_filter_is_bounded_strict_and_unique(rig, keys):
    response = rig[0].post(BASE + '/claim', headers=bearer(), json={
        'instance_id': str(uuid4()), 'action_keys': keys})
    assert response.status_code == 422


def test_claim_filter_can_only_narrow_server_enrolled_admission(rig):
    client, _, queue, _, _, submission, *_ = rig
    first = client.post(BASE + '/jobs', headers=bearer(), json=submission()).json()
    second = client.post(BASE + '/jobs', headers=bearer(), json=submission(action_key='viewer.execute')).json()
    queue.claim_verifier = lambda worker, action, environment, machine, target: (
        worker.agent == 'hermes' and action == 'viewer.execute' and environment == 'staging' and machine == 'staging-pc'
        and target.task_id == second['target']['task_id'] and target.source_commit == COMMIT and target.artifact_digest == DIGEST)
    instance = str(uuid4())
    request = dict(instance_id=instance, action_keys=['viewer.check'])
    assert client.post(BASE + '/claim', headers=bearer('hermes'), json=request).json()['job'] is None
    request['action_keys'] = ['viewer.execute']
    assert client.post(BASE + '/claim', headers=bearer('codex'), json=request).json()['job'] is None
    request['action_keys'] = ['unregistered.validate']
    assert client.post(BASE + '/claim', headers=bearer('hermes'), json=request).status_code == 403
    request['action_keys'] = ['viewer.execute']
    result = client.post(BASE + '/claim', headers=bearer('hermes'), json=request).json()['job']
    assert result['id'] == second['id']
    assert queue.get_job(first['id'])['state'] == 'queued'
    # Filtering is claim-only, never silently accepted on renew/completion.
    response = client.post(BASE + f"/jobs/{result['id']}/renew", headers=bearer('hermes'),
                           json={**request, 'fence': result['fence']})
    assert response.status_code == 422


def test_claim_filter_does_not_grant_unenrolled_exact_target(rig):
    client, _, queue, _, _, submission, *_ = rig
    body = submission(action_key='viewer.execute')
    body['target']['artifact_digest'] = 'sha256:' + 'd' * 64
    job = client.post(BASE + '/jobs', headers=bearer(), json=body).json()
    observed = []
    def admission(worker, action, environment, machine, target):
        observed.append(target)
        return action == 'viewer.execute' and target.source_commit == COMMIT and target.artifact_digest == DIGEST
    queue.claim_verifier = admission
    response = client.post(BASE + '/claim', headers=bearer('hermes'), json={
        'instance_id': str(uuid4()), 'action_keys': ['viewer.execute']})
    assert response.status_code == 200 and response.json()['job'] is None
    assert observed and observed[-1].artifact_digest == body['target']['artifact_digest']
    assert queue.get_job(job['id'])['state'] == 'queued'
