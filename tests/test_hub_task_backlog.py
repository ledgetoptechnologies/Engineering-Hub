"""Backlog tasks: agent creation, reviewer/acceptance, waiting-on-Beau and done. No executor."""
import json

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.repository import HubError, HubRepository


COMMIT = 'a' * 40
BASE = '/api/hub/v1'
PROJECT = 'engineering-hub'
BEAU = 'human:42'


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'hub.db')


def create(repo, actor='claude', key='k1', **fields):
    return repo.create_task(actor, PROJECT, 'Unify the backlog', key, details='Why it matters',
                            acceptance=['Agents can create tasks', 'Old rows still load'], **fields)


def verified(repo, task, owner='claude', reviewer='codex'):
    task = repo.claim_task(owner, task['id'], task['revision'])
    task = repo.set_candidate(owner, task['id'], task['revision'], COMMIT)
    return repo.review_task(reviewer, task['id'], task['revision'], COMMIT, 'approve')


def audit(repo, identity):
    with repo._connection() as db:
        return [tuple(row) for row in db.execute(
            'SELECT actor, action FROM hub_audit WHERE entity_id=? ORDER BY sequence', (identity,))]


def test_agent_creates_backlog_task_idempotently_without_environment_permission(repo):
    task = create(repo, owner='claude', reviewer='codex', request_text='Beau: make the Hub the backlog')
    assert task['proposed_by'] == 'claude' and 'scope' not in task
    assert task['status'] == 'assigned' and task['owner'] == 'claude' and task['reviewer'] == 'codex'
    assert task['acceptance'] == ['Agents can create tasks', 'Old rows still load']
    assert create(repo, owner='claude', reviewer='codex', request_text='Beau: make the Hub the backlog') == task
    with pytest.raises(HubError, match='idempotency_conflict'):
        create(repo, owner='claude')
    assert audit(repo, task['id']) == [('claude', 'task.created')]
    for environment in ('staging', 'production'):
        assert repo.task_allows_environment(task['id'], environment) is False
    # A human-created task keeps its legacy shape, fingerprint and environment semantics.
    legacy = repo.create_task(BEAU, PROJECT, 'Human task', 'k1')
    assert 'proposed_by' not in legacy and repo.task_allows_environment(legacy['id'], 'staging') is True
    assert repo.create_task(BEAU, PROJECT, 'Human task', 'k1') == legacy


@pytest.mark.parametrize('fields,code', [
    (dict(owner='codex', reviewer='codex'), 'independent_review_required'),
    (dict(owner='human:42'), 'invalid_owner'), (dict(reviewer='someone'), 'invalid_reviewer'),
    (dict(acceptance=['ok', '']), 'invalid_acceptance'), (dict(acceptance=['x'] * 21), 'invalid_acceptance'),
])
def test_create_rejects_invalid_backlog_fields(repo, fields, code):
    with pytest.raises(HubError, match=code):
        repo.create_task('claude', PROJECT, 'Task', 'key', **fields)


def test_named_owner_claims_and_others_cannot(repo):
    task = create(repo, owner='claude', reviewer='codex')
    with pytest.raises(HubError, match='forbidden'):
        repo.claim_task('hermes', task['id'], 1)
    assert repo.claim_task('claude', task['id'], 1)['status'] == 'claimed'


def test_update_permissions_owner_reviewer_human_only(repo):
    task = create(repo, owner='claude', reviewer='codex')
    with pytest.raises(HubError, match='forbidden'):
        repo.update_task('hermes', task['id'], 1, note='Not mine')
    task = repo.update_task('codex', task['id'], 1, note='Reviewer note')
    task = repo.update_task('claude', task['id'], 2, acceptance=['Only this'], note='Owner note')
    assert task['acceptance'] == ['Only this'] and task['details'] == 'Why it matters'
    assert [(n['by'], n['text']) for n in task['notes']] == [('codex', 'Reviewer note'), ('claude', 'Owner note')]
    task = repo.update_task(BEAU, task['id'], 3, reviewer='hermes')
    assert task['reviewer'] == 'hermes'
    with pytest.raises(HubError, match='forbidden'):
        repo.update_task('codex', task['id'], 4, note='No longer the reviewer')
    with pytest.raises(HubError, match='independent_review_required'):
        repo.update_task(BEAU, task['id'], 4, reviewer='claude')
    with pytest.raises(HubError, match='revision_conflict'):
        repo.update_task('claude', task['id'], 3, note='Stale')
    with pytest.raises(HubError, match='invalid_input'):
        repo.update_task('claude', task['id'], 4)


def test_reassignment_only_before_work_starts(repo):
    task = create(repo, owner='claude', reviewer='codex')
    task = repo.claim_task('claude', task['id'], 1)
    task = repo.update_task(BEAU, task['id'], 2, owner='hermes')
    assert task['owner'] == 'hermes' and task['status'] == 'assigned'
    task = repo.claim_task('hermes', task['id'], 3)
    task = repo.set_candidate('hermes', task['id'], 4, COMMIT)
    with pytest.raises(HubError, match='task_in_progress'):
        repo.update_task(BEAU, task['id'], 5, owner='claude')


def test_waiting_on_beau_round_trip_and_agent_cannot_clear(repo):
    task = repo.claim_task('claude', create(repo, owner='claude')['id'], 1)
    with pytest.raises(HubError, match='invalid_waiting_item'):
        repo.update_task('claude', task['id'], 2, status='waiting_on_beau')
    with pytest.raises(HubError, match='invalid_waiting_item'):
        repo.update_task('claude', task['id'], 2, status='waiting_on_beau', waiting={'type': 'vote', 'text': 'x'})
    task = repo.update_task('claude', task['id'], 2, status='waiting_on_beau',
                            waiting={'type': 'action', 'text': 'Add the DNS record.'})
    assert task['status'] == 'waiting_on_beau'
    assert task['waiting_on']['previous_status'] == 'claimed' and task['waiting_on']['by'] == 'claude'
    for status in ('claimed', 'done'):
        with pytest.raises(HubError, match='waiting_on_beau'):
            repo.update_task('claude', task['id'], 3, status=status, evidence='x')
    # The raising agent may withdraw only a decision item; this action item stays for Beau.
    with pytest.raises(HubError, match='waiting_action_needs_owner'):
        repo.withdraw('claude', 'waiting', task['id'], 3, 'No longer needed')
    with pytest.raises(HubError, match='forbidden'):
        repo.resolve_waiting('claude', task['id'], 3)
    with pytest.raises(HubError, match='waiting_on_beau'):
        repo.set_candidate('claude', task['id'], 3, COMMIT)
    assert repo.update_task('claude', task['id'], 3, note='Still blocked')['status'] == 'waiting_on_beau'
    resolved = repo.resolve_waiting(BEAU, task['id'], 4, 'Added it')
    assert resolved['status'] == 'claimed' and 'waiting_on' not in resolved
    assert resolved['waiting_resolved'] == dict(type='action', text='Add the DNS record.', by=BEAU,
                                                at=resolved['waiting_resolved']['at'], note='Added it')
    assert repo.resolve_waiting(BEAU, task['id'], 4, 'Added it') == resolved
    with pytest.raises(HubError, match='revision_conflict'):
        repo.resolve_waiting(BEAU, task['id'], 4, 'Different')
    assert audit(repo, task['id'])[-1] == (BEAU, 'task.waiting_resolved')
    state = {t['id']: t for t in repo.list_task_views()}[task['id']]
    assert state['state_since'] == resolved['updated_at']


def test_human_leaves_waiting_by_restoring_previous_status_only(repo):
    task = create(repo)
    task = repo.update_task(BEAU, task['id'], 1, status='waiting_on_beau',
                            waiting={'type': 'decision', 'text': 'Pick the domain name.'})
    with pytest.raises(HubError, match='invalid_transition'):
        repo.update_task(BEAU, task['id'], 2, status='verified')
    back = repo.update_task(BEAU, task['id'], 2, status='assigned')
    assert back['status'] == 'assigned' and back['waiting_resolved']['type'] == 'decision'


def test_done_rules_owner_after_verified_and_other_family_denied(repo):
    task = create(repo, owner='claude', reviewer='codex')
    claimed = repo.claim_task('claude', task['id'], 1)
    with pytest.raises(HubError, match='forbidden'):
        repo.update_task('claude', task['id'], claimed['revision'], status='done', evidence='Merged')
    task = repo.set_candidate('claude', claimed['id'], claimed['revision'], COMMIT)
    task = repo.review_task('codex', task['id'], task['revision'], COMMIT, 'approve')
    assert task['status'] == 'verified'
    for actor in ('codex', 'hermes'):
        with pytest.raises(HubError, match='forbidden'):
            repo.update_task(actor, task['id'], task['revision'], status='done', evidence='Not my task')
    with pytest.raises(HubError, match='invalid_evidence'):
        repo.update_task('claude', task['id'], task['revision'], status='done')
    done = repo.update_task('claude', task['id'], task['revision'], status='done', evidence='PR 200 merged')
    assert done['status'] == 'done' and done['completed_by'] == 'claude'
    assert done['completion_evidence'] == 'PR 200 merged'
    with pytest.raises(HubError, match='task_closed'):
        repo.update_task(BEAU, task['id'], done['revision'], note='Reopen?')
    with pytest.raises(HubError, match='task_closed'):
        repo.set_candidate('claude', task['id'], done['revision'], COMMIT)
    # Beau may close any open task, with or without evidence.
    other = repo.create_task(BEAU, PROJECT, 'Drop it', 'k9')
    closed = repo.update_task(BEAU, other['id'], 1, status='done')
    assert closed['status'] == 'done' and 'completion_evidence' not in closed
    assert repo.task_allows_environment(other['id'], 'staging') is False


def test_content_edit_invalidates_review_like_context_update(repo):
    task = verified(repo, create(repo, owner='claude', reviewer='codex'))
    edited = repo.update_task('claude', task['id'], task['revision'], acceptance=['Changed'])
    assert edited['status'] == 'claimed' and edited['review'] is None and edited['candidate_commit'] is None
    assert audit(repo, task['id'])[-1] == ('claude', 'task.context_invalidated')


def test_content_edit_on_waiting_task_keeps_it_waiting(repo):
    task = verified(repo, create(repo, owner='claude', reviewer='codex'))
    task = repo.update_task('claude', task['id'], task['revision'], status='waiting_on_beau',
                            waiting={'type': 'action', 'text': 'Merge the PR.'})
    edited = repo.update_context(BEAU, 'task', task['id'], task['revision'], 'New details', None)
    assert edited['status'] == 'waiting_on_beau' and edited['review'] is None
    assert edited['waiting_on']['previous_status'] == 'claimed'
    assert repo.resolve_waiting(BEAU, task['id'], edited['revision'])['status'] == 'claimed'


def test_notes_are_capped(repo):
    task = create(repo, owner='claude')
    for index in range(55):
        task = repo.update_task('claude', task['id'], task['revision'], note=f'n{index}')
    assert len(task['notes']) == 50 and task['notes'][-1]['text'] == 'n54' and task['notes'][0]['text'] == 'n5'


def test_old_task_rows_without_backlog_fields_still_load(repo):
    legacy = dict(id='00000000-0000-4000-8000-000000000001', project=PROJECT, title='Old', status='verified',
                  owner='codex', candidate_commit=COMMIT, review=dict(actor='claude', commit=COMMIT, verdict='approve'),
                  repair_count=0, revision=3, updated_at='2026-01-01T00:00:00+00:00')
    with repo._transaction() as db:
        db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?, 'task', ?)", (legacy['id'], json.dumps(legacy)))
    from engineering_hub.dto import TaskDTO
    assert TaskDTO.model_validate(repo.get_task(legacy['id'])).status == 'verified'
    done = repo.update_task('codex', legacy['id'], 3, status='done', evidence='Shipped')
    TaskDTO.model_validate(done)
    assert HubRepository.overview_task(done) == {k: v for k, v in done.items() if k != 'completion_evidence'}


def test_overview_task_omits_long_backlog_text(repo):
    task = repo.update_task('claude', create(repo, owner='claude', request_text='Long original request')['id'], 1,
                            note='progress')
    compact = HubRepository.overview_task(task)
    assert not {'acceptance', 'notes', 'request_text'} & compact.keys()
    assert compact['acceptance_count'] == 2 and compact['notes_count'] == 1 and compact['details'] == 'Why it matters'


@pytest.fixture
def hub(tmp_path):
    settings = Settings(tmp_path / 'hub.sqlite3', AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    app = create_app(settings)
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        yield client, app, settings


def agent(name):
    return {'Authorization': f'Bearer test-{name}'}


def beau(client, app, settings):
    session = app.state.sessions.create_session(42)
    client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
    return {'Origin': settings.auth_config.origin, 'X-Hub-CSRF': session['csrf_token']}


def test_rest_agent_create_update_and_beau_clears_action(hub):
    client, app, settings = hub
    body = {'project': PROJECT, 'title': 'Backlog', 'client_request_id': 'r1', 'acceptance': ['A'],
            'assignee': 'claude', 'assigned_reviewer': 'codex', 'request_text': 'From chat'}
    created = client.post(BASE + '/tasks', headers=agent('claude'), json=body)
    assert created.status_code == 201, created.text
    task = created.json()
    assert task['proposed_by'] == 'claude' and task['reviewer'] == 'codex' and task['acceptance'] == ['A']
    assert client.post(BASE + '/tasks', headers=agent('claude'), json=body).json()['id'] == task['id']
    update = client.post(f"{BASE}/tasks/{task['id']}/update", headers=agent('claude'), json={
        'expected_revision': 1, 'client_request_id': 'u1', 'status': 'waiting_on_beau',
        'waiting': {'type': 'action', 'text': 'Approve the spend.'}})
    assert update.status_code == 200, update.text
    assert update.json()['waiting_on']['type'] == 'action'
    denied = client.post(f"{BASE}/tasks/{task['id']}/resolve-waiting", headers=agent('claude'),
                         json={'expected_revision': 2})
    assert denied.status_code == 403
    human = beau(client, app, settings)
    cleared = client.post(f"{BASE}/tasks/{task['id']}/resolve-waiting", headers=human,
                          json={'expected_revision': 2, 'note': 'Done'})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['status'] == 'assigned' and cleared.json()['waiting_resolved']['note'] == 'Done'
    with app.state.repository._connection() as db:
        row = db.execute("SELECT actor, action FROM hub_audit WHERE entity_id=? ORDER BY sequence DESC LIMIT 1",
                         (task['id'],)).fetchone()
    assert tuple(row) == (BEAU, 'task.waiting_resolved')
    listed = client.get(BASE + '/tasks', headers=agent('hermes')).json()['items']
    assert listed[0]['waiting_resolved']['by'] == BEAU
    spoof = client.post(f"{BASE}/tasks/{task['id']}/update", headers=agent('claude'),
                        json={'expected_revision': 3, 'client_request_id': 'u2', 'note': 'x', 'actor': 'human:42'})
    assert spoof.status_code == 422


# Lead review fixes: each test below fails on the first version of this branch.

def test_agent_edits_content_only_on_its_own_familys_tasks(repo):
    beaus = repo.claim_task('claude', repo.create_task(BEAU, PROJECT, 'Beau task', 'h1', details='Original')['id'], 1)
    for fields in (dict(details='Rewritten'), dict(acceptance=['Rewritten']),
                   dict(links=[{'label': 'x', 'url': 'https://example.com'}])):
        with pytest.raises(HubError, match='forbidden'):
            repo.update_task('claude', beaus['id'], beaus['revision'], **fields)
    noted = repo.update_task('claude', beaus['id'], beaus['revision'], note='Progress is fine')
    assert noted['details'] == 'Original' and repo.task_allows_environment(beaus['id'], 'staging') is True
    codexs = repo.claim_task('claude', create(repo, actor='codex', key='c1', owner='claude')['id'], 1)
    with pytest.raises(HubError, match='forbidden'):
        repo.update_task('claude', codexs['id'], codexs['revision'], details='Not my family')
    assert repo.update_task('codex', codexs['id'], codexs['revision'], details='Creator edit')['details'] == 'Creator edit'
    assert repo.update_task(BEAU, beaus['id'], noted['revision'], details='Beau edit')['details'] == 'Beau edit'


def test_only_human_or_unclaimed_creator_reassigns(repo):
    task = create(repo, owner='claude', reviewer='codex')
    task = repo.claim_task('claude', task['id'], 1)
    for actor in ('codex', 'claude'):  # the reviewer, then the owner-creator after the claim
        with pytest.raises(HubError, match='forbidden'):
            repo.update_task(actor, task['id'], 2, owner='codex', reviewer='hermes')
        with pytest.raises(HubError, match='forbidden'):
            repo.update_task(actor, task['id'], 2, reviewer='hermes')
    assert repo.get_task(task['id'])['owner'] == 'claude'
    unclaimed = create(repo, actor='hermes', key='h2', owner='claude', reviewer='codex')
    moved = repo.update_task('hermes', unclaimed['id'], 1, owner='codex', reviewer='claude')
    assert (moved['owner'], moved['reviewer'], moved['status']) == ('codex', 'claude', 'assigned')
    with pytest.raises(HubError, match='forbidden'):
        repo.update_task('codex', unclaimed['id'], 2, reviewer='hermes')
    task = repo.set_candidate('claude', task['id'], 2, COMMIT)
    assert repo.update_task(BEAU, task['id'], task['revision'], reviewer='hermes')['reviewer'] == 'hermes'


def test_named_reviewer_binds_review(repo):
    task = create(repo, owner='claude', reviewer='codex')
    task = repo.claim_task('claude', task['id'], 1)
    task = repo.set_candidate('claude', task['id'], 2, COMMIT)
    with pytest.raises(HubError, match='named_reviewer_required'):
        repo.review_task('hermes', task['id'], 3, COMMIT, 'approve')
    with pytest.raises(HubError, match='independent_review_required'):
        repo.review_task('claude', task['id'], 3, COMMIT, 'approve')
    assert repo.review_task('codex', task['id'], 3, COMMIT, 'approve')['status'] == 'verified'
    open_task = repo.create_task(BEAU, PROJECT, 'No reviewer', 'h3')
    open_task = repo.claim_task('claude', open_task['id'], 1)
    open_task = repo.set_candidate('claude', open_task['id'], 2, COMMIT)
    assert repo.review_task('hermes', open_task['id'], 3, COMMIT, 'approve')['status'] == 'verified'


def test_update_request_id_replays_original_result_and_conflicts_on_change(repo):
    task = create(repo, owner='claude')
    first = repo.update_task('claude', task['id'], 1, note='Once', client_request_id='u1')
    later = repo.update_task('claude', task['id'], 2, note='Another')
    assert repo.update_task('claude', task['id'], 1, note='Once', client_request_id='u1') == first
    assert len(repo.get_task(task['id'])['notes']) == 2 and later['revision'] == 3
    with pytest.raises(HubError, match='idempotency_conflict'):
        repo.update_task('claude', task['id'], 1, note='Changed', client_request_id='u1')
    # Keys are per actor: another actor's identical key is a separate request.
    assert repo.update_task(BEAU, task['id'], 3, note='Beau', client_request_id='u1')['revision'] == 4


def test_creator_agent_withdraws_only_unclaimed_tasks(repo):
    task = repo.claim_task('codex', create(repo, key='w1')['id'], 1)
    with pytest.raises(HubError, match='forbidden'):
        repo.withdraw('claude', 'task', task['id'], 2, 'No longer needed')
    assert repo.withdraw(BEAU, 'task', task['id'], 2, 'No longer needed')['status'] == 'withdrawn'
    unclaimed = create(repo, key='w2', owner='codex')
    assert repo.withdraw('claude', 'task', unclaimed['id'], 1, 'Superseded')['status'] == 'withdrawn'
    assert repo.withdraw('claude', 'task', unclaimed['id'], 1, 'Superseded')['status'] == 'withdrawn'


def test_beau_marking_waiting_task_done_audits_the_item(repo):
    task = repo.update_task('claude', create(repo, owner='claude')['id'], 1, status='waiting_on_beau',
                            waiting={'type': 'action', 'text': 'Rotate the key.'})
    done = repo.update_task(BEAU, task['id'], 2, status='done')
    assert done['status'] == 'done' and 'waiting_on' not in done
    assert done['waiting_resolved']['text'] == 'Rotate the key.' and done['waiting_resolved']['by'] == BEAU
    assert audit(repo, task['id'])[-2:] == [(BEAU, 'task.waiting_resolved'), (BEAU, 'task.done')]


def test_rest_update_replay_and_bearer_cannot_resolve_waiting(hub):
    client, app, settings = hub
    task = client.post(BASE + '/tasks', headers=agent('claude'), json={
        'project': PROJECT, 'title': 'Replay', 'client_request_id': 'r9', 'assignee': 'claude'}).json()
    body = {'expected_revision': 1, 'client_request_id': 'u9', 'status': 'waiting_on_beau',
            'waiting': {'type': 'action', 'text': 'Sign the form.'}}
    first = client.post(f"{BASE}/tasks/{task['id']}/update", headers=agent('claude'), json=body)
    again = client.post(f"{BASE}/tasks/{task['id']}/update", headers=agent('claude'), json=body)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    changed = client.post(f"{BASE}/tasks/{task['id']}/update", headers=agent('claude'), json={**body, 'note': 'x'})
    assert changed.status_code == 409
    missing = client.post(f"{BASE}/tasks/{task['id']}/update", headers=agent('claude'),
                          json={'expected_revision': 2, 'note': 'x'})
    assert missing.status_code == 422
    # A human cookie plus any bearer, including a session-installation token, never clears the item.
    human = beau(client, app, settings)
    for token in ('Bearer ehs_' + 'a' * 43, 'Bearer test-claude'):
        response = client.post(f"{BASE}/tasks/{task['id']}/resolve-waiting",
                               headers={**human, 'Authorization': token}, json={'expected_revision': 2})
        assert response.status_code == 403
    assert app.state.repository.get_task(task['id'])['status'] == 'waiting_on_beau'
