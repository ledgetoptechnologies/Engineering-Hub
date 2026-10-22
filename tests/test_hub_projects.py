import json
import sqlite3
from contextlib import contextmanager

import pytest

from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository, PROJECTS


@pytest.fixture
def state(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    return repo, ProjectRepository(repo)


def create(projects, identity='client-notes', **fields):
    return projects.create_project('human:123', identity, 'Client notes', identity, **fields)


def test_seed_preserves_exact_ids_and_is_once(state):
    repo, projects = state
    assert {p['id'] for p in projects.list_projects()} == PROJECTS
    updated = projects.update_project('human:123', '3d-viewer', 1, name='Viewer module', parent_project='ledge-top-ops')
    assert ProjectRepository(repo).get_project('3d-viewer') == updated
    assert len(repo.list_audit()) == 1
    assert len(repo.list_outbox()) == 1


def test_composition_not_paths_or_subclasses(tmp_path):
    with pytest.raises(TypeError):
        ProjectRepository(tmp_path / 'other.sqlite3')


def test_non_repository_project_idempotency_and_history(state):
    repo, projects = state
    first = create(projects)
    assert first['repository_url'] is None
    assert create(projects) == first
    task = repo.create_task('human:123', first['id'], 'Remember this', 'task1')
    decision = repo.create_decision('codex', first['id'], 'Which?', ['a', 'b'])
    archived = projects.archive_project('human:123', first['id'], 1)
    assert archived['archived']
    assert first['id'] not in {p['id'] for p in projects.list_projects()}
    assert projects.get_project(first['id']) == archived
    assert archived in projects.list_projects(True)
    assert repo.get_task(task['id']) == task
    assert decision in repo.list_decisions()
    with pytest.raises(HubError, match='project_archived'):
        repo.create_task('human:123', first['id'], 'New', 'task2')
    with pytest.raises(HubError, match='project_exists'):
        projects.create_project('human:123', first['id'], 'Reused', 'other')
    assert len(repo.list_audit()) == len(repo.list_outbox()) == 4


@pytest.mark.parametrize('actor', ['codex', 'claude', 'hermes', 'human:fake', 'executor:ci'])
def test_agents_cannot_mutate_metadata(state, actor):
    _, projects = state
    first = create(projects)
    for call in [lambda: projects.create_project(actor, 'new-one', 'New', 'key'),
                 lambda: projects.update_project(actor, first['id'], 1, name='Changed'),
                 lambda: projects.archive_project(actor, first['id'], 1)]:
        with pytest.raises(HubError, match='forbidden'):
            call()
    assert projects.get_project(first['id']) == first


def test_cas_idempotency_and_system_root(state):
    _, projects = state
    first = create(projects)
    with pytest.raises(HubError, match='idempotency_conflict'):
        projects.create_project('human:123', first['id'], 'Different', first['id'])
    updated = projects.update_project('human:123', first['id'], 1, status='paused', repository_url='https://example.com/repo')
    assert updated['revision'] == 2
    for revision in [True, 1, None]:
        with pytest.raises(HubError, match='revision_conflict'):
            projects.archive_project('human:123', first['id'], revision)
    with pytest.raises(HubError, match='system_project_required'):
        projects.archive_project('human:123', 'engineering-hub', 1)


def test_parent_cycles_missing_archived(state):
    _, projects = state
    create(projects, 'parent')
    create(projects, 'child', parent_project='parent')
    with pytest.raises(HubError, match='project_parent_cycle'):
        projects.update_project('human:123', 'parent', 1, parent_project='child')
    with pytest.raises(HubError, match='not_found'):
        create(projects, 'missing-parent', parent_project='missing')
    projects.archive_project('human:123', 'child', 1)
    with pytest.raises(HubError, match='project_archived'):
        create(projects, 'archived-parent', parent_project='child')


@pytest.mark.parametrize('fields', [dict(name='x' * 101), dict(description='x' * 4097),
    dict(repository_url='http://example.com'), dict(repository_url='https://u:p@example.com'),
    dict(repository_url='https://example.com/\n'), dict(repository_url=[]),
    dict(repository_url='https://example.com:0/'), dict(repository_url='https://@example.com/'),
    dict(repository_url='https://example.com/\\other'), dict(repository_url='https://example.com/%'),
    dict(description='\ud800'), dict(description='bad\x7f'),
    dict(lead_agent='external'), dict(status='deployed'), dict(parent_project='../bad')])
def test_bounded_metadata(state, fields):
    _, projects = state
    values = dict(project_id='new-project', name='Name', client_request_id='key')
    values.update(fields)
    with pytest.raises(HubError):
        projects.create_project('human:123', **values)
    assert len(projects.list_projects()) == 5


def test_same_database_and_bare_legacy_validation(tmp_path):
    first = HubRepository(tmp_path / 'first.sqlite3')
    other = HubRepository(tmp_path / 'other.sqlite3')
    first._project('treedar')
    connection = sqlite3.connect(first.path)
    try:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='hub_projects'").fetchone()
    finally:
        connection.close()
    create(ProjectRepository(first))
    first.create_decision('claude', 'client-notes', 'Choose', ['one', 'two'])
    with pytest.raises(HubError, match='invalid_project'):
        other._project('client-notes')
    with pytest.raises(HubError, match='invalid_project'):
        first._project(['bad'])


def test_transaction_rollback_preserves_audit_and_metadata(state, monkeypatch):
    repo, projects = state
    def fail(*args):
        raise HubError('owner_lost', 409)
    monkeypatch.setattr(repo, '_emit', fail)
    with pytest.raises(HubError, match='owner_lost'):
        create(projects)
    assert len(projects.list_projects()) == 5
    assert repo.list_audit() == []


def test_archive_refuses_live_run_and_queue(state):
    repo, projects = state
    create(projects)
    task = repo.create_task('human:123', 'client-notes', 'Work', 'key')
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_entities VALUES(?,?,?)', ('run', 'run', json.dumps(
            dict(id='run', task_id=task['id'], status='held'))))
    with pytest.raises(HubError, match='active_run'):
        projects.archive_project('human:123', 'client-notes', 1)
    with repo._transaction() as db:
        db.execute("DELETE FROM hub_entities WHERE id='run'")
        db.execute('CREATE TABLE hub_work_jobs(project TEXT,state TEXT)')
        db.execute("INSERT INTO hub_work_jobs VALUES('client-notes','claimed')")
    with pytest.raises(HubError, match='active_job'):
        projects.archive_project('human:123', 'client-notes', 1)


def test_cap_includes_archived_records(state):
    repo, projects = state
    with repo._transaction() as db:
        for number in range(995):
            identity = f'old-{number}'
            db.execute('INSERT INTO hub_projects VALUES(?,?)', (identity, json.dumps(
                dict(id=identity, archived=True))))
    with pytest.raises(HubError, match='project_limit'):
        create(projects)
    assert len(projects.list_projects(True)) == 1000


def test_dynamic_label_project_not_execution_permission(state):
    from engineering_hub.label_sets import LabelSetRepository
    repo, projects = state
    create(projects)
    labels = LabelSetRepository(repo)
    item = labels.create(set_id='first', project='client-notes', title='Review',
                         choices=[dict(key='yes', label='Yes'), dict(key='no', label='No')],
                         items=[dict(id='sample')], actor='codex')
    assert item['project'] == 'client-notes'
    from engineering_hub.work_queue_config import PROJECTS as EXECUTION_PROJECTS
    assert 'client-notes' not in EXECUTION_PROJECTS


def test_owner_check_at_commit_rolls_back(state, monkeypatch):
    repo, projects = state
    original = repo._assert_pilot_owner
    checks = 0
    def check(db):
        nonlocal checks
        checks += 1
        if checks == 4:
            raise HubError('pilot_ownership_required', 409)
        return original(db)
    monkeypatch.setattr(repo, '_assert_pilot_owner', check)
    with pytest.raises(HubError, match='pilot_ownership_required'):
        create(projects)
    assert len(projects.list_projects()) == 5
    assert repo.list_audit() == []


def test_archived_project_blocks_existing_work_mutations_and_dispatch(state):
    repo, projects = state
    create(projects)
    assigned = repo.create_task('human:123', 'client-notes', 'Assigned', 'assigned')
    claimed = repo.create_task('human:123', 'client-notes', 'Claimed', 'claimed')
    claimed = repo.claim_task('codex', claimed['id'], claimed['revision'])
    review = repo.create_task('human:123', 'client-notes', 'Review', 'review')
    review = repo.claim_task('codex', review['id'], review['revision'])
    review = repo.set_candidate('codex', review['id'], review['revision'], 'a' * 40)
    verified = repo.create_task('human:123', 'client-notes', 'Verified', 'verified')
    verified = repo.claim_task('codex', verified['id'], verified['revision'])
    verified = repo.set_candidate('codex', verified['id'], verified['revision'], 'a' * 40)
    verified = repo.review_task('claude', verified['id'], verified['revision'], 'a' * 40, 'approve')
    decision = repo.create_decision('claude', 'client-notes', 'Which?', ['one', 'two'])
    proposal = repo.propose_staging_task('codex', 'client-notes', 'Proposal', 'proposal')
    assert repo.task_allows_environment(proposal['id'], 'staging')
    assert not repo.task_allows_environment(proposal['id'], 'production')
    projects.archive_project('human:123', 'client-notes', 1)
    before = len(repo.list_audit())
    for call in [
        lambda: repo.claim_task('codex', assigned['id'], assigned['revision']),
        lambda: repo.set_candidate('codex', claimed['id'], claimed['revision'], 'b' * 40),
        lambda: repo.review_task('claude', review['id'], review['revision'], 'a' * 40, 'approve'),
        lambda: repo.update_context('human:123', 'task', assigned['id'], assigned['revision'], 'New', []),
        lambda: repo.update_context('human:123', 'decision', decision['id'], decision['revision'], 'New', []),
        lambda: repo.answer_decision('human:123', decision['id'], decision['revision'], 'one'),
        lambda: repo.create_run('codex', verified['id'], verified['revision'], 'sha256:' + 'b' * 64, 'client-notes:staging'),
        lambda: repo.propose_staging_task('codex', 'client-notes', 'New', 'new-proposal'),
        lambda: repo.create_decision('claude', 'client-notes', 'New', ['one', 'two']),
    ]:
        with pytest.raises(HubError, match='project_archived'):
            call()
    for item in [assigned, claimed, review, verified, proposal]:
        assert repo.get_task(item['id']) == item
        assert not repo.task_allows_environment(item['id'], 'staging')
        assert not repo.task_allows_environment(item['id'], 'production')
    assert len(repo.list_audit()) == before


@pytest.mark.parametrize('operation', ['create_task', 'propose_staging_task', 'create_decision'])
def test_archive_before_write_transaction_is_rechecked(state, monkeypatch, operation):
    repo, projects = state
    create(projects)
    original = repo._transaction
    armed = True
    @contextmanager
    def transaction():
        nonlocal armed
        # Another writer archives before this operation obtains its write
        # transaction. Admission must inspect the current project inside it.
        if armed:
            armed = False
            projects.archive_project('human:123', 'client-notes', 1)
        with original() as db:
            yield db
    monkeypatch.setattr(repo, '_transaction', transaction)
    with pytest.raises(HubError, match='project_archived'):
        if operation == 'create_decision':
            repo.create_decision('codex', 'client-notes', 'Which?', ['one', 'two'])
        else:
            actor = 'human:123' if operation == 'create_task' else 'codex'
            getattr(repo, operation)(actor, 'client-notes', 'New work', 'new-key')
    assert repo.list_tasks() == []
    assert repo.list_decisions() == []


def test_terminal_run_receipt_remains_readable_after_archive(state):
    repo, projects = state
    create(projects)
    task = repo.create_task('human:123', 'client-notes', 'Work', 'work')
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], 'a' * 40)
    task = repo.review_task('claude', task['id'], task['revision'], 'a' * 40, 'approve')
    digest = 'sha256:' + 'b' * 64
    run = repo.create_run('codex', task['id'], task['revision'], digest,
                          'client-notes:staging', client_request_id='run-key')
    with repo._transaction() as db:
        run['status'] = 'succeeded'
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(run), run['id']))
    projects.archive_project('human:123', 'client-notes', 1)
    before = len(repo.list_audit())
    assert repo.create_run('codex', task['id'], task['revision'], digest,
                           'client-notes:staging', client_request_id='run-key') == run
    assert len(repo.list_audit()) == before
