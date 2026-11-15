import copy
import json
import uuid
import threading
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from engineering_hub.repository import HubError, HubRepository
from engineering_hub.staging_resources import StagingResourceStore


PROFILE = {'schema': 'modular-staging-profile-v1', 'image_id': 'sha256:' + 'a' * 64,
           'build_argv': ['npm', 'run', 'build'], 'test_argv': ['npm', 'test'],
           'timeout_seconds': 30, 'memory_mb': 128, 'pids': 32, 'output_bytes': 4096,
           'log_bytes': 1024, 'workspace_bytes': 65536, 'cpu_millis': 250}
COMMIT = 'b' * 40
PROJECT = 'engineering-hub'


@pytest.fixture
def setup(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}))
    store.set_policy('human:1', PROJECT, 0, {'codex': ['read'], 'claude': ['read']})
    enrollment = store.enroll_profile('human:1', PROJECT, 'static', 0, PROFILE)
    task = repo.create_task('human:1', PROJECT, 'Reviewed profile work', str(uuid.uuid4()))
    task = repo.claim_task('codex', task['id'], task['revision'])
    return repo, store, enrollment, task


def confirmation(enrollment):
    return {key: enrollment[key] for key in ('project', 'recipe', 'generation', 'digest')}


def test_exact_owner_enrollment_and_independent_profile_review(setup):
    repo, store, enrollment, task = setup
    selection = {'recipe': 'static', 'generation': 1}
    candidate = repo.set_candidate('codex', task['id'], task['revision'], COMMIT, selection)
    assert 'staging_profile' not in candidate and 'staging_profile' not in repo.get_task(task['id'])
    bound = repo.get_task_staging_profile('claude', task['id'])
    assert bound['profile'] == PROFILE and bound['digest'] == enrollment['digest']
    assert bound['candidate_revision'] == candidate['revision'] and not bound['execution_authorization']
    with pytest.raises(HubError, match='staging_profile_unavailable'):
        repo.get_task_staging_profile('hermes', task['id'])
    with pytest.raises(HubError, match='staging_profile_unavailable'):
        repo.get_task_staging_profile('hermes', str(uuid.uuid4()))
    with pytest.raises(HubError, match='independent_review_required'):
        repo.review_task('codex', task['id'], candidate['revision'], COMMIT, 'approve', confirmation(enrollment))
    with pytest.raises(HubError, match='staging_profile_confirmation_required'):
        repo.review_task('claude', task['id'], candidate['revision'], COMMIT, 'approve')
    wrong = confirmation(enrollment); wrong['generation'] = True
    with pytest.raises(HubError, match='staging_profile_confirmation_required'):
        repo.review_task('claude', task['id'], candidate['revision'], COMMIT, 'approve', wrong)
    verified = repo.review_task('claude', task['id'], candidate['revision'], COMMIT, 'approve', confirmation(enrollment))
    assert verified['status'] == 'verified'
    assert repo.get_task_staging_profile('claude', task['id'])['review_revision'] == verified['revision']
    # Profile-bound execution is intentionally unavailable until the worker
    # protocol consumes the tuple. Do not downgrade to commit-only admission.
    assert not repo.reviewed_task_matches(task['id'], 'codex', verified['revision'], COMMIT)


def test_changed_enrollment_revoked_grant_and_stale_review_fail_closed(setup):
    repo, store, enrollment, task = setup
    candidate = repo.set_candidate('codex', task['id'], task['revision'], COMMIT, {'recipe': 'static', 'generation': 1})
    changed = copy.deepcopy(PROFILE); changed['memory_mb'] = 256
    store.enroll_profile('human:1', PROJECT, 'static', 1, changed)
    with pytest.raises(HubError, match='staging_profile_unavailable'):
        repo.review_task('claude', task['id'], candidate['revision'], COMMIT, 'approve', confirmation(enrollment))
    with pytest.raises(HubError, match='revision_conflict'):
        repo.review_task('claude', task['id'], candidate['revision'] - 1, COMMIT, 'approve', confirmation(enrollment))
    assert repo.get_task(task['id'])['status'] == 'in_review'
    store.set_policy('human:1', PROJECT, 1, {'codex': ['read']})
    with pytest.raises(HubError, match='staging_profile_unavailable'):
        repo.get_task_staging_profile('claude', task['id'])


def test_old_reviews_never_become_profile_reviews_and_ordinary_replacement_works(setup):
    repo, store, enrollment, task = setup
    candidate = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    with pytest.raises(HubError, match='staging_profile_confirmation_invalid'):
        repo.review_task('claude', task['id'], candidate['revision'], COMMIT, 'approve', confirmation(enrollment))
    reviewed = repo.review_task('claude', task['id'], candidate['revision'], COMMIT, 'approve')
    assert repo.reviewed_task_matches(task['id'], 'codex', reviewed['revision'], COMMIT)
    with pytest.raises(HubError, match='staging_profile_unavailable'):
        repo.get_task_staging_profile('claude', task['id'])
    # Replacing a bound candidate with an ordinary candidate drops active
    # binding but preserves append-only historical candidate evidence.
    bound = repo.set_candidate('codex', task['id'], reviewed['revision'], COMMIT, {'recipe': 'static', 'generation': 1})
    ordinary = repo.set_candidate('codex', task['id'], bound['revision'], COMMIT)
    plain = repo.review_task('claude', task['id'], ordinary['revision'], COMMIT, 'approve')
    assert repo.reviewed_task_matches(task['id'], 'codex', plain['revision'], COMMIT)
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_task_profiles WHERE task_id=?', (task['id'],)).fetchone()[0] == 1


def test_selection_is_server_resolved_and_same_project_only(setup):
    repo, store, enrollment, task = setup
    with pytest.raises(HubError, match='invalid_staging_profile'):
        repo.set_candidate('codex', task['id'], task['revision'], COMMIT,
                           {'recipe': 'static', 'generation': 1, 'digest': enrollment['digest']})
    with pytest.raises(HubError, match='staging_profile_unavailable'):
        repo.set_candidate('codex', task['id'], task['revision'], COMMIT,
                           {'recipe': 'other', 'generation': 1})
    assert repo.get_task(task['id'])['candidate_commit'] is None


def test_corrupt_string_grant_cannot_authorize_private_profile(setup):
    repo, store, _, task = setup
    repo.set_candidate('codex', task['id'], task['revision'], COMMIT, {'recipe': 'static', 'generation': 1})
    with repo._transaction() as db:
        row = db.execute('SELECT data FROM hub_staging_policy WHERE project=?', (PROJECT,)).fetchone()
        policy = json.loads(row['data']); policy['grants']['claude'] = 'read'
        db.execute('UPDATE hub_staging_policy SET data=? WHERE project=?', (json.dumps(policy), PROJECT))
    with pytest.raises(HubError, match='staging_profile_unavailable'):
        repo.get_task_staging_profile('claude', task['id'])
    with pytest.raises(HubError, match='staging_forbidden'):
        store.get_profile('claude', PROJECT, 'static')


@pytest.mark.parametrize('kind', ['task', 'enrollment'])
@pytest.mark.parametrize('order', ['read_first', 'revoke_first'])
def test_private_profile_read_serializes_with_project_grant_revocation(setup, monkeypatch, kind, order):
    repo, store, _, task = setup
    if kind == 'task':
        repo.set_candidate('codex', task['id'], task['revision'], COMMIT, {'recipe': 'static', 'generation': 1})
        read = lambda: repo.get_task_staging_profile('claude', task['id'])
        target, attr = repo, '_staging_profile_read'
    else:
        read = lambda: store.get_profile('claude', PROJECT, 'static')
        target, attr = store, '_authorize'
    entered, release, other_started = threading.Event(), threading.Event(), threading.Event()
    original = getattr(target, attr)

    def assert_hub_writer_locked():
        connection = sqlite3.connect(repo.path, timeout=0.1)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                connection.execute('BEGIN IMMEDIATE')
        finally:
            connection.close()

    def blocked_authorize(*args, **kwargs):
        value = original(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return value

    def read_result():
        other_started.set()
        try:
            return read()
        except HubError as error:
            return error.code

    if order == 'read_first':
        monkeypatch.setattr(target, attr, blocked_authorize)
        def revoke():
            other_started.set()
            return store.set_policy('human:1', PROJECT, 1, {'codex': ['read']})
        with ThreadPoolExecutor(max_workers=2) as executor:
            reader = executor.submit(read)
            assert entered.wait(5)
            assert_hub_writer_locked()
            other_started.clear()
            writer = executor.submit(revoke)
            assert other_started.wait(5)
            assert not writer.done()  # reader retains the SQLite write reservation
            release.set()
            assert reader.result(5)['profile'] == PROFILE
            assert writer.result(5)['revision'] == 2
        with pytest.raises(HubError): read()
    else:
        def before_commit(_):
            entered.set()
            assert release.wait(5)
        def revoke():
            return store.set_policy('human:1', PROJECT, 1, {'codex': ['read']}, admission_guard=before_commit)
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(revoke)
            assert entered.wait(5)
            assert_hub_writer_locked()
            reader = executor.submit(read_result)
            assert other_started.wait(5)
            assert not reader.done()  # reader cannot use the old authorization
            release.set()
            assert writer.result(5)['revision'] == 2
            assert reader.result(5) in {'staging_forbidden', 'staging_profile_unavailable'}


@pytest.mark.parametrize('kind', ['task', 'enrollment'])
@pytest.mark.parametrize('order', ['read_first', 'archive_first'])
def test_private_profile_read_serializes_with_project_archive(tmp_path, monkeypatch, kind, order):
    from engineering_hub.projects import ProjectRepository
    repo = HubRepository(tmp_path / 'archive-hub.sqlite3')
    projects = ProjectRepository(repo)
    project = '3d-viewer'
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}))
    store.set_policy('human:1', project, 0, {'codex': ['read'], 'claude': ['read']})
    store.enroll_profile('human:1', project, 'static', 0, PROFILE)
    task = repo.create_task('human:1', project, 'Fixture', 'archive-profile')
    task = repo.claim_task('codex', task['id'], task['revision'])
    repo.set_candidate('codex', task['id'], task['revision'], COMMIT, {'recipe': 'static', 'generation': 1})
    read = (lambda: repo.get_task_staging_profile('claude', task['id'])) if kind == 'task' else (lambda: store.get_profile('claude', project, 'static'))
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()

    def read_result():
        attempted.set()
        try: return read()
        except HubError as error: return error.code

    if order == 'read_first':
        target, name = (repo, '_staging_profile_read') if kind == 'task' else (store, '_authorize')
        original = getattr(target, name)
        def block_after_auth(*args, **kwargs):
            result = original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
            return result
        monkeypatch.setattr(target, name, block_after_auth)
        def archive():
            attempted.set()
            return projects.archive_project('human:1', project, 1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            reader = executor.submit(read)
            assert entered.wait(5)
            writer = executor.submit(archive)
            assert attempted.wait(5) and not writer.done()
            release.set()
            assert reader.result(5)['profile'] == PROFILE
            assert writer.result(5)['archived'] is True
        with pytest.raises(HubError): read()
    else:
        original = projects._save_project
        def block_before_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            assert release.wait(5)
            return result
        monkeypatch.setattr(projects, '_save_project', block_before_commit)
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(projects.archive_project, 'human:1', project, 1)
            assert entered.wait(5)
            reader = executor.submit(read_result)
            assert attempted.wait(5) and not reader.done()
            release.set()
            assert writer.result(5)['archived'] is True
            assert reader.result(5) in {'project_archived', 'staging_profile_unavailable'}

def test_bound_admission_resolves_exact_review_without_enabling_commit_only(setup):
    repo, store, enrollment, task = setup
    candidate = repo.set_candidate('codex', task['id'], task['revision'], COMMIT,
                                   {'recipe':'static', 'generation':1})
    reviewed = repo.review_task('claude', task['id'], candidate['revision'], COMMIT,
                                'approve', confirmation(enrollment))
    def resolve(**overrides):
        args = dict(identity=task['id'], actor='codex', revision=reviewed['revision'],
                    commit=COMMIT, project=PROJECT, recipe='static')
        args.update(overrides)
        with repo._transaction() as db:
            return repo.reviewed_staging_profile(db, **args)
    assert resolve() == confirmation(enrollment)
    for wrong in ({'actor':'hermes'}, {'revision':reviewed['revision']-1},
                  {'commit':'a'*40}, {'project':'3d-viewer'}, {'recipe':'other'}):
        assert resolve(**wrong) is None
    assert not repo.reviewed_task_matches(task['id'], 'codex', reviewed['revision'], COMMIT)
    changed = copy.deepcopy(PROFILE); changed['memory_mb'] = 256
    store.enroll_profile('human:1', PROJECT, 'static', 1, changed)
    assert resolve() is None
