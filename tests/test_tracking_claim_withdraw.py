from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace

import pytest

from engineering_hub.repository import HubError
from tests.test_session_hub_bridge import OWNER, PROJECT, call, data, env, pair, sdk, unified


def test_claimant_withdrawal_preserves_history_and_requires_new_independent_review(env):
    _, claimant, _ = pair(env, name='claimant', caps=('tracking.update', 'tracking.read', 'tracking.review'))
    _, other, _ = pair(env, name='other-installation', caps=('tracking.update', 'tracking.read'))
    _, reviewer, _ = pair(env, name='reviewer', family='claude', caps=('tracking.review', 'tracking.read'))
    repo = env.repo
    task = repo.propose_tracking_task('codex', PROJECT, 'Work', 'initial')
    def update(revision, key, *, claim=False):
        return call(env, claimant, 'hub_tracking_task_update', lambda actor: repo.update_tracking_task(
            actor, task['id'], revision, key, progress='Claim' if claim else 'Progress',
            evidence_links=[{'label': 'Evidence', 'url': 'https://example.com/evidence'}] if claim else [],
            completion_claim=claim))
    for i in range(49):
        task = update(task['revision'], f'progress-{i}')
    task = update(task['revision'], 'claim-1', claim=True)
    claim_revision = task['revision']
    evidence = task['updates'][-1]['evidence_links']
    task = call(env, reviewer, 'hub_tracking_task_review', lambda actor: repo.review_tracking_task(
        actor, task['id'], task['revision'], 'review-1', verdict='verify', note='Verified', evidence_links=evidence))
    before = task
    def withdraw(token, revision, claim_rev, key, reason='Work remains'):
        return call(env, token, 'hub_tracking_claim_withdraw', lambda actor: repo.withdraw_tracking_claim(
            actor, task['id'], revision, claim_rev, key, reason=reason))
    with pytest.raises(HubError, match='tracking_claim_unavailable'):
        withdraw(other, task['revision'], claim_revision, 'other-installation')
    with pytest.raises(HubError, match='tracking_claim_unavailable'):
        withdraw(claimant, task['revision'], claim_revision - 1, 'wrong-claim')
    with pytest.raises(HubError, match='revision_conflict'):
        withdraw(claimant, task['revision'] - 1, claim_revision, 'stale')
    with pytest.raises(HubError):
        withdraw(claimant, task['revision'], claim_revision, 'empty', reason='')
    assert repo._read(task['id'], 'tracking_task') == before
    task = withdraw(claimant, task['revision'], claim_revision, 'withdraw-1')
    assert task['status'] == 'open' and task['reviews'][-1]['verdict'] == 'verify'
    assert task['updates'][-1]['claim_withdrawal'] and task['updates'][-1]['withdrawn_claim_revision'] == claim_revision
    assert task['history_segments']['updates'] >= 1
    with repo._connection() as db:
        assert not db.execute('SELECT 1 FROM hub_outbox WHERE entity_id=?', (task['id'],)).fetchone()
    assert any(item['id'] == task['id'] for item in repo.tracking_tasks(PROJECT, 100, None, unfinished=True))
    receipt_revision = task['revision'] - 1
    assert withdraw(claimant, receipt_revision, claim_revision, 'withdraw-1') == task
    with pytest.raises(HubError, match='idempotency_conflict'):
        withdraw(claimant, receipt_revision, claim_revision, 'withdraw-1', reason='Changed')
    task = update(task['revision'], 'claim-2', claim=True)
    with pytest.raises(HubError, match='independent_review_required'):
        call(env, claimant, 'hub_tracking_task_review', lambda actor: repo.review_tracking_task(
            actor, task['id'], task['revision'], 'self-review', verdict='verify', note='No', evidence_links=evidence))
    task = call(env, reviewer, 'hub_tracking_task_review', lambda actor: repo.review_tracking_task(
        actor, task['id'], task['revision'], 'review-2', verdict='verify', note='Again', evidence_links=evidence))
    assert task['status'] == 'completion_verified' and len(task['reviews']) == 2
    assert withdraw(claimant, receipt_revision, claim_revision, 'withdraw-1') == task
    assert task['status'] == 'completion_verified'


def test_missing_claim_revision_and_legacy_actors_fail_closed(env):
    _, token, _ = pair(env, caps=('tracking.update',))
    task = env.repo.propose_tracking_task('codex', PROJECT, 'Legacy', 'legacy')
    task = call(env, token, 'hub_tracking_task_update', lambda actor: env.repo.update_tracking_task(
        actor, task['id'], 1, 'claim', progress='Claim', evidence_links=[{'label': 'E', 'url': 'https://example.com/e'}], completion_claim=True))
    with env.repo._transaction() as db:
        item = env.repo._get(db, task['id'], 'tracking_task')
        del item['updates'][-1]['revision']
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), item['id']))
    with pytest.raises(HubError, match='tracking_claim_unavailable'):
        call(env, token, 'hub_tracking_claim_withdraw', lambda actor: env.repo.withdraw_tracking_claim(
            actor, task['id'], task['revision'], task['revision'], 'withdraw', reason='Incomplete'))
    with pytest.raises(HubError, match='tracking_claim_unavailable'):
        env.repo.withdraw_tracking_claim('codex', task['id'], task['revision'], task['revision'], 'legacy', reason='Incomplete')


def test_reviewer_and_withdrawal_race_one_wins(env):
    _, claimant, _ = pair(env, name='race-claimant', caps=('tracking.update',))
    _, reviewer, _ = pair(env, name='race-reviewer', family='claude', caps=('tracking.review',))
    repo = env.repo
    task = repo.propose_tracking_task('codex', PROJECT, 'Race', 'race-task')
    links = [{'label': 'Evidence', 'url': 'https://example.com/evidence'}]
    task = call(env, claimant, 'hub_tracking_task_update', lambda actor: repo.update_tracking_task(
        actor, task['id'], 1, 'race-claim', progress='Done', evidence_links=links, completion_claim=True))
    barrier = threading.Barrier(2)
    def attempt(token, tool, fn):
        barrier.wait(timeout=5)
        try:
            return call(env, token, tool, fn)['status']
        except HubError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(attempt, claimant, 'hub_tracking_claim_withdraw', lambda actor: repo.withdraw_tracking_claim(
            actor, task['id'], task['revision'], task['revision'], 'race-withdraw', reason='Incomplete'))
        b = pool.submit(attempt, reviewer, 'hub_tracking_task_review', lambda actor: repo.review_tracking_task(
            actor, task['id'], task['revision'], 'race-review', verdict='verify', note='Reviewed', evidence_links=links))
        results = [a.result(timeout=10), b.result(timeout=10)]
    assert sum(value in ('open', 'completion_verified') for value in results) == 1
    assert 'revision_conflict' in results


def test_claim_withdrawal_commit_guard_rolls_back(env):
    _, token, _ = pair(env, name='withdraw-fail-closed', caps=('tracking.update',))
    repo = env.repo
    task = repo.propose_tracking_task('codex', PROJECT, 'Guarded', 'guarded-task')
    task = call(env, token, 'hub_tracking_task_update', lambda actor: repo.update_tracking_task(
        actor, task['id'], 1, 'guarded-claim', progress='Claim',
        evidence_links=[{'label': 'E', 'url': 'https://example.com/e'}], completion_claim=True))
    with pytest.raises(HubError, match='injected_revocation'):
        with env.bridge.admit(token, 'hub_tracking_claim_withdraw') as operation:
            def reject(_db):
                raise HubError('injected_revocation', 403)
            operation.commit_checks.append(reject)
            repo.withdraw_tracking_claim(operation.principal.actor, task['id'], task['revision'],
                                         task['revision'], 'guarded-withdraw', reason='Incomplete')
    assert repo._read(task['id'], 'tracking_task') == task
    with repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_requests WHERE key='guarded-withdraw'").fetchone()[0] == 0
        assert not db.execute('SELECT 1 FROM hub_outbox WHERE entity_id=?', (task['id'],)).fetchone()


def test_real_mcp_scope_receipts_and_revocation(unified):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, name='withdraw-wire', caps=('tracking.propose', 'tracking.update'))
    invoke, _ = sdk(client, token)
    proposal = data(invoke('hub_tracking_task_propose', {'project': PROJECT, 'title': 'Scoped', 'client_request_id': 'p'}))
    identity = proposal['id']; receipt = {'id': identity, 'response_scope': 'mutation_receipt', 'current_state_included': False}
    links = [{'label': 'E', 'url': 'https://example.com/e'}]
    assert data(invoke('hub_tracking_task_update', {'task_id': identity, 'expected_revision': 1,
        'client_request_id': 'claim', 'progress': 'Done', 'evidence_links': links, 'completion_claim': True})) == receipt
    request = {'task_id': identity, 'expected_revision': 2, 'expected_claim_revision': 2,
               'client_request_id': 'withdraw', 'reason': 'Incomplete'}
    _, outsider, _ = pair(env, name='withdraw-outsider', caps=('tracking.update',), projects=('project-alpha',))
    other, _ = sdk(client, outsider)
    unavailable = other('hub_tracking_claim_withdraw', request)
    missing = other('hub_tracking_claim_withdraw', dict(request, task_id='00000000-0000-4000-8000-000000000000'))
    assert unavailable['isError'] and missing['isError']
    assert unavailable['content'][0]['text'] == missing['content'][0]['text']
    assert data(invoke('hub_tracking_claim_withdraw', request)) == receipt
    assert data(invoke('hub_tracking_claim_withdraw', request)) == receipt
    env.bridge.grant_revoke(OWNER, installation, 1)
    assert invoke('hub_tracking_claim_withdraw', request)['isError']
    assert app.state.repository._read(identity, 'tracking_task')['status'] == 'open'
