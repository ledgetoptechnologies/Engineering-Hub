"""An agent withdraws the waiting-on-Beau question it raised; Beau sees why and can undo it."""
import json

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.dto import TaskDTO
from engineering_hub.repository import HubError, HubRepository

BEAU = 'human:42'
PROJECT = 'engineering-hub'
BASE = '/api/hub/v1'


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'hub.db')


def waiting(repo, raiser='claude', kind='decision', key='k1'):
    task = repo.claim_task('claude', repo.create_task('claude', PROJECT, 'Stale item', key, owner='claude',
                                                      reviewer='codex')['id'], 1)
    return repo.update_task(raiser, task['id'], task['revision'], status='waiting_on_beau',
                            waiting={'type': kind, 'text': 'Pick the domain name.'})


def audit(repo, identity):
    with repo._connection() as db:
        return [tuple(row) for row in db.execute(
            'SELECT actor, action FROM hub_audit WHERE entity_id=? ORDER BY sequence', (identity,))]


def test_raising_agent_withdraws_its_question_with_a_recorded_reason(repo):
    task = waiting(repo)
    done = repo.withdraw('claude', 'waiting', task['id'], task['revision'], 'Answered in the PR thread')
    assert done['status'] == 'claimed' and 'waiting_on' not in done and done['revision'] == task['revision'] + 1
    record = done['waiting_withdrawn']
    assert record == dict(type='decision', text='Pick the domain name.', previous_status='claimed',
                          raised_by='claude', raised_at=task['waiting_on']['at'], by='claude',
                          at=record['at'], reason='Answered in the PR thread')
    assert audit(repo, task['id'])[-1] == ('claude', 'task.waiting_withdrawn')
    TaskDTO.model_validate(done)
    view = {t['id']: t for t in repo.list_task_views()}[task['id']]
    assert view['state_since'] == done['updated_at']
    # Retrying the same withdrawal returns the task; a different one conflicts.
    assert repo.withdraw('claude', 'waiting', task['id'], task['revision'], 'Answered in the PR thread') == done
    with pytest.raises(HubError, match='revision_conflict'):
        repo.withdraw('claude', 'waiting', task['id'], task['revision'], 'Other reason')
    with pytest.raises(HubError, match='not_waiting'):
        repo.withdraw('claude', 'waiting', task['id'], done['revision'], 'Again')
    assert len([row for row in audit(repo, task['id']) if row[1] == 'task.waiting_withdrawn']) == 1


def test_another_agents_or_a_humans_item_is_refused(repo):
    task = waiting(repo)
    for agent in ('codex', 'hermes'):
        with pytest.raises(HubError, match='waiting_not_raised_by_you'):
            repo.withdraw(agent, 'waiting', task['id'], task['revision'], 'Not mine')
    beaus = waiting(repo, raiser=BEAU, key='k2')
    with pytest.raises(HubError, match='waiting_not_raised_by_you'):
        repo.withdraw('claude', 'waiting', beaus['id'], beaus['revision'], 'Owner raised it')
    # Beau clears items through resolve-waiting, not this agent path.
    with pytest.raises(HubError, match='waiting_withdraw_agents_only'):
        repo.withdraw(BEAU, 'waiting', task['id'], task['revision'], 'Mine to clear')
    assert repo.get_task(task['id']) == task


def test_already_answered_item_is_refused(repo):
    task = waiting(repo)
    answered = repo.resolve_waiting(BEAU, task['id'], task['revision'], 'Use the short one')
    with pytest.raises(HubError, match='waiting_already_answered'):
        repo.withdraw('claude', 'waiting', task['id'], answered['revision'], 'Stale')
    # Beau answering first makes the agent's stale revision conflict, never overwrite.
    with pytest.raises(HubError, match='revision_conflict'):
        repo.withdraw('claude', 'waiting', task['id'], task['revision'], 'Stale')
    assert repo.get_task(task['id']) == answered


def test_action_item_needs_the_owner_and_stays(repo):
    task = waiting(repo, kind='action')
    with pytest.raises(HubError, match='waiting_action_needs_owner'):
        repo.withdraw('claude', 'waiting', task['id'], task['revision'], 'Approve no longer needed')
    assert repo.get_task(task['id'])['status'] == 'waiting_on_beau'


@pytest.mark.parametrize('reason', ['', '   ', None, 'x' * 501, 'a\x00b'])
def test_reason_is_required(repo, reason):
    task = waiting(repo)
    with pytest.raises(HubError, match='invalid_reason'):
        repo.withdraw('claude', 'waiting', task['id'], task['revision'], reason)
    assert repo.get_task(task['id']) == task


def test_replacement_and_stale_revision_are_refused_without_writes(repo):
    task = waiting(repo)
    with pytest.raises(HubError, match='invalid_replacement'):
        repo.withdraw('claude', 'waiting', task['id'], task['revision'], 'Moved', task['id'])
    with pytest.raises(HubError, match='revision_conflict'):
        repo.withdraw('claude', 'waiting', task['id'], task['revision'] - 1, 'Moved')
    assert repo.get_task(task['id']) == task


def test_owner_undo_restores_the_item_and_is_idempotent(repo):
    task = waiting(repo)
    done = repo.withdraw('claude', 'waiting', task['id'], task['revision'], 'Not needed')
    with pytest.raises(HubError, match='forbidden'):
        repo.restore_waiting('claude', task['id'], done['revision'])
    with pytest.raises(HubError, match='revision_conflict'):
        repo.restore_waiting(BEAU, task['id'], task['revision'])
    back = repo.restore_waiting(BEAU, task['id'], done['revision'])
    assert back['status'] == 'waiting_on_beau' and 'waiting_withdrawn' not in back
    assert back['waiting_on'] == dict(type='decision', text='Pick the domain name.', previous_status='claimed',
                                      by='claude', at=task['waiting_on']['at'], restored_by=BEAU,
                                      restored_at=back['waiting_on']['restored_at'])
    assert repo.restore_waiting(BEAU, task['id'], done['revision']) == back
    assert audit(repo, task['id'])[-2:] == [('claude', 'task.waiting_withdrawn'), (BEAU, 'task.waiting_restored')]
    TaskDTO.model_validate(back)
    # Restored, it is Beau's again to clear as usual.
    assert repo.resolve_waiting(BEAU, task['id'], back['revision'])['status'] == 'claimed'
    with pytest.raises(HubError, match='not_withdrawn'):
        repo.restore_waiting(BEAU, task['id'], back['revision'] + 1)


def test_new_question_replaces_the_withdrawn_record(repo):
    task = waiting(repo)
    done = repo.withdraw('claude', 'waiting', task['id'], task['revision'], 'Not needed')
    again = repo.update_task('claude', task['id'], done['revision'], status='waiting_on_beau',
                             waiting={'type': 'decision', 'text': 'New question.'})
    assert 'waiting_withdrawn' not in again and again['waiting_on']['text'] == 'New question.'


def test_installation_raiser_family_is_read_from_server_fields(repo):
    install = 'installation:' + '0' * 8 + '-0000-4000-8000-' + '0' * 12
    with repo._connection() as db:
        assert repo._waiting_raiser_family(db, 'x', {'by': install, 'by_family': 'claude'}) == 'claude'
        assert repo._waiting_raiser_family(db, 'x', {'by': install}) is None
        assert repo._waiting_raiser_family(db, 'x', {'by': install, 'by_family': 'someone'}) is None
        assert repo._waiting_raiser_family(db, 'x', {'by': BEAU, 'by_family': 'claude'}) is None


@pytest.fixture
def hub(tmp_path):
    settings = Settings(tmp_path / 'hub.sqlite3', AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    app = create_app(settings)
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        yield client, app, settings


def test_mcp_withdraw_and_rest_owner_undo(hub):
    client, app, settings = hub
    task = waiting(app.state.repository)

    def call(agent):
        response = client.post('/mcp/', headers={'Accept': 'application/json, text/event-stream',
                                                 'MCP-Protocol-Version': '2025-06-18',
                                                 'Authorization': f'Bearer test-{agent}'},
                               json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                                   'name': 'hub_entity_withdraw', 'arguments': {
                                       'kind': 'waiting', 'entity_id': task['id'],
                                       'expected_revision': task['revision'], 'reason': 'Not needed'}}})
        assert response.status_code == 200, response.text
        return response.json()['result']

    refused = call('codex')
    assert refused.get('isError') and 'waiting_not_raised_by_you' in refused['content'][0]['text']
    done = json.loads(call('claude')['content'][0]['text'])
    assert done['status'] == 'claimed' and done['waiting_withdrawn']['reason'] == 'Not needed'
    url = f"{BASE}/tasks/{task['id']}/restore-waiting"
    body = {'expected_revision': done['revision']}
    assert client.post(url, headers={'Authorization': 'Bearer test-claude'}, json=body).status_code in (401, 403)
    session = app.state.sessions.create_session(42)
    client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
    origin = {'Origin': settings.auth_config.origin}
    assert client.post(url, headers=origin, json=body).status_code == 403
    restored = client.post(url, headers={**origin, 'X-Hub-CSRF': session['csrf_token']}, json=body)
    assert restored.status_code == 200, restored.text
    assert restored.json()['status'] == 'waiting_on_beau' and restored.json()['waiting_on']['restored_by'] == BEAU
