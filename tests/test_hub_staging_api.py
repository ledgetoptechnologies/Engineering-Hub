"""Route authentication/contracts with the real same-ledger resource store."""
from datetime import datetime, timezone
import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.responses import JSONResponse

from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import AGENTS, HubError
from engineering_hub.repository import HubRepository
from engineering_hub.staging_api import attach_staging_routes
from engineering_hub.staging_preview import PreviewBroker
from engineering_hub.staging_resources import StagingResourceStore


PREFIX = '/api/hub/v1/staging'
PROJECT = 'engineering-hub'
ADMIN = 'human:1'
COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


class Controller:
    def __init__(self, resources, control):
        self.resources, self.control = resources, control
        self.calls = []

    async def status(self, actor):
        self.calls.append(('status', actor))
        return {'configured': True, 'reachable': True, 'profiles': {PROJECT: ['static']},
                'worker_url': 'https://private.invalid', 'token_file': '/private/secret'}

    async def runtime(self, actor):
        self.calls.append(('runtime', actor))
        return {'configured': True, 'state': 'ready', 'snapshot': {
            'schema': 'hub-staging-runtime.v1', 'observed_at': datetime.now(timezone.utc).isoformat(),
            'sources': [{'id': 'worker', 'state': 'unavailable', 'observed_at': None}],
            'containers': [], 'services': []}, 'worker_url': 'https://private.invalid'}

    async def submit(self, actor, payload):
        self.resources.authorize(actor, payload['project'], 'build')
        self.calls.append(('submit', actor, payload))
        return {'job_id': str(uuid.uuid4()), 'state': 'queued'}

    def get_job(self, actor, job_id):
        self.resources.authorize(actor, PROJECT, 'read')
        return {'job_id': job_id, 'state': 'queued'}

    async def reconcile_for(self, actor, job_id):
        if actor not in self.resources.admin_actors:
            raise HubError('staging_admin_required', 403)
        self.resources.authorize(actor, PROJECT, 'read')
        self.calls.append(('reconcile', actor, job_id))
        return {'job': {'id': job_id, 'state': 'held'}, 'phase': 'held', 'resource_ids': {}}

    async def cleanup_apply(self, actor, plan_id, digest):
        self.calls.append(('cleanup', actor, plan_id, digest))
        begun = self.resources.cleanup_begin(actor, plan_id, digest)
        removed = []
        if begun['dispatch']:
            for operation in begun['operations']:
                self.resources.cleanup_finish(self.control, plan_id, operation['id'],
                    operation['expected_revision'], absent=True, receipt_id=str(uuid.uuid4()))
                removed.append(operation['id'])
        return {'removed': removed, 'failed': [], 'skipped_changed': [r['id'] for r in begun['skipped']]}


@pytest.fixture
def env(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    ProjectRepository(repo)
    control = object()
    resources = StagingResourceStore(repo, admin_actors=frozenset({ADMIN}), control_principal=control)
    resources.set_policy(ADMIN, PROJECT, 0, {
        a: ['read', 'retain', 'cleanup', 'build', 'test', 'preview'] for a in (ADMIN, 'codex')})
    controller = Controller(resources, control)
    return repo, resources, control, controller


def headers(actor='codex', *, csrf=False):
    result = {'Authorization': 'Bearer ' + actor} if actor in AGENTS else {'X-Test-Human': actor}
    if csrf:
        result['X-Test-CSRF'] = 'verified'
    return result


def application(controller, *, broker=None, session=None):
    app = FastAPI()
    calls = []

    @app.exception_handler(HubError)
    async def error(request, exc):
        return JSONResponse({'code': exc.code}, status_code=exc.status)

    def actor(request, mutation=False):
        token = request.headers.get('authorization', '')
        if token.startswith('Bearer '):
            principal = token[7:]
            if principal not in AGENTS:
                raise HubError('unauthorized', 401)
        else:
            principal = request.headers.get('x-test-human')
            if principal is None:
                raise HubError('unauthorized', 401)
            if mutation and request.headers.get('x-test-csrf') != 'verified':
                raise HubError('csrf_required', 403)
        calls.append((principal, mutation))
        return principal

    def human(request, mutation=False):
        principal = actor(request, mutation)
        HubRepository._actor(principal, human=True)
        return principal

    attach_staging_routes(app, controller, actor, human, broker, session)
    return TestClient(app), calls


def resource(env, *, kind='build', expired=False):
    _, store, control, _ = env
    job = store.bind_job(control, str(uuid.uuid4()), project=PROJECT,
                         created_by='codex', source_commit=COMMIT)
    item = store.register_resource(control, job['id'], kind)
    item = store.mark_resource_ready(control, item['id'], item['revision'],
        artifact_digest=DIGEST, size_bytes=10,
        preview_path='/staging/previews/' + item['id'] if kind == 'preview' else None)
    store.update_job(control, job['id'], job['revision'], terminal=True)
    if expired:
        # Controlled fixture time, not a direct DB edit or host resource change.
        original = store.clock
        store.clock = lambda: original() + 31 * 86400
    return store.inspect_resource(control, item['id'])


def submission():
    return {'request_id': '33762456-e740-4f80-bda9-776f8dbcf753', 'project': PROJECT, 'recipe': 'static',
            'source_commit': COMMIT, 'source_digest': 'c' * 64,
            'source_zip_base64': 'eA==', 'topic': 'topic'}


def test_disabled_status_is_authenticated_and_actions_closed():
    client, _ = application(None)
    assert client.get(PREFIX + '/status').status_code == 401
    assert client.get(PREFIX + '/status', headers=headers()).json() == {
        'configured': False, 'running': False, 'reachable': False, 'profiles': {}, 'previews_configured': False}
    assert client.get(PREFIX + '/resources', headers=headers()).status_code == 503
    assert client.post(PREFIX + '/jobs', headers=headers(), json=submission()).status_code == 503


def test_status_strips_private_configuration(env):
    client, _ = application(env[3])
    response = client.get(PREFIX + '/status', headers=headers())
    assert response.status_code == 200
    assert response.json() == {'configured': True, 'running': False, 'reachable': True,
                               'profiles': {PROJECT: ['static']}, 'previews_configured': False}
    assert 'private' not in response.text and 'secret' not in response.text


def test_runtime_is_human_admin_only_before_upstream_call(env):
    client, _ = application(env[3])
    for principal in (None, 'codex', 'human:2'):
        response = client.get(PREFIX + '/runtime', headers={} if principal is None else headers(principal))
        assert response.status_code == (401 if principal is None else 403)
    assert not env[3].calls
    response = client.get(PREFIX + '/runtime', headers=headers(ADMIN))
    assert response.status_code == 200
    assert set(response.json()) == {'configured', 'state', 'snapshot'}
    assert response.json()['snapshot']['sources'][0]['state'] == 'unavailable'
    assert 'private.invalid' not in response.text
    assert env[3].calls == [('runtime', ADMIN)]


def test_runtime_not_configured_does_not_report_empty_healthy_host():
    client, _ = application(None)
    response = client.get(PREFIX + '/runtime', headers=headers(ADMIN))
    assert response.status_code == 503
    assert response.json()['code'] == 'staging_not_configured'


def test_resource_listing_pagination_scopes_and_query_validation(env):
    first = resource(env)
    second = resource(env)
    client, _ = application(env[3])
    response = client.get(PREFIX + '/resources', headers=headers(), params={'project': PROJECT, 'limit': 1})
    assert response.status_code == 200
    body = response.json()
    assert len(body['items']) == 1 and body['next_after']
    following = client.get(PREFIX + '/resources', headers=headers(), params={'after': body['next_after']}).json()
    assert {r['id'] for r in body['items'] + following['items']} == {first['id'], second['id']}
    assert client.get(PREFIX + '/resources/' + first['id'], headers=headers()).json() == first
    assert client.get(PREFIX + '/resources', headers=headers('claude'), params={'project': PROJECT}).status_code == 403
    for params in ({'limit': 101}, {'kind': 'shell'}, {'state': 'running'}, {'after': '../path'}):
        assert client.get(PREFIX + '/resources', headers=headers(), params=params).status_code == 422


def test_keep_human_csrf_agent_owner_and_strict_body(env):
    item = resource(env)
    client, calls = application(env[3])
    path = PREFIX + '/resources/' + item['id'] + '/keep'
    payload = {'mode': 'pinned', 'reason': 'review', 'expected_revision': item['revision']}
    assert client.post(path, headers=headers(ADMIN), json=payload).status_code == 403
    assert client.post(path, headers=headers(), json=payload).status_code == 403
    assert client.post(path, headers=headers(ADMIN, csrf=True), json=dict(payload, worker_url='https://foreign')).status_code == 422
    response = client.post(path, headers=headers(ADMIN, csrf=True), json=payload)
    assert response.status_code == 200 and response.json()['keep']['mode'] == 'pinned'
    assert (ADMIN, True) in calls
    assert client.post(path, headers=headers(ADMIN, csrf=True), json=payload).status_code == 409


def test_agent_can_retain_own_but_not_peer_resource(env):
    item = resource(env)
    store = env[1]
    policy = store.get_policy(ADMIN, PROJECT)
    policy['grants']['claude'] = ['read', 'retain']
    store.set_policy(ADMIN, PROJECT, policy['revision'], policy['grants'])
    client, _ = application(env[3])
    payload = {'mode': 'until', 'until': datetime.fromtimestamp(time.time() + 1000, timezone.utc).isoformat(),
               'reason': 'test', 'expected_revision': item['revision']}
    path = PREFIX + '/resources/' + item['id'] + '/keep'
    assert client.post(path, headers=headers('claude'), json=payload).status_code == 403
    assert client.post(path, headers=headers(), json=payload).status_code == 200


def test_cleanup_plan_and_apply_use_exact_actor_digest(env):
    item = resource(env, expired=True)
    client, _ = application(env[3])
    plan = client.post(PREFIX + '/cleanup/plan', headers=headers(), json={'project': PROJECT, 'kinds': ['build']}).json()
    assert plan['remove'][0]['id'] == item['id']
    assert client.post(PREFIX + '/cleanup/apply', headers=headers(), json={'plan_id': plan['plan_id'], 'digest': 'sha256:' + '0' * 64}).status_code == 409
    response = client.post(PREFIX + '/cleanup/apply', headers=headers(), json={'plan_id': plan['plan_id'], 'digest': plan['digest']})
    assert response.status_code == 200
    assert response.json() == {'removed': [item['id']], 'failed': [], 'skipped_changed': []}


def test_submit_is_agent_only_derives_actor_and_forbids_host_controls(env):
    client, _ = application(env[3])
    assert client.post(PREFIX + '/jobs', headers=headers(ADMIN, csrf=True), json=submission()).status_code == 403
    for extra in ({'actor': ADMIN}, {'resource_ids': {}}, {'command': 'bash'}, {'worker_url': 'https://x'}, {'image': 'latest'}):
        response = client.post(PREFIX + '/jobs', headers=headers(), json=dict(submission(), **extra))
        assert response.status_code == 422
        assert 'eA==' not in response.text
    response = client.post(PREFIX + '/jobs', headers=headers(), json=submission())
    assert response.status_code == 202
    assert env[3].calls[-1] == ('submit', 'codex', submission())
    assert client.get(PREFIX + '/jobs/' + response.json()['job_id'], headers=headers()).status_code == 200


def test_job_schema_rejects_coercion_and_invalid_provenance(env):
    client, _ = application(env[3])
    for changed in ({'request_id': 123}, {'project': '../unsafe'}, {'source_digest': DIGEST}, {'source_commit': 'latest'}, {'topic': ''}):
        assert client.post(PREFIX + '/jobs', headers=headers(), json=dict(submission(), **changed)).status_code == 422


def test_recheck_is_owner_human_csrf_only_and_accepts_no_release_command(env):
    client, _ = application(env[3])
    identity = str(uuid.uuid4())
    path = PREFIX + '/jobs/' + identity + '/reconcile'
    assert client.post(path, headers=headers(), json={}).status_code == 403
    assert client.post(path, headers=headers('human:2', csrf=True), json={}).status_code == 403
    assert client.post(path, headers=headers(ADMIN), json={}).status_code == 403
    assert client.post(path, headers=headers(ADMIN, csrf=True), json={'force': True}).status_code == 422
    assert client.post(path, headers=headers(ADMIN, csrf=True), json={}).json()['phase'] == 'held'
    assert env[3].calls[-1] == ('reconcile', ADMIN, identity)


def test_policy_owner_only_cas_and_human_csrf(env):
    client, _ = application(env[3])
    path = PREFIX + '/projects/' + PROJECT + '/policy'
    assert client.get(path, headers=headers()).status_code == 403
    assert client.get(path, headers=headers('human:2')).status_code == 403
    current = client.get(path, headers=headers(ADMIN)).json()
    payload = {'expected_revision': current['revision'], 'grants': {'codex': ['read']}}
    assert client.put(path, headers=headers(ADMIN), json=payload).status_code == 403
    assert client.put(path, headers=headers(ADMIN, csrf=True), json=payload).status_code == 200
    assert client.put(path, headers=headers(ADMIN, csrf=True), json=payload).status_code == 409


def test_preview_uses_real_broker_human_csrf_and_normalizes_url(env):
    item = resource(env, kind='preview')
    broker = PreviewBroker(env[0], env[1], hub_origin='https://hub.example.test',
        preview_origin='https://preview.example.test', session_alive=lambda actor, session_hash: True)
    client, _ = application(env[3], broker=broker, session=lambda request: ('d' * 64, time.time() + 600))
    path = PREFIX + '/resources/' + item['id'] + '/preview-session'
    assert client.post(path, headers=headers(), json={}).status_code == 403
    assert client.post(path, headers=headers(ADMIN), json={}).status_code == 403
    assert client.post(path, headers=headers(ADMIN, csrf=True), json={'url': 'https://foreign'}).status_code == 422
    response = client.post(path, headers=headers(ADMIN, csrf=True), json={})
    assert response.status_code == 200
    assert set(response.json()) == {'url', 'expires_at'}
    assert response.json()['url'].startswith('https://preview.example.test/p/')


def test_preview_unconfigured_is_closed(env):
    item = resource(env, kind='preview')
    client, _ = application(env[3])
    assert client.post(PREFIX + '/resources/' + item['id'] + '/preview-session',
                       headers=headers(ADMIN, csrf=True), json={}).status_code == 503


def test_duplicate_json_nonjson_and_streamed_body_bound(env):
    item = resource(env)
    client, _ = application(env[3])
    path = PREFIX + '/resources/' + item['id'] + '/keep'
    assert client.post(path, headers=headers(), content='{}').status_code == 415
    request_headers = dict(headers(), **{'Content-Type': 'application/json'})
    assert client.post(path, headers=request_headers, content='{"mode":"none","mode":"pinned"}').status_code == 400
    assert client.post(path, headers=request_headers, content='{"expected_revision":NaN}').status_code == 400
    streamed = (b' ' * 40000 for _ in range(2))
    assert client.post(path, headers=request_headers, content=streamed).status_code == 413


def test_duplicate_bearer_does_not_fall_back_to_human(env):
    client, _ = application(env[3])
    response = client.get(PREFIX + '/status', headers=[('Authorization', 'Bearer codex'),
        ('Authorization', 'Bearer claude'), ('X-Test-Human', ADMIN)])
    assert response.status_code == 401
