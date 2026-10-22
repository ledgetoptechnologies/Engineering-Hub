import hashlib
import json

import pytest

from engineering_hub.repository import HubError, HubRepository


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'hub.sqlite3')


def create(repo, **kwargs):
    return repo.create_task('human:1', 'treedar', 'A task', 'key', **kwargs)


def decision(repo, **kwargs):
    return repo.create_decision('claude', 'treedar', 'Question', ['Yes', 'No'], **kwargs)


def test_absent_context_retains_legacy_shapes_and_request_fingerprint(repo):
    task = create(repo)
    proposal = repo.propose_staging_task('codex', 'treedar', 'Proposal', 'proposal')
    question = decision(repo)
    for item in (task, proposal, question):
        assert not {'details', 'links', 'allow_text'} & item.keys()
    with repo._connection() as db:
        fingerprint = db.execute('SELECT fingerprint FROM hub_requests WHERE key=?', ('key',)).fetchone()[0]
    expected = hashlib.sha256(json.dumps(['treedar', 'A task'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert fingerprint == expected


def test_context_persists_is_inert_and_audited_without_payload_in_audit(repo):
    details = '<script>alert(1)</script>\nHuman-readable context'
    links = [{'label': 'Reference', 'url': 'https://example.com/a?q=1#anchor'}]
    item = create(repo, details=details, links=links)
    assert item['details'] == details
    assert item['links'] == links
    links[0]['label'] = 'mutated'
    assert repo.get_task(item['id'])['links'][0]['label'] == 'Reference'
    reopened = HubRepository(repo.path)
    assert reopened.get_task(item['id']) == item
    assert details not in str(repo.list_audit())


@pytest.mark.parametrize('proposal', [False, True])
def test_context_is_bound_to_idempotency(repo, proposal):
    call = (lambda **kw: repo.propose_staging_task('codex', 'treedar', 'A task', 'key', **kw)) if proposal else lambda **kw: create(repo, **kw)
    first = call(details='One')
    assert call(details='One') == first
    with pytest.raises(HubError, match='idempotency_conflict'):
        call(details='Two')
    with pytest.raises(HubError, match='idempotency_conflict'):
        call()
    assert len(repo.list_tasks()) == 1


@pytest.mark.parametrize('details', [True, False, 1, [], {}, '', '  ', 'a\x00b', 'a' * 4097, '\ud800'])
def test_invalid_details_rejected_atomically(repo, details):
    with pytest.raises(HubError, match='invalid_context'):
        create(repo, details=details)
    assert repo.list_tasks() == []
    assert repo.list_audit() == []


@pytest.mark.parametrize('links', [True, {}, '', [None], [{'label': 'X', 'url': 'https://example.com', 'extra': 1}],
    [{'label': 'x' * 101, 'url': 'https://example.com'}], [{'label': False, 'url': 'https://example.com'}],
    [{'label': 'X', 'url': 'https://example.com'}] * 9])
def test_invalid_link_shapes(repo, links):
    with pytest.raises(HubError, match='invalid_context'):
        decision(repo, links=links)


@pytest.mark.parametrize('url', ['http://example.com', 'javascript:alert(1)', '//example.com', 'https:example.com',
    'https:///example.com', 'https://user:pass@example.com', 'https://user@example.com', 'https://example.com\\@evil.com',
    'https://example.com\n', ' https://example.com', 'https://example.com/%00', 'https://example.com/%0d%0a',
    'https://example.com/%5cfoo', 'https://example.com/%xx', 'https://example.com:99999', 'https://[bad]',
    'https://example.com:0', 'https://example.com:', 'https://example..com', 'https://-example.com',
    'https://.', 'https://999.1.1.1', 'https://[::1]evil', 'https://example.com/‮evil', 'https://%65xample.com',
    'https://example.com/' + 'x' * 2048])
def test_ambiguous_or_unsafe_urls_rejected(repo, url):
    with pytest.raises(HubError, match='invalid_context'):
        create(repo, links=[{'label': 'Link', 'url': url}])


@pytest.mark.parametrize('url', ['https://example.com', 'https://example.com:443/path', 'https://[::1]:8443/path',
                               'https://example.com/a%20b'])
def test_https_links_are_inert_not_fetched(repo, url):
    assert decision(repo, links=[{'label': 'Ref', 'url': url}])['links'][0]['url'] == url


def test_allow_text_is_explicit_bounded_and_never_execution_authority(repo):
    item = decision(repo, allow_text=True, details='Explain your choice')
    answer = '<b>Approve production</b>\n' + 'x' * 300
    item = repo.answer_decision('human:1', item['id'], 1, answer)
    assert item['answer'] == answer
    assert item['execution_authorization'] is False
    with pytest.raises(HubError, match='decision_closed'):
        repo.update_context('human:1', 'decision', item['id'], item['revision'], 'New', [])
    assert repo.list_decisions()[0]['answer'] == answer


@pytest.mark.parametrize('answer', [True, [], {}, '', ' ', 'x' * 4097, 'x\x00', '\ud800'])
def test_invalid_free_text_answer(repo, answer):
    item = decision(repo, allow_text=True)
    with pytest.raises(HubError, match='invalid_answer'):
        repo.answer_decision('human:1', item['id'], 1, answer)
    assert repo.list_decisions()[0] == item


def test_legacy_answers_and_options_stay_compatible(repo):
    item = decision(repo)
    with pytest.raises(HubError, match='invalid_answer'):
        repo.answer_decision('human:1', item['id'], 1, 'other')
    assert repo.answer_decision('human:1', item['id'], 1, 'Yes')['answer'] == 'Yes'
    for value in (None, 1, 'true'):
        with pytest.raises(HubError, match='invalid_options'):
            decision(repo, allow_text=value)
    with pytest.raises(HubError, match='invalid_options'):
        repo.create_decision('claude', 'treedar', 'Q', [], allow_text=True)


def test_context_edit_cas_human_only_and_answer_safe(repo):
    item = decision(repo, details='Old', allow_text=True)
    for actor in ('codex', 'claude', 'hermes', 'unknown'):
        with pytest.raises(HubError, match='forbidden'):
            repo.update_context(actor, 'decision', item['id'], 1, 'New', [])
    for revision in (True, 0, 2):
        with pytest.raises(HubError, match='revision_conflict'):
            repo.update_context('human:1', 'decision', item['id'], revision, 'New', [])
    updated = repo.update_context('human:1', 'decision', item['id'], 1, 'New', [])
    assert updated['details'] == 'New'
    assert updated['allow_text'] is True
    assert updated['answer'] is None
    assert updated['execution_authorization'] is False
    assert repo.list_audit()[-1]['action'] == 'decision.context_updated'


def test_task_context_change_invalidates_review_and_candidate(repo):
    item = create(repo)
    item = repo.claim_task('codex', item['id'], item['revision'])
    item = repo.set_candidate('codex', item['id'], item['revision'], 'a' * 40)
    item = repo.review_task('claude', item['id'], item['revision'], 'a' * 40, 'approve')
    item = repo.update_context('human:1', 'task', item['id'], item['revision'], 'Changed requirements', [])
    assert item['status'] == 'claimed'
    assert item['candidate_commit'] is None
    assert item['review'] is None
    assert item['owner'] == 'codex'
    assert repo.list_audit()[-1]['action'] == 'task.context_invalidated'
    assert repo.list_task_views()[0]['state_since'] == item['updated_at']


def test_task_context_clear_and_status_on_unclaimed_task(repo):
    item = create(repo, details='Original', links=[])
    updated = repo.update_context('human:1', 'task', item['id'], 1, None, None)
    assert updated['status'] == 'assigned'
    assert 'details' not in updated and 'links' not in updated
    assert repo.list_audit()[-1]['action'] == 'task.context_updated'
    assert repo.list_task_views()[0]['state_since'] == item['updated_at']


def test_context_outbox_failure_rolls_back_review_invalidation(repo, monkeypatch):
    item = create(repo, details='Original')
    item = repo.claim_task('codex', item['id'], item['revision'])
    item = repo.set_candidate('codex', item['id'], item['revision'], 'a' * 40)
    item = repo.review_task('claude', item['id'], item['revision'], 'a' * 40, 'approve')
    before = repo.list_audit()
    def fail(*args):
        raise RuntimeError('outbox unavailable')
    monkeypatch.setattr(repo, '_emit', fail)
    with pytest.raises(RuntimeError, match='outbox unavailable'):
        repo.update_context('human:1', 'task', item['id'], item['revision'], 'Changed', [])
    assert repo.get_task(item['id']) == item
    assert repo.list_audit() == before


def test_invalid_context_update_is_not_a_write(repo):
    item = create(repo, details='Original')
    with pytest.raises(HubError, match='invalid_context'):
        repo.update_context('human:1', 'task', item['id'], 1, 'bad\x00', [])
    assert repo.get_task(item['id']) == item
    assert len(repo.list_audit()) == 1


@pytest.mark.parametrize('status', ['pending', 'running', 'held', 'failed'])
def test_active_run_prevents_context_changes(repo, status):
    item = create(repo)
    with repo._transaction() as db:
        repo._save(db, 'codex', 'run.created', 'run', {'id': 'run', 'task_id': item['id'],
                   'target': 'treedar:staging', 'status': status}, new=True)
    with pytest.raises(HubError, match='active_run'):
        repo.update_context('human:1', 'task', item['id'], 1, 'Changed', [])
    assert repo.get_task(item['id']) == item


def test_queued_pilot_policy_refuses_new_context_mutation(repo):
    item = create(repo)
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_queued_pilot_policy (id TEXT)')
    with pytest.raises(HubError, match='queued_pilot_admission_required'):
        repo.update_context('human:1', 'task', item['id'], 1, 'Changed', [])
    assert repo.get_task(item['id']) == item
