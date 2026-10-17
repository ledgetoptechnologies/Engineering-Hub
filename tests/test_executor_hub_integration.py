"""Real loopback HTTP integration with the pinned Codex Hub implementation.

Set PYTHONPATH to this worktree plus the exact Hub checkout's src/. The
executor-only source branch has no management package; that case is explicitly
skipped, never counted as integration acceptance.
"""
import threading
from contextlib import contextmanager

import pytest

pytest.importorskip('engineering_hub')
from engineering_hub.executor_client import ExecutorClient
from engineering_hub.fixture_workflow import FixtureTarget, FixtureWorkflow
from engineering_hub.repository import HubRepository
from hub_executor.fixtures import (
    FIXTURE_TARGET, FIXTURE_CANDIDATE_DIGEST, FIXTURE_CANDIDATE_COMMIT,
    FIXTURE_ROLLBACK_DIGEST, FIXTURE_ROLLBACK_COMMIT, make_fixture_executor,
)
from hub_executor.http import make_server

TOKEN = 'isolated-fixture-controller-not-a-live-secret'


@contextmanager
def actual_http(executor):
    server = make_server(executor, TOKEN)
    server.daemon_threads = False
    server.block_on_close = True
    calls = []
    original = server.RequestHandlerClass

    class TracedHandler(original):
        def do_POST(self):
            calls.append(('POST', self.path))
            return super().do_POST()

        def do_GET(self):
            calls.append(('GET', self.path))
            return super().do_GET()

    server.RequestHandlerClass = TracedHandler
    thread = threading.Thread(target=server.serve_forever, daemon=False)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def ready_run(root):
    repo = HubRepository(root / 'hub.sqlite3', execution_principal='executor:fixture')
    task = repo.create_task('human:42', '3d-viewer', 'Tiny fixture integration', 'fixture-one')
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], FIXTURE_CANDIDATE_COMMIT)
    task = repo.review_task('claude', task['id'], task['revision'], FIXTURE_CANDIDATE_COMMIT, 'approve')
    run = repo.create_run('codex', task['id'], task['revision'], FIXTURE_CANDIDATE_DIGEST, FIXTURE_TARGET)
    return repo, run


def configured_target():
    return FixtureTarget(FIXTURE_TARGET, FIXTURE_CANDIDATE_DIGEST,
                         FIXTURE_CANDIDATE_COMMIT, FIXTURE_ROLLBACK_DIGEST,
                         FIXTURE_ROLLBACK_COMMIT)


@pytest.mark.asyncio
async def test_actual_http_workflow_uses_one_run_generation_for_deploy_and_smoke(tmp_path):
    executor = make_fixture_executor(tmp_path / 'executor')
    repo, run = ready_run(tmp_path)
    with actual_http(executor) as (origin, calls):
        async with ExecutorClient(origin, TOKEN) as client:
            workflow = FixtureWorkflow(repo, client, [configured_target()])
            result = await workflow.advance(run['id'])
            assert result['status'] == 'succeeded'
            assert (await workflow.advance(run['id']))['status'] == 'succeeded'
    assert calls.count(('POST', '/v1/commands')) == 2
    assert executor._read_observed(FIXTURE_TARGET)['effect_count'] == 2


def subclass_executor(root, executor_type):
    import hub_executor.fixtures as f
    return executor_type(
        root / 'ledger.sqlite3', {f.FIXTURE_TARGET: root / 'targets/3d-viewer-staging'},
        f.FIXTURE_ARTIFACTS, candidate_commit=f.FIXTURE_CANDIDATE_COMMIT,
        artifact_commits=f.FIXTURE_ARTIFACT_COMMITS,
        artifact_roles=f.FIXTURE_ARTIFACT_ROLES, policy_revision=1,
    )


@pytest.mark.asyncio
async def test_actual_http_smoke_state_failure_rolls_back_pinned_fixture(tmp_path):
    import json
    from hub_executor.core import HubExecutor

    class CorruptBeforeSmoke(HubExecutor):
        def execute_command(self, command):
            if command['action'] == 'smoke_fixture':
                path = self.target_roots[FIXTURE_TARGET] / 'state.json'
                state = json.loads(path.read_text())
                state['active_artifact_digest'] = FIXTURE_ROLLBACK_DIGEST
                state['artifact_bytes'] = 'engineering hub fixture v1\n'
                path.write_text(json.dumps(state))  # Explicit test-only corruption.
            return super().execute_command(command)

    executor = subclass_executor(tmp_path / 'executor', CorruptBeforeSmoke)
    repo, run = ready_run(tmp_path)
    with actual_http(executor) as (origin, calls):
        async with ExecutorClient(origin, TOKEN) as client:
            result = await FixtureWorkflow(repo, client, [configured_target()]).advance(run['id'])
    assert result['status'] == 'rolled_back'
    assert calls.count(('POST', '/v1/commands')) == 3
    assert executor._read_observed(FIXTURE_TARGET)['active_artifact_digest'] == FIXTURE_ROLLBACK_DIGEST
    assert repo.get_task(run['task_id'])['repair_count'] == 1


@pytest.mark.asyncio
async def test_actual_http_lost_response_holds_then_get_inspects_without_repost(tmp_path):
    from hub_executor.core import HubExecutor
    release = threading.Event()

    class SlowResponseAfterEffect(HubExecutor):
        def execute_command(self, command):
            result = super().execute_command(command)
            release.wait(timeout=3)  # Real effect committed before HTTP timeout.
            return result

    executor = subclass_executor(tmp_path / 'executor', SlowResponseAfterEffect)
    repo, run = ready_run(tmp_path)
    with actual_http(executor) as (origin, calls):
        try:
            async with ExecutorClient(origin, TOKEN, timeout_seconds=0.03) as client:
                workflow = FixtureWorkflow(repo, client, [configured_target()])
                result = await workflow.advance(run['id'])
                assert result['status'] == 'held'
                release.set()
                observed = await workflow.inspect(run['id'], 'deploy_fixture')
                assert observed.state == 'succeeded'
                assert (await workflow.advance(run['id']))['status'] == 'held'
            reopened = HubRepository(repo.path, execution_principal='executor:fixture')
            async with ExecutorClient(origin, TOKEN) as client:
                workflow = FixtureWorkflow(reopened, client, [configured_target()])
                assert (await workflow.advance(run['id']))['status'] == 'held'
                assert (await workflow.inspect(run['id'], 'deploy_fixture')).state == 'succeeded'
        finally:
            release.set()
    assert calls.count(('POST', '/v1/commands')) == 1
    assert len([call for call in calls if call[0] == 'GET']) == 2
    assert executor._read_observed(FIXTURE_TARGET)['effect_count'] == 1


@pytest.mark.asyncio
async def test_actual_http_pause_blocks_effect_and_persists_after_executor_restart(tmp_path):
    import uuid
    root = tmp_path / 'executor'
    executor = make_fixture_executor(root)
    repo, run = ready_run(tmp_path)
    with actual_http(executor) as (origin, calls):
        async with ExecutorClient(origin, TOKEN) as client:
            assert await client.health()
            assert await client.pause(True) is True
            result = await FixtureWorkflow(repo, client, [configured_target()]).advance(run['id'])
            assert result['status'] == 'held'
    assert calls.count(('POST', '/v1/commands')) == 1
    command_id = str(uuid.uuid5(uuid.UUID(run['id']), 'deploy_fixture'))
    restarted = make_fixture_executor(root)
    assert restarted.is_paused()
    assert restarted.get_command(command_id) is None
    assert restarted._read_observed(FIXTURE_TARGET) == {}


@pytest.mark.asyncio
async def test_whole_hub_rest_assignment_review_dispatch_to_actual_executor_http(tmp_path):
    import httpx
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    settings = Settings(tmp_path / 'rest-hub.sqlite3',
                        AuthSettings('fixture-hermes', 'fixture-codex', claude_token='fixture-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    repo = HubRepository(settings.database, execution_principal='executor:fixture')
    executor = make_fixture_executor(tmp_path / 'executor')
    prefix = '/api/hub/v1'
    with actual_http(executor) as (origin, calls):
        async with ExecutorClient(origin, TOKEN) as service:
            workflow = FixtureWorkflow(repo, service, [configured_target()])
            app = create_app(settings, repository=repo, fixture_workflow=workflow)
            # Internal test session only: no OAuth or human-session mint route enabled.
            session = app.state.sessions.create_session(42)
            human = {'Origin': settings.auth_config.origin, 'X-Hub-CSRF': session['csrf_token']}
            codex = {'Authorization': 'Bearer fixture-codex'}
            claude = {'Authorization': 'Bearer fixture-claude'}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url=settings.auth_config.origin) as http:
                    http.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
                    response = await http.post(prefix + '/tasks', headers=human,
                        json={'project': '3d-viewer', 'title': 'Real HTTP fixture', 'client_request_id': 'rest-fixture'})
                    assert response.status_code == 201
                    task = response.json()
                    response = await http.post(f"{prefix}/tasks/{task['id']}/claim", headers=codex,
                                               json={'expected_revision': task['revision']})
                    assert response.status_code == 200
                    task = response.json()
                    response = await http.post(f"{prefix}/tasks/{task['id']}/candidate", headers=codex,
                        json={'expected_revision': task['revision'], 'commit': FIXTURE_CANDIDATE_COMMIT})
                    assert response.status_code == 200
                    task = response.json()
                    review = {'expected_revision': task['revision'], 'commit': FIXTURE_CANDIDATE_COMMIT, 'verdict': 'approve'}
                    assert (await http.post(f"{prefix}/tasks/{task['id']}/review", headers=codex, json=review)).status_code == 403
                    response = await http.post(f"{prefix}/tasks/{task['id']}/review", headers=claude, json=review)
                    assert response.status_code == 200
                    task = response.json()
                    response = await http.post(prefix + '/runs', headers=codex, json={
                        'task_id': task['id'], 'expected_revision': task['revision'],
                        'artifact_digest': FIXTURE_CANDIDATE_DIGEST, 'target': FIXTURE_TARGET})
                    assert response.status_code == 201
                    run = response.json()
                    response = await http.post(f"{prefix}/runs/{run['id']}/dispatch", headers=codex)
                    assert response.status_code == 200
                    assert response.json()['status'] == 'succeeded'
                    capabilities = (await http.get(prefix + '/overview', headers=codex)).json()['capabilities']
                    assert capabilities['fixture_dispatch'] is True
                    assert capabilities['host_dispatch'] is False
    assert calls.count(('POST', '/v1/commands')) == 2
