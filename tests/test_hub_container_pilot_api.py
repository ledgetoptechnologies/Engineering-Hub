"""Container-pilot API/dispatcher unit integration, not actual Docker acceptance."""
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.container_pilot_client import ContainerPilotClient
from engineering_hub.dispatcher import StagingDispatcher
from engineering_hub.fixture_workflow import FixtureWorkflow
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_container_pilot_manifest import make_manifest_data

BASE = '/api/hub/v1'
PILOT = BASE + '/container-pilot/runs'
COMMIT = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64
ROLLBACK = 'sha256:' + 'c' * 64
SECRET = 'synthetic-pilot-only-secret'
CODEX = {'Authorization': 'Bearer fixture-codex'}
CLAUDE = {'Authorization': 'Bearer fixture-claude'}
HERMES = {'Authorization': 'Bearer fixture-hermes'}


def pilot_manifest():
    data = make_manifest_data()
    data['candidate']['image_config_id'] = IMAGE
    data['candidate']['source_commit'] = COMMIT
    data['rollback']['image_config_id'] = ROLLBACK
    data['rollback']['source_commit'] = 'f' * 40
    return PilotManifest.from_bytes(json.dumps(data).encode('utf-8'))


MANIFEST = pilot_manifest().digest()


def prepare(tmp_path, *, fail_health=False):
    settings = Settings(tmp_path / 'hub.sqlite3',
                        AuthSettings('fixture-hermes', 'fixture-codex', claude_token='fixture-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    repo = HubRepository(settings.database, execution_principal='executor:container-pilot')
    commands = []

    def transport(request):
        assert request.method == 'POST'
        assert request.url.path == '/v1/container-pilot/commands'
        assert request.headers['Authorization'] == f'Bearer {SECRET}'
        payload = json.loads(request.content)
        commands.append(payload)
        rollback = payload['action'] in {'rollback_container', 'smoke_container_rollback'}
        failed = fail_health and payload['action'] == 'smoke_container'
        return httpx.Response(200, json=payload | {
            'state': 'failed' if failed else 'succeeded',
            'reason': 'health_failed' if failed else None, 'replayed': False,
            'evidence': {'container_id': ('f' if rollback else 'e') * 64,
                         'image_config_id': ROLLBACK if rollback else IMAGE,
                         'health': 'unhealthy' if failed else 'healthy',
                         'evidence_verified': True},
        })

    adapter = ContainerPilotClient('https://pilot-adapter.test', SECRET,
                                  transport=httpx.MockTransport(transport))
    target = ContainerPilotTarget.from_manifest(pilot_manifest())
    workflow = ContainerPilotWorkflow(repo, adapter, [target])
    return SimpleNamespace(settings=settings, repo=repo, adapter=adapter, target=target,
                           workflow=workflow, commands=commands)


def human(client, app):
    session = app.state.sessions.create_session(42)
    client.cookies.set('hub_session', session['cookie_token'])
    return {'Origin': 'https://hub.test', 'X-Hub-CSRF': session['csrf_token']}


def task_through_http(client, app, *, review=True):
    payload = {'project': 'engineering-hub', 'title': 'Trusted container pilot',
               'client_request_id': 'pilot-task'}
    assert client.post(BASE + '/tasks', headers=CODEX, json=payload).status_code == 403
    response = client.post(BASE + '/tasks', headers=human(client, app), json=payload)
    assert response.status_code == 201
    task = response.json()
    response = client.post(f"{BASE}/tasks/{task['id']}/claim", headers=CODEX,
                           json={'expected_revision': task['revision']})
    assert response.status_code == 200
    task = response.json()
    response = client.post(f"{BASE}/tasks/{task['id']}/candidate", headers=CODEX,
                           json={'expected_revision': task['revision'], 'commit': COMMIT})
    assert response.status_code == 200
    task = response.json()
    if review:
        response = client.post(f"{BASE}/tasks/{task['id']}/review", headers=CLAUDE,
                               json={'expected_revision': task['revision'], 'commit': COMMIT,
                                     'verdict': 'approve'})
        assert response.status_code == 200
        task = response.json()
    return task


def run_payload(task, **changes):
    return {'task_id': task['id'], 'expected_revision': task['revision'],
            'artifact_digest': IMAGE, 'target': 'engineering-hub:staging'} | changes


def test_default_pilot_is_closed_and_cannot_be_enabled_by_request_or_environment(tmp_path, monkeypatch):
    harness = prepare(tmp_path)
    monkeypatch.setenv('HUB_CONTAINER_PILOT_ENABLED', 'true')
    monkeypatch.setenv('HUB_EXECUTOR_URL', 'https://pilot-adapter.test')
    app = create_app(harness.settings, repository=harness.repo)
    with TestClient(app, base_url='https://hub.test') as client:
        try:
            task = task_through_http(client, app)
            client.cookies.clear()
            for path, payload in ((PILOT, run_payload(task)), (PILOT + '/not-a-run/dispatch', None)):
                assert client.post(path, json=payload).status_code == 401
                assert client.post(path, headers=CODEX, json=payload).status_code == 423
            assert client.post(PILOT, headers=CODEX, json=run_payload(task, enabled=True)).status_code == 422
            caps = client.get(BASE + '/overview', headers=CODEX).json()['capabilities']
            assert caps['container_pilot_dispatch'] is False
            assert harness.commands == [] and harness.repo.list_runs() == []
        finally:
            client.portal.call(harness.adapter.aclose)


async def test_injection_requires_exact_workflow_repository_and_excludes_fixture(tmp_path):
    harness = prepare(tmp_path)
    other = HubRepository(tmp_path / 'other.sqlite3', execution_principal='executor:container-pilot')
    with pytest.raises(ValueError, match='container_pilot_repository_mismatch'):
        create_app(harness.settings, repository=other, container_pilot_workflow=harness.workflow)
    with pytest.raises(ValueError, match='container_pilot_repository_mismatch'):
        create_app(harness.settings, repository=harness.repo,
                   container_pilot_workflow=SimpleNamespace(repo=harness.repo))
    fixture = FixtureWorkflow(harness.repo, harness.adapter, [])
    with pytest.raises(ValueError, match='one_staging_workflow_required'):
        create_app(harness.settings, repository=harness.repo, fixture_workflow=fixture,
                   container_pilot_workflow=harness.workflow)
    no_identity = HubRepository(tmp_path / 'no-executor.sqlite3')
    with pytest.raises(ValueError, match='execution_identity_required'):
        ContainerPilotWorkflow(no_identity, harness.adapter, [harness.target])
    await harness.adapter.aclose()


@pytest.mark.parametrize('fail_health', [False, True])
def test_http_reviewed_pilot_is_owner_only_with_separate_fixture_namespace(tmp_path, fail_health):
    harness = prepare(tmp_path, fail_health=fail_health)
    app = create_app(harness.settings, repository=harness.repo, container_pilot_workflow=harness.workflow)
    with TestClient(app, base_url='https://hub.test') as client:
        try:
            task = task_through_http(client, app)
            response = client.post(PILOT, headers=CODEX, json=run_payload(task))
            assert response.status_code == 201
            run = response.json()
            assert run['execution_manifest_digest'] == MANIFEST
            assert harness.commands == []
            path = PILOT + f"/{run['id']}/dispatch"
            assert client.post(path, headers=HERMES).status_code == 403
            assert client.post(BASE + '/runs', headers=CODEX, json=run_payload(task)).status_code == 423
            assert client.post(BASE + f"/runs/{run['id']}/dispatch", headers=CODEX).status_code == 423
            response = client.post(path, headers=CODEX)
            assert response.status_code == 200
            assert response.json()['status'] == ('rolled_back' if fail_health else 'succeeded')
            expected = ['deploy_container', 'smoke_container']
            if fail_health:
                expected += ['rollback_container', 'smoke_container_rollback']
            assert [item['action'] for item in harness.commands] == expected
            assert all(item['run_id'] == run['id'] and item['manifest_digest'] == MANIFEST
                       for item in harness.commands)
            assert len({item['command_id'] for item in harness.commands}) == len(expected)
            assert client.post(path, headers=CODEX).json()['status'] == response.json()['status']
            assert len(harness.commands) == len(expected)
            caps = client.get(BASE + '/overview', headers=CODEX).json()['capabilities']
            assert caps['container_pilot_dispatch'] is True and caps['fixture_dispatch'] is False
            assert caps['production'] is False and caps['processing'] is False
        finally:
            client.portal.call(harness.adapter.aclose)


def test_unreviewed_stale_revision_and_manifest_mismatch_cannot_post_commands(tmp_path):
    harness = prepare(tmp_path)
    app = create_app(harness.settings, repository=harness.repo, container_pilot_workflow=harness.workflow)
    with TestClient(app, base_url='https://hub.test') as client:
        try:
            task = task_through_http(client, app, review=False)
            assert client.post(PILOT, headers=CODEX, json=run_payload(task)).status_code == 409
            review = client.post(f"{BASE}/tasks/{task['id']}/review", headers=CLAUDE,
                                json={'expected_revision': task['revision'], 'commit': 'b' * 40,
                                      'verdict': 'approve'})
            assert review.status_code == 409
            reviewed = client.post(f"{BASE}/tasks/{task['id']}/review", headers=CLAUDE,
                                  json={'expected_revision': task['revision'], 'commit': COMMIT,
                                        'verdict': 'approve'}).json()
            assert client.post(PILOT, headers=CODEX, json=run_payload(task)).status_code == 409
            assert client.post(PILOT, headers=CODEX,
                               json=run_payload(reviewed, artifact_digest=ROLLBACK)).status_code == 403
            assert harness.commands == [] and harness.repo.list_runs() == []
        finally:
            client.portal.call(harness.adapter.aclose)


async def test_dispatcher_startup_catchup_and_reopen_do_not_resubmit(tmp_path):
    harness = prepare(tmp_path)
    task = harness.repo.create_task('human:42', 'engineering-hub', 'Startup pilot', 'startup')
    task = harness.repo.claim_task('codex', task['id'], task['revision'])
    task = harness.repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    task = harness.repo.review_task('claude', task['id'], task['revision'], COMMIT, 'approve')
    try:
        dispatcher = StagingDispatcher(harness.repo, harness.workflow)
        result = await dispatcher.drain_batch()
        assert result['dispatched'] == 1
        assert [item['action'] for item in harness.commands] == ['deploy_container', 'smoke_container']
        assert len(harness.repo.list_runs()) == 1
        assert dispatcher.receipts()[0]['state'] == 'succeeded'
        reopened_repo = HubRepository(harness.settings.database, execution_principal='executor:container-pilot')
        reopened_workflow = ContainerPilotWorkflow(reopened_repo, harness.adapter, [harness.target])
        reopened = StagingDispatcher(reopened_repo, reopened_workflow)
        assert (await reopened.drain_batch())['dispatched'] == 0
        assert (await reopened.drain_batch())['dispatched'] == 0
        assert len(harness.commands) == 2 and len(reopened_repo.list_runs()) == 1
        assert reopened.receipts()[0]['entity_id'] == task['id']
    finally:
        await harness.adapter.aclose()


@pytest.mark.parametrize('crash_phase', ['local_run_receipt', 'completed_before_record'])
async def test_dispatcher_explicit_recovery_uses_exact_pilot_receipt(tmp_path, crash_phase):
    """Simulated process interruption, not a real SIGKILL/Docker crash test."""
    class SimulatedProcessExit(BaseException):
        pass

    harness = prepare(tmp_path)
    task = harness.repo.create_task('human:42', 'engineering-hub', 'Recover pilot', 'recover')
    task = harness.repo.claim_task('codex', task['id'], task['revision'])
    task = harness.repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    harness.repo.review_task('claude', task['id'], task['revision'], COMMIT, 'approve')
    dispatcher = StagingDispatcher(harness.repo, harness.workflow)
    original_create = harness.workflow.create_run
    original_record = dispatcher._record

    def crash_after_local_receipt(*args, **kwargs):
        original_create(*args, **kwargs)
        raise SimulatedProcessExit()

    def crash_before_completion_record(reservation, state, **kwargs):
        if state == 'succeeded':
            raise SimulatedProcessExit()
        return original_record(reservation, state, **kwargs)

    if crash_phase == 'local_run_receipt':
        harness.workflow.create_run = crash_after_local_receipt
    else:
        dispatcher._record = crash_before_completion_record
    try:
        with pytest.raises(SimulatedProcessExit):
            await dispatcher.drain_batch()
        original_run = harness.repo.list_runs()[0]
        original_fence = original_run['fencing_generation']
        receipt = dispatcher.receipts()[0]
        assert receipt['needs_reconciliation'] is True
        if crash_phase == 'local_run_receipt':
            assert receipt['run_id'] is None and receipt['state'] == 'reserved'
            assert harness.commands == [] and original_run['status'] == 'pending'
        else:
            assert receipt['run_id'] == original_run['id']
            assert len(harness.commands) == 2 and original_run['status'] == 'succeeded'

        # Reopen actual durable repository/client workflow state. Catchup alone
        # cannot steal a reservation, regardless of the original process age.
        repo = HubRepository(harness.settings.database, execution_principal='executor:container-pilot')
        workflow = ContainerPilotWorkflow(repo, harness.adapter, [harness.target])
        reopened = StagingDispatcher(repo, workflow)
        before = len(harness.commands)
        assert (await reopened.drain_batch())['dispatched'] == 0
        assert len(harness.commands) == before
        with pytest.raises(HubError, match='dispatch_quiescence_required'):
            await reopened.reconcile(receipt['sequence'], expected_owner=receipt['owner'])
        with pytest.raises(HubError, match='dispatch_reservation_conflict'):
            await reopened.reconcile(receipt['sequence'], expected_owner='not-the-owner',
                                     confirmed_quiescent=True)
        assert len(harness.commands) == before

        recovered = await reopened.reconcile(receipt['sequence'], expected_owner=receipt['owner'],
                                             confirmed_quiescent=True)
        assert recovered['state'] == 'succeeded' and recovered['run_id'] == original_run['id']
        assert len(repo.list_runs()) == 1
        assert repo.get_run(original_run['id'])['fencing_generation'] == original_fence
        assert len(harness.commands) == 2
        assert len({item['command_id'] for item in harness.commands}) == 2
        assert all(item['run_id'] == original_run['id']
                   and item['fencing_generation'] == original_fence for item in harness.commands)
        again = await reopened.reconcile(receipt['sequence'], expected_owner=recovered['owner'],
                                         confirmed_quiescent=True)
        assert again['state'] == 'succeeded' and again['run_id'] == original_run['id']
        assert len(harness.commands) == 2 and len(repo.list_runs()) == 1
    finally:
        await harness.adapter.aclose()
