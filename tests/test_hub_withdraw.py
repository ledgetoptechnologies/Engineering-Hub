"""Withdrawing superseded tasks and decisions: history kept, nothing approved or deleted."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.private_gateway import browser_route_allowed
from engineering_hub.repository import HubError, HubRepository

HUMAN = 'human:1'
BASE = '/api/hub/v1'
COMMIT = 'a' * 40


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'hub.sqlite3')


def proposal(repo, agent='claude', key='old', project='treedar'):
    return repo.propose_staging_task(agent, project, 'Old task', key)


def question(repo, agent='claude', project='treedar', **kwargs):
    return repo.create_decision(agent, project, 'Old question', ['Yes', 'No'], **kwargs)


def test_human_withdraws_a_decision_with_replacement_and_history_is_kept(repo):
    old, new = question(repo), question(repo, project='engineering-hub')
    done = repo.withdraw(HUMAN, 'decision', old['id'], old['revision'], 'Replaced by the mapped copy', new['id'])
    assert done['status'] == 'withdrawn' and done['revision'] == old['revision'] + 1
    assert done['superseded_by'] == new['id'] and done['withdrawn_by'] == HUMAN
    assert done['withdrawn_reason'] == 'Replaced by the mapped copy' and done['withdrawn_at']
    assert done['title'] == old['title'] and done['options'] == old['options'] and done['answer'] is None
    assert repo.list_decisions()[0] == done
    assert repo.list_audit()[-1]['action'] == 'decision.withdrawn'


def test_retrying_the_same_withdrawal_is_idempotent_but_a_different_one_conflicts(repo):
    item = proposal(repo)
    done = repo.withdraw('claude', 'task', item['id'], item['revision'], 'Moved')
    assert repo.withdraw('claude', 'task', item['id'], item['revision'], 'Moved') == done
    for actor, reason in ((HUMAN, 'Moved'), ('claude', 'Other reason')):
        with pytest.raises(HubError, match='withdrawn'):
            repo.withdraw(actor, 'task', item['id'], done['revision'], reason)
    assert len([row for row in repo.list_audit() if row['action'] == 'task.withdrawn']) == 1


def test_agent_may_withdraw_only_what_it_created_per_the_audit_log(repo):
    item, ask = proposal(repo, agent='codex'), question(repo, agent='codex')
    for agent in ('claude', 'hermes'):
        with pytest.raises(HubError, match='forbidden'):
            repo.withdraw(agent, 'task', item['id'], item['revision'], 'Not mine')
        with pytest.raises(HubError, match='forbidden'):
            repo.withdraw(agent, 'decision', ask['id'], ask['revision'], 'Not mine')
    # A human-created task has no agent creator, so no agent may withdraw it.
    human_task = repo.create_task(HUMAN, 'treedar', 'Human task', 'k')
    with pytest.raises(HubError, match='forbidden'):
        repo.withdraw('codex', 'task', human_task['id'], human_task['revision'], 'Mine')
    assert repo.withdraw('codex', 'task', item['id'], item['revision'], 'Mine')['status'] == 'withdrawn'


def test_protected_and_answered_decisions_cannot_be_withdrawn(repo):
    protected = question(repo, protected=True)
    with pytest.raises(HubError, match='protected_decision'):
        repo.withdraw(HUMAN, 'decision', protected['id'], protected['revision'], 'No')
    answered = question(repo)
    answered = repo.answer_decision(HUMAN, answered['id'], answered['revision'], 'Yes')
    with pytest.raises(HubError, match='decision_closed'):
        repo.withdraw(HUMAN, 'decision', answered['id'], answered['revision'], 'No')


def test_started_tasks_cannot_be_withdrawn(repo):
    item = repo.claim_task('codex', proposal(repo)['id'], 1)
    item = repo.set_candidate('codex', item['id'], item['revision'], COMMIT)
    with pytest.raises(HubError, match='task_in_progress'):
        repo.withdraw(HUMAN, 'task', item['id'], item['revision'], 'No')
    verified = repo.review_task('claude', item['id'], item['revision'], COMMIT, 'approve')
    with pytest.raises(HubError, match='task_in_progress'):
        repo.withdraw(HUMAN, 'task', verified['id'], verified['revision'], 'No')


def test_claimed_task_without_candidate_can_be_withdrawn_and_nothing_reopens_it(repo):
    item = repo.claim_task('codex', proposal(repo)['id'], 1)
    done = repo.withdraw(HUMAN, 'task', item['id'], item['revision'], 'Superseded')
    for call, code in ((lambda: repo.set_candidate('codex', done['id'], done['revision'], COMMIT), 'withdrawn'),
                       (lambda: repo.claim_task('codex', done['id'], done['revision']), 'already_claimed'),
                       (lambda: repo.update_context(HUMAN, 'task', done['id'], done['revision'], 'New', None), 'withdrawn')):
        with pytest.raises(HubError, match=code):
            call()
    assert repo.get_task(done['id']) == done


def test_withdrawn_decision_cannot_be_answered_or_edited(repo):
    ask = question(repo)
    done = repo.withdraw(HUMAN, 'decision', ask['id'], ask['revision'], 'Superseded')
    with pytest.raises(HubError, match='invalid_answer'):
        repo.answer_decision(HUMAN, done['id'], done['revision'], 'Yes')
    with pytest.raises(HubError, match='decision_closed'):
        repo.update_context(HUMAN, 'decision', done['id'], done['revision'], 'New', None)


def test_cas_reason_and_replacement_are_validated_without_writes(repo):
    item, other_kind = proposal(repo), question(repo)
    gone = question(repo)
    gone = repo.withdraw(HUMAN, 'decision', gone['id'], gone['revision'], 'Gone')
    audit = len(repo.list_audit())
    cases = [
        (dict(expected_revision=9), 'revision_conflict'),
        (dict(reason=''), 'invalid_reason'), (dict(reason='x' * 501), 'invalid_reason'),
        (dict(reason=None), 'invalid_reason'), (dict(reason='a\x00b'), 'invalid_reason'),
        (dict(superseded_by=item['id'][:8]), 'invalid_replacement'),
        (dict(superseded_by=item['id']), 'invalid_replacement'),
        (dict(superseded_by=item['id'].upper()), 'invalid_replacement'),
        (dict(superseded_by=str(uuid.uuid4())), 'not_found'),
        (dict(superseded_by=other_kind['id']), 'not_found'),
    ]
    for change, code in cases:
        args = dict(expected_revision=item['revision'], reason='Superseded', superseded_by=None) | change
        with pytest.raises(HubError, match=code):
            repo.withdraw(HUMAN, 'task', item['id'], args['expected_revision'], args['reason'], args['superseded_by'])
    with pytest.raises(HubError, match='invalid_replacement'):
        repo.withdraw(HUMAN, 'decision', other_kind['id'], other_kind['revision'], 'x', gone['id'])
    with pytest.raises(HubError, match='invalid_kind'):
        repo.withdraw(HUMAN, 'run', item['id'], item['revision'], 'x')
    with pytest.raises(HubError, match='forbidden'):
        repo.withdraw('stranger', 'task', item['id'], item['revision'], 'x')
    assert len(repo.list_audit()) == audit
    assert repo.get_task(item['id']) == item


def test_withdrawal_resets_the_tasks_state_since(repo):
    item = proposal(repo)
    done = repo.withdraw(HUMAN, 'task', item['id'], item['revision'], 'Moved')
    view = next(task for task in repo.list_task_views() if task['id'] == item['id'])
    assert view['status'] == 'withdrawn' and view['state_since'] == done['updated_at']


def test_gateway_allows_only_exact_withdraw_routes():
    good = '/api/hub/v1/decisions/' + str(uuid.uuid4()) + '/withdraw'
    assert browser_route_allowed('POST', good)
    assert browser_route_allowed('POST', good.replace('decisions', 'tasks'))
    assert not browser_route_allowed('GET', good)
    assert not browser_route_allowed('POST', '/api/hub/v1/runs/' + str(uuid.uuid4()) + '/withdraw')
    assert not browser_route_allowed('POST', '/api/hub/v1/decisions/abc/withdraw')


@pytest.fixture
def hub(tmp_path):
    settings = Settings(tmp_path / 'hub.sqlite3', AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    app = create_app(settings)
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        yield client, app, settings


def human_headers(client, app, settings):
    session = app.state.sessions.create_session(42)
    client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
    return {'Origin': settings.auth_config.origin, 'X-Hub-CSRF': session['csrf_token']}


def test_rest_withdraw_is_human_only_with_csrf_and_returns_the_dto(hub):
    client, app, settings = hub
    ask = app.state.repository.create_decision('claude', 'treedar', 'Old', ['Yes', 'No'])
    body = {'expected_revision': ask['revision'], 'reason': 'Replaced'}
    url = f"{BASE}/decisions/{ask['id']}/withdraw"
    assert client.post(url, headers={'Authorization': 'Bearer test-claude'}, json=body).status_code in (401, 403)
    headers = human_headers(client, app, settings)
    assert client.post(url, headers={'Origin': headers['Origin']}, json=body).status_code == 403
    assert client.post(url, headers=headers, json={**body, 'superseded_by': 'nope'}).status_code == 422
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'withdrawn' and response.json()['withdrawn_reason'] == 'Replaced'
    overview = client.get(BASE + '/overview', headers={'Authorization': 'Bearer test-codex'}).json()
    assert next(item for item in overview['decisions'] if item['id'] == ask['id'])['status'] == 'withdrawn'


def test_mcp_withdraw_tool_uses_the_authenticated_agent(hub):
    client, app, _ = hub
    ask = app.state.repository.create_decision('claude', 'treedar', 'Old', ['Yes', 'No'])

    def call(agent):
        response = client.post('/mcp/', headers={'Accept': 'application/json, text/event-stream',
                                                 'MCP-Protocol-Version': '2025-06-18',
                                                 'Authorization': f'Bearer test-{agent}'},
                               json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                                   'name': 'hub_entity_withdraw', 'arguments': {
                                       'kind': 'decision', 'entity_id': ask['id'],
                                       'expected_revision': ask['revision'], 'reason': 'Moved'}}})
        assert response.status_code == 200, response.text
        return response.json()['result']

    refused = call('codex')
    assert refused.get('isError') and 'forbidden' in refused['content'][0]['text']
    done = call('claude')
    assert not done.get('isError'), done
    assert json.loads(done['content'][0]['text'])['status'] == 'withdrawn'
    assert app.state.repository.list_audit()[-1]['action'] == 'decision.withdrawn'


from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker


@pytest.fixture
def queue(repo):
    actions = {'validate': WorkAction('machine-one', frozenset({'staging'}), project='treedar', requires_execution_proof=False)}
    return WorkQueue(repo, actions, target_verifier=lambda *args: True)


def submit(queue, task, key='job'):
    return queue.submit(actor='codex', request_id=key, action_key='validate', environment='staging',
                        target=WorkTarget(task['id'], COMMIT, 'sha256:' + 'b' * 64))


WORKER = WorkWorker('codex', str(uuid.uuid5(uuid.NAMESPACE_DNS, 'one')))


@pytest.mark.parametrize('state', ['queued', 'claimed', 'held'])
def test_task_with_open_queue_work_cannot_be_withdrawn_and_the_job_is_untouched(repo, queue, state):
    task = proposal(repo, agent='codex')
    job = submit(queue, task)
    if state != 'queued':
        job = queue.claim(worker=WORKER)
        if state == 'held':
            queue.complete(job_id=job['id'], worker=WORKER, fence=job['fence'], outcome='held')
    with pytest.raises(HubError, match='task_in_progress'):
        repo.withdraw('codex', 'task', task['id'], task['revision'], 'Moved')
    assert repo.get_task(task['id'])['status'] == 'assigned'
    assert queue.get_job(job['id'])['state'] == state


def test_finished_queue_work_does_not_block_withdrawal(repo, queue):
    task = proposal(repo, agent='codex')
    submit(queue, task)
    job = queue.claim(worker=WORKER)
    queue.complete(job_id=job['id'], worker=WORKER, fence=job['fence'], outcome='succeeded')
    assert repo.withdraw('codex', 'task', task['id'], task['revision'], 'Done elsewhere')['status'] == 'withdrawn'


def test_withdrawn_task_is_refused_by_queue_admission(repo, queue):
    task = proposal(repo, agent='codex')
    repo.withdraw('codex', 'task', task['id'], task['revision'], 'Moved')
    assert repo.task_allows_environment(task['id'], 'staging') is False
    with pytest.raises(HubError):
        submit(queue, task, key='after')
    assert queue.claim(worker=WORKER) is None
