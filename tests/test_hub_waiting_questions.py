"""Questions to Beau: three options and his text, answers, dismissals, retraction by agents, levels, bulk."""
import json

import pytest

from engineering_hub.repository import HubError
from tests.test_hub_task_backlog import BASE, BEAU, PROJECT, agent, audit, beau, hub, repo  # noqa: F401 (fixtures)

OPTIONS = [{'label': 'Keep it'}, {'label': 'Split it', 'detail': 'Two PRs'}, {'label': 'Drop it'}]
QUESTION = dict(type='decision', text='How should this ship?', options=OPTIONS, recommended=1,
                recommended_reason='Smaller reviews')


def raised(repo, actor='claude', owner='codex', reviewer='claude', waiting=QUESTION, key='k1'):
    task = repo.create_task(BEAU, PROJECT, 'Ship it', key, owner=owner, reviewer=reviewer)
    return repo.update_task(actor, task['id'], 1, status='waiting_on_beau', waiting=waiting)


def test_agent_decision_stores_options_recommendation_family_and_level(repo):
    task = raised(repo)
    assert task['waiting_on'] == dict(QUESTION, previous_status='assigned', by='claude', by_family='claude',
                                      at=task['waiting_on']['at'], level='now')
    later = raised(repo, key='k2', waiting=dict(type='action', text='Add DNS.', level='later'))
    assert later['waiting_on']['level'] == 'later' and 'options' not in later['waiting_on']
    # A human may still raise a plain decision; an agent's decision needs options.
    task = repo.create_task(BEAU, PROJECT, 'Plain', 'k3', owner='codex')
    assert repo.update_task(BEAU, task['id'], 1, status='waiting_on_beau',
                            waiting=dict(type='decision', text='Name?'))['status'] == 'waiting_on_beau'
    with pytest.raises(HubError, match='waiting_options_required') as error:
        raised(repo, key='k4', waiting=dict(type='decision', text='Name?'))
    assert error.value.detail == dict(code='waiting_options_required', field='waiting.options', limit=3)


@pytest.mark.parametrize('change,field', [
    (dict(options=OPTIONS[:2]), 'waiting.options'),
    (dict(options=OPTIONS + [{'label': 'More'}]), 'waiting.options'),
    (dict(options=[{'label': 'x' * 81}] + OPTIONS[1:]), 'waiting.options.label'),
    (dict(options=[{'label': 'Same'}, {'label': 'same'}, {'label': 'Other'}]), 'waiting.options.label'),
    (dict(options=[{'label': 'A', 'detail': 'd' * 161}] + OPTIONS[1:]), 'waiting.options.detail'),
    (dict(options=[{'label': 'A', 'extra': 'x'}] + OPTIONS[1:]), 'waiting.options'),
    (dict(recommended=3), 'waiting.recommended'), (dict(recommended=None), 'waiting.recommended'),
    (dict(recommended_reason=None), 'waiting.recommended_reason'),
    (dict(recommended_reason='r' * 161), 'waiting.recommended_reason'),
    (dict(level='soon'), 'waiting.level'), (dict(type='action'), 'waiting.options'),
])
def test_option_validation_names_the_field(repo, change, field):
    with pytest.raises(HubError, match='invalid_waiting_item') as error:
        raised(repo, waiting={key: value for key, value in dict(QUESTION, **change).items() if value is not None})
    assert error.value.field == field


def test_beau_answers_by_option_or_text_and_agents_read_it(repo):
    task = raised(repo)
    with pytest.raises(HubError, match='answer_required'):
        repo.resolve_waiting(BEAU, task['id'], 2)
    with pytest.raises(HubError, match='invalid_input'):
        repo.resolve_waiting(BEAU, task['id'], 2, option=0, answer='Both')
    answered = repo.resolve_waiting(BEAU, task['id'], 2, 'Go', option=1)
    assert answered['status'] == 'assigned' and 'waiting_on' not in answered
    assert answered['waiting_resolved'] == dict(type='decision', text=QUESTION['text'], by=BEAU, note='Go',
                                                at=answered['waiting_resolved']['at'], outcome='answered',
                                                answer=dict(option=1, label='Split it'))
    assert repo.resolve_waiting(BEAU, task['id'], 2, 'Go', option=1) == answered  # idempotent retry
    with pytest.raises(HubError, match='revision_conflict|not_waiting'):
        repo.resolve_waiting(BEAU, task['id'], 2, 'Go', option=2)
    assert repo.get_task(task['id'])['waiting_resolved']['answer']['label'] == 'Split it'
    assert audit(repo, task['id'])[-1] == (BEAU, 'task.waiting_resolved')
    typed = raised(repo, key='k2')
    typed = repo.resolve_waiting(BEAU, typed['id'], 2, answer='Ship it behind a flag')
    assert typed['waiting_resolved']['answer'] == dict(text='Ship it behind a flag')
    action = raised(repo, key='k3', waiting=dict(type='action', text='Add DNS.'))
    with pytest.raises(HubError, match='invalid_answer'):
        repo.resolve_waiting(BEAU, action['id'], 2, answer='Done')
    assert 'outcome' not in repo.resolve_waiting(BEAU, action['id'], 2, 'Added')['waiting_resolved']


def test_beau_dismisses_and_agents_cannot_answer_or_dismiss(repo):
    task = raised(repo)
    for call in (lambda: repo.resolve_waiting('claude', task['id'], 2, option=1),
                 lambda: repo.resolve_waiting_many('codex', [dict(task_id=task['id'], expected_revision=2, dismiss=True)]),
                 lambda: repo.set_waiting_level('claude', task['id'], 2, 'later')):
        with pytest.raises(HubError, match='forbidden'):
            call()
    with pytest.raises(HubError, match='waiting_on_beau'):
        repo.update_task('claude', task['id'], 2, status='assigned')
    dismissed = repo.resolve_waiting(BEAU, task['id'], 2, 'Not needed', dismiss=True)
    assert dismissed['waiting_resolved']['outcome'] == 'dismissed' and 'answer' not in dismissed['waiting_resolved']


def test_raising_family_or_owner_retracts_others_cannot(repo):
    # claude reviews and raised it; codex owns the task; hermes has no role.
    task = raised(repo)
    with pytest.raises(HubError, match='forbidden'):
        repo.update_task('hermes', task['id'], 2, retract_reason='Mine now')
    with pytest.raises(HubError, match='forbidden'):
        repo.update_task(BEAU, task['id'], 2, retract_reason='Beau dismisses instead')
    with pytest.raises(HubError, match='invalid_input'):
        repo.update_task('claude', task['id'], 2, retract_reason='x', status='assigned')
    back = repo.update_task('claude', task['id'], 2, retract_reason='Answered in chat', note='Beau said split',
                           client_request_id='r1')
    assert back['status'] == 'assigned' and 'waiting_on' not in back and back['notes'][-1]['text'] == 'Beau said split'
    assert back['waiting_retracted'] == dict(type='decision', text=QUESTION['text'], by='claude',
                                             at=back['waiting_retracted']['at'], reason='Answered in chat')
    assert repo.update_task('claude', task['id'], 2, retract_reason='Answered in chat', note='Beau said split',
                            client_request_id='r1') == back
    assert audit(repo, task['id'])[-1] == ('claude', 'task.waiting_retracted')
    with pytest.raises(HubError, match='not_waiting'):
        repo.update_task('claude', task['id'], 3, retract_reason='Again')
    owned = raised(repo, key='k2')
    assert repo.update_task('codex', owned['id'], 2, retract_reason='No longer needed')['status'] == 'assigned'
    # Without a role on the task, the raising family still takes back its own item.
    other = raised(repo, key='k3')
    other = repo.update_task(BEAU, other['id'], 2, reviewer='hermes')
    assert repo.update_task('claude', other['id'], other['revision'], retract_reason='Moot')['status'] == 'assigned'


def test_level_is_changed_by_beau_and_noop_when_same(repo):
    task = raised(repo)
    later = repo.set_waiting_level(BEAU, task['id'], 2, 'later')
    assert later['waiting_on']['level'] == 'later' and later['revision'] == 3
    assert repo.set_waiting_level(BEAU, task['id'], 2, 'later') == later
    assert audit(repo, task['id'])[-1] == (BEAU, 'task.waiting_level')
    with pytest.raises(HubError, match='invalid_input'):
        repo.set_waiting_level(BEAU, task['id'], 3, 'never')


def test_bulk_answer_and_dismiss_apply_together_or_not_at_all(repo):
    first, second, third = (raised(repo, key=key) for key in ('k1', 'k2', 'k3'))
    with pytest.raises(HubError, match='revision_conflict'):
        repo.resolve_waiting_many(BEAU, [dict(task_id=first['id'], expected_revision=2, option=1),
                                         dict(task_id=second['id'], expected_revision=9, dismiss=True)])
    assert repo.get_task(first['id'])['status'] == 'waiting_on_beau'
    items = [dict(task_id=first['id'], expected_revision=2, option=1),
             dict(task_id=second['id'], expected_revision=2, dismiss=True, note='Moot')]
    done = repo.resolve_waiting_many(BEAU, items)
    assert [task['waiting_resolved']['outcome'] for task in done] == ['answered', 'dismissed']
    assert repo.resolve_waiting_many(BEAU, items) == done
    assert repo.get_task(third['id'])['status'] == 'waiting_on_beau'
    for bad in ([], [dict(task_id=third['id'], expected_revision=2, dismiss=True)] * 2,
                [dict(task_id=third['id'], expected_revision=2, option=5)]):
        with pytest.raises(HubError):
            repo.resolve_waiting_many(BEAU, bad)


def test_old_waiting_rows_still_load_and_clear(repo, hub):
    task = repo.create_task(BEAU, PROJECT, 'Old', 'k1', owner='codex')
    legacy = dict(task, status='waiting_on_beau', revision=2,
                  waiting_on=dict(type='decision', text='Old question', previous_status='assigned', by='claude',
                                  at='2026-11-16T00:00:00+00:00'))
    with repo._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(legacy), task['id']))
    # The raising family is read from a bare family id on rows without by_family.
    assert repo.update_task('claude', task['id'], 2, retract_reason='Moot')['waiting_retracted']['by'] == 'claude'
    client, app, settings = hub
    old = app.state.repository.create_task(BEAU, PROJECT, 'Old', 'k1', owner='claude')
    stored = dict(old, status='waiting_on_beau', revision=2, waiting_on=legacy['waiting_on'])
    with app.state.repository._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(stored), old['id']))
    listed = client.get(BASE + '/tasks', headers=agent('claude'))
    assert listed.status_code == 200 and listed.json()['items'][0]['waiting_on']['text'] == 'Old question'
    human = beau(client, app, settings)
    cleared = client.post(f"{BASE}/tasks/{old['id']}/resolve-waiting", headers=human,
                          json={'expected_revision': 2, 'answer': 'Typed'})
    assert cleared.status_code == 200 and cleared.json()['waiting_resolved']['answer'] == {'text': 'Typed'}


def test_rest_question_answer_bulk_level_and_limits(hub):
    client, app, settings = hub
    human = beau(client, app, settings)
    ids = []
    for key in ('r1', 'r2', 'r3'):
        task = client.post(BASE + '/tasks', headers=human, json={
            'project': PROJECT, 'title': 'Ship', 'client_request_id': key, 'assignee': 'claude'}).json()
        waited = client.post(f"{BASE}/tasks/{task['id']}/update", headers=agent('claude'), json={
            'expected_revision': 1, 'client_request_id': 'u' + key, 'status': 'waiting_on_beau', 'waiting': QUESTION})
        assert waited.status_code == 200, waited.text
        ids.append(task['id'])
    long_note = client.post(f"{BASE}/tasks/{ids[0]}/update", headers=agent('claude'), json={
        'expected_revision': 2, 'client_request_id': 'n', 'note': 'x' * 501})
    assert long_note.json()['detail'] == {'code': 'invalid_input', 'field': 'note', 'limit': 500}
    links = client.post(BASE + '/tasks', headers=human, json={
        'project': PROJECT, 'title': 'Links', 'client_request_id': 'l', 'links': [{'label': 'a', 'url': 'https://a.test'}] * 9})
    assert links.json()['detail'] == {'code': 'invalid_input', 'field': 'links', 'limit': 8}
    for path, body in (('/tasks/resolve-waiting', {'items': [{'task_id': ids[0], 'expected_revision': 2, 'dismiss': True}]}),
                       (f'/tasks/{ids[0]}/waiting-level', {'expected_revision': 2, 'level': 'later'}),
                       (f'/tasks/{ids[0]}/resolve-waiting', {'expected_revision': 2, 'option': 0})):
        assert client.post(BASE + path, headers=agent('claude'), json=body).status_code == 403
    level = client.post(f"{BASE}/tasks/{ids[0]}/waiting-level", headers=human, json={'expected_revision': 2, 'level': 'later'})
    assert level.json()['waiting_on']['level'] == 'later'
    bulk = client.post(BASE + '/tasks/resolve-waiting', headers=human, json={'items': [
        {'task_id': ids[0], 'expected_revision': 3, 'option': 1}, {'task_id': ids[1], 'expected_revision': 2, 'dismiss': True}]})
    assert bulk.status_code == 200, bulk.text
    assert [item['waiting_resolved'].get('outcome') for item in bulk.json()['items']] == ['answered', 'dismissed']
    got = client.get(BASE + '/tasks', headers=agent('codex')).json()['items']
    assert next(item for item in got if item['id'] == ids[0])['waiting_resolved']['answer']['label'] == 'Split it'


def test_title_edit_by_human_or_creating_family_and_link_limit(repo):
    task = repo.create_task('claude', PROJECT, 'Old title', 'k1', owner='codex', reviewer='hermes')
    assert repo.update_task('claude', task['id'], 1, title='  New title ')['title'] == 'New title'
    with pytest.raises(HubError, match='forbidden'):
        repo.update_task('codex', task['id'], 2, title='Owner rename')
    assert repo.update_task(BEAU, task['id'], 2, title='Beau title')['title'] == 'Beau title'
    with pytest.raises(HubError) as error:
        repo.update_task(BEAU, task['id'], 3, links=[{'label': 'a', 'url': 'https://a.test'}] * 9)
    assert error.value.detail == dict(code='invalid_context', field='links', limit=8)
    with pytest.raises(HubError) as error:
        repo.update_task('claude', task['id'], 3, note='n' * 501)
    assert error.value.message == 'invalid_input: note (limit 500)'
