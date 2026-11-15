from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from engineering_hub.repository import HubError
from tests.test_session_hub_bridge import PROJECT, call, env, pair


PROOF = [{'label': 'Proof', 'url': 'https://example.com/proof'}]


def test_claim_cannot_be_silently_replaced_or_cleared_and_retries_remain_idempotent(env):
    _, claimant, _ = pair(env, name='claim-integrity-owner', caps=('tracking.update', 'tracking.review'))
    _, other, _ = pair(env, name='claim-integrity-other', family='claude', caps=('tracking.update', 'tracking.review'))
    repo = env.repo
    task = repo.propose_tracking_task('codex', PROJECT, 'Work', 'integrity-task')
    def update(token, revision, key, claim):
        return call(env, token, 'hub_tracking_task_update', lambda actor: repo.update_tracking_task(
            actor, task['id'], revision, key, progress='Work', evidence_links=PROOF if claim else [], completion_claim=claim))
    claim = update(claimant, task['revision'], 'integrity-claim', True)
    for token in (claimant, other):
        for new_claim in (False, True):
            with pytest.raises(HubError, match='tracking_claim_pending'):
                update(token, claim['revision'], f'replace-{token[-4:]}-{new_claim}', new_claim)
    assert repo._read(task['id'], 'tracking_task') == claim
    assert update(claimant, task['revision'], 'integrity-claim', True) == claim
    with pytest.raises(HubError, match='idempotency_conflict'):
        update(claimant, task['revision'], 'integrity-claim', False)
    with pytest.raises(HubError, match='independent_review_required'):
        call(env, claimant, 'hub_tracking_task_review', lambda actor: repo.review_tracking_task(
            actor, task['id'], claim['revision'], 'self-review', verdict='verify', note='No', evidence_links=PROOF))
    reviewed = call(env, other, 'hub_tracking_task_review', lambda actor: repo.review_tracking_task(
        actor, task['id'], claim['revision'], 'independent-review', verdict='verify', note='Checked', evidence_links=PROOF))
    assert reviewed['status'] == 'completion_verified'
    assert update(claimant, task['revision'], 'integrity-claim', True) == reviewed


def test_legacy_claim_requires_independent_reopen_before_update(env):
    repo = env.repo
    task = repo.propose_tracking_task('codex', PROJECT, 'Legacy', 'legacy-integrity')
    claim = repo.update_tracking_task('codex', task['id'], task['revision'], 'legacy-claim',
                                      progress='Done', evidence_links=PROOF, completion_claim=True)
    with pytest.raises(HubError, match='tracking_claim_pending'):
        repo.update_tracking_task('codex', task['id'], claim['revision'], 'legacy-progress', progress='More', evidence_links=[])
    reopened = repo.review_tracking_task('claude', task['id'], claim['revision'], 'legacy-reopen',
                                         verdict='reopen', note='Work remains', evidence_links=[])
    assert repo.update_tracking_task('codex', task['id'], reopened['revision'], 'legacy-progress',
                                     progress='More', evidence_links=[])['status'] == 'open'


def test_update_and_review_race_cannot_replace_pending_claim(env):
    _, claimant, _ = pair(env, name='race-integrity-owner', caps=('tracking.update',))
    _, reviewer, _ = pair(env, name='race-integrity-reviewer', family='claude', caps=('tracking.update', 'tracking.review'))
    repo = env.repo
    task = repo.propose_tracking_task('codex', PROJECT, 'Race', 'integrity-race')
    claim = call(env, claimant, 'hub_tracking_task_update', lambda actor: repo.update_tracking_task(
        actor, task['id'], 1, 'race-claim', progress='Done', evidence_links=PROOF, completion_claim=True))
    barrier = threading.Barrier(2)
    def attempt_update():
        barrier.wait()
        try:
            return call(env, reviewer, 'hub_tracking_task_update', lambda actor: repo.update_tracking_task(
                actor, task['id'], claim['revision'], 'race-update', progress='More', evidence_links=[]))
        except HubError as exc:
            return exc.code
    def attempt_review():
        barrier.wait()
        return call(env, reviewer, 'hub_tracking_task_review', lambda actor: repo.review_tracking_task(
            actor, task['id'], claim['revision'], 'race-review', verdict='verify', note='Checked', evidence_links=PROOF))
    with ThreadPoolExecutor(max_workers=2) as pool:
        update_future, review_future = pool.submit(attempt_update), pool.submit(attempt_review)
        update_result, review_result = update_future.result(), review_future.result()
    assert update_result in ('tracking_claim_pending', 'revision_conflict')
    assert review_result['status'] == 'completion_verified'
