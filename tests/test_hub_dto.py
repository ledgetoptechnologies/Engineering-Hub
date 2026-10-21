"""Response contracts use actual repository/workflow values, not UI mocks."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.autonomy import StagingRuntime
from engineering_hub.dispatcher import StagingDispatcher
from engineering_hub.dto import (
    AutonomyDTO, DecisionDTO, OverviewDTO, PauseDTO, RunDTO, TaskDTO,
)
from engineering_hub.fixture_workflow import FixtureTarget, FixtureWorkflow
from engineering_hub.repository import HubRepository


COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
PREFIX = '/api/hub/v1'
HEADERS = {'Authorization': 'Bearer test-codex'}


@pytest.fixture
def hub(tmp_path):
    settings = Settings(tmp_path / 'hub.sqlite3',
                        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    repo = HubRepository(settings.database, execution_principal='executor:fixture')
    return settings, repo


def assert_exact(model, payload):
    parsed = model.model_validate(payload)
    assert parsed.model_dump(exclude_unset=True) == payload
    return parsed


def task_states(repo):
    task = repo.create_task('human:42', '3d-viewer', 'Contract fixture', 'dto-task')
    yield task
    task = repo.claim_task('codex', task['id'], task['revision'])
    yield task
    task = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    yield task
    yield repo.review_task('claude', task['id'], task['revision'], COMMIT, 'approve')


def test_all_persisted_task_decision_pause_shapes_roundtrip_exactly(hub):
    _, repo = hub
    for task in task_states(repo):
        assert_exact(TaskDTO, task)
    decision = repo.create_decision('hermes', '3d-viewer', 'Choose', ['Later', 'Now'], protected=True)
    assert 'answered_by' not in decision
    assert_exact(DecisionDTO, decision)
    answered = repo.answer_decision('human:42', decision['id'], decision['revision'], 'Later')
    assert assert_exact(DecisionDTO, answered).answered_by == 'human:42'
    assert_exact(PauseDTO, repo.set_pause('human:42', True))


@pytest.mark.parametrize('events', [
    ['running', 'succeeded'], ['running', 'failed', 'rolled_back'], ['held'],
])
def test_actual_run_and_all_public_event_fields_are_preserved(hub, events):
    _, repo = hub
    task = list(task_states(repo))[-1]
    run = repo.create_run('codex', task['id'], task['revision'], DIGEST, '3d-viewer:staging')
    assert_exact(RunDTO, run)
    for event in events:
        run = repo.append_run_event('executor:fixture', run['id'], event,
                                   run['id'], run['fencing_generation'], run['revision'])
        assert_exact(RunDTO, run)
        assert set(run['events'][-1]) == {'event', 'actor', 'command_id', 'fencing_generation'}


def test_strict_models_reject_extra_private_fields_not_silently_filter(hub):
    _, repo = hub
    task = list(task_states(repo))[-1]
    for payload in [{**task, 'service_token': 'not-public'}, {**task, 'revision': str(task['revision'])}]:
        with pytest.raises(ValidationError):
            TaskDTO.model_validate(payload)
    with pytest.raises(ValidationError):
        TaskDTO.model_validate({**task, 'review': {**task['review'], 'private_owner': 'secret'}})


def test_rest_overview_and_lists_preserve_exact_source_shapes(hub):
    settings, repo = hub
    task = list(task_states(repo))[-1]
    decision = repo.create_decision('hermes', '3d-viewer', 'Choose', ['Later', 'Now'])
    run = repo.create_run('codex', task['id'], task['revision'], DIGEST, '3d-viewer:staging')
    app = create_app(settings, repository=repo)
    with TestClient(app, base_url='https://hub.test') as client:
        overview = client.get(PREFIX + '/overview', headers=HEADERS).json()
        assert_exact(OverviewDTO, overview)
        assert overview['tasks'] == repo.list_task_views()
        assert [{k: v for k, v in item.items() if k != 'state_since'} for item in overview['tasks']] == repo.list_tasks()
        assert overview['decisions'] == [decision]
        assert overview['runs'] == [run]
        assert overview['autonomy'] == {'configured': False, 'state': 'disabled', 'running': False, 'receipts': []}
        for path, expected in [('tasks', repo.list_task_views()), ('decisions', [decision]), ('runs', [run])]:
            assert client.get(PREFIX + '/' + path, headers=HEADERS).json() == {'items': expected}
        assert client.get(PREFIX + '/tasks/' + task['id'], headers=HEADERS).json() == repo.get_task(task['id'])


def test_openapi_response_objects_reference_closed_typed_schemas(hub):
    settings, repo = hub
    schema = create_app(settings, repository=repo).openapi()
    expected = {
        'overview': 'OverviewDTO', 'tasks': 'TaskListDTO', 'decisions': 'DecisionListDTO',
        'runs': 'RunListDTO', 'projects': 'ProjectListDTO', 'agents': 'AgentListDTO',
        'autonomy': 'AutonomyDTO', 'events/status': 'EventStatusDTO',
    }
    for path, name in expected.items():
        operation = schema['paths'][PREFIX + '/' + path]['get']
        assert operation['responses']['200']['content']['application/json']['schema'] == {'$ref': '#/components/schemas/' + name}
        assert schema['components']['schemas'][name]['additionalProperties'] is False
    components = schema['components']['schemas']
    assert components['TaskListDTO']['properties']['items']['items']['$ref'].endswith('/TaskDTO')
    assert components['RunDTO']['properties']['events']['items']['$ref'].endswith('/RunEventDTO')
    for name in ['TaskDTO', 'ReviewDTO', 'DecisionDTO', 'RunDTO', 'RunEventDTO', 'CapabilitiesDTO', 'DispatchReceiptDTO']:
        assert components[name]['additionalProperties'] is False


def test_unknown_source_field_fails_closed_without_exposing_value(hub, monkeypatch):
    settings, repo = hub
    task = list(task_states(repo))[-1]
    # /tasks reads list_task_views; an unknown field there must still fail closed.
    monkeypatch.setattr(repo, 'list_task_views', lambda: [{**task, 'service_token': 'private-sentinel'}])
    app = create_app(settings, repository=repo)
    with TestClient(app, base_url='https://hub.test', raise_server_exceptions=False) as client:
        response = client.get(PREFIX + '/tasks', headers=HEADERS)
        assert response.status_code == 500
        assert 'private-sentinel' not in response.text and 'service_token' not in response.text


@pytest.mark.asyncio
async def test_actual_autonomy_receipt_and_last_result_schema(hub):
    settings, repo = hub
    list(task_states(repo))
    class Client:
        async def submit_command(self, command):
            return SimpleNamespace(state='succeeded')
    target = FixtureTarget('3d-viewer:staging', DIGEST, COMMIT, 'sha256:' + 'd' * 64, 'c' * 40)
    workflow = FixtureWorkflow(repo, Client(), [target])
    dispatcher = StagingDispatcher(repo, workflow)
    result = await dispatcher.drain_batch()
    runtime = StagingRuntime(dispatcher, SimpleNamespace())
    runtime.last_result = result
    payload = {**runtime.status, 'receipts': [
        {key: value for key, value in receipt.items() if key != 'owner'} for receipt in dispatcher.receipts()
    ]}
    assert_exact(AutonomyDTO, payload)
    with pytest.raises(ValidationError):
        AutonomyDTO.model_validate({**payload, 'receipts': dispatcher.receipts()})
