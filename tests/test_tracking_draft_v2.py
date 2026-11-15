from concurrent.futures import ThreadPoolExecutor
import secrets
import threading

import pytest

from engineering_hub.repository import HubError
from tests.test_session_hub_bridge import PROJECT, call, env, pair, unified


def test_v2_intake_concurrent_installations_preserves_creator_and_conflicts(env):
    _, first, _ = pair(env, name='intake-first', caps=('tracking.propose', 'tracking.read'))
    _, second, _ = pair(env, name='intake-second', caps=('tracking.propose', 'tracking.read'))
    intake = secrets.token_urlsafe(32)
    barrier = threading.Barrier(2)
    def create(token):
        barrier.wait(timeout=5)
        return call(env, token, 'hub_tracking_draft_propose', lambda actor: env.repo.propose_tracking_draft(
            actor, PROJECT, 'Synthetic work', 'stable-request', intake))
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(create, first)
        b = pool.submit(create, second)
        first_result, second_result = a.result(), b.result()
    assert first_result['id'] == second_result['id']
    assert first_result['created_by'] == second_result['created_by']
    with env.repo._connection() as db:
        rows = db.execute("SELECT data FROM hub_entities WHERE kind='tracking_intake_receipt'").fetchall()
        assert len(rows) == 1 and intake not in rows[0]['data']
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='tracking_task'").fetchone()[0] == 1
    with pytest.raises(HubError, match='idempotency_conflict|tracking_draft_conflict'):
        call(env, second, 'hub_tracking_draft_propose', lambda actor: env.repo.propose_tracking_draft(
            actor, PROJECT, 'Changed', 'stable-request', intake))
    assert call(env, first, 'hub_tracking_draft_propose', lambda actor: env.repo.propose_tracking_draft(
        actor, PROJECT, 'Synthetic work', 'stable-request', intake))['id'] == first_result['id']


def test_v2_requires_enrolled_installation_and_canonical_intake(env):
    _, token, _ = pair(env, name='intake-validation', caps=('tracking.propose',))
    with pytest.raises(HubError):
        env.repo.propose_tracking_draft('codex', PROJECT, 'Work', 'key', secrets.token_urlsafe(32))
    with pytest.raises(HubError, match='invalid_tracking_intake'):
        call(env, token, 'hub_tracking_draft_propose', lambda actor: env.repo.propose_tracking_draft(
            actor, PROJECT, 'Work', 'key', 'short'))


def test_intake_quotas_allow_registered_retries(env, monkeypatch):
    from engineering_hub import repository
    monkeypatch.setattr(repository, '_TRACKING_DRAFT_PROJECT_LIMIT', 1)
    monkeypatch.setattr(repository, '_TRACKING_DRAFT_REGISTRATION_LIMIT', 1)
    _, first, _ = pair(env, name='quota-first', caps=('tracking.propose',))
    _, second, _ = pair(env, name='quota-second', caps=('tracking.propose',))
    intake = secrets.token_urlsafe(32)
    def submit(token, identity):
        return call(env, token, 'hub_tracking_draft_propose', lambda actor: env.repo.propose_tracking_draft(
            actor, PROJECT, 'Quota', 'quota-key', identity))
    original = submit(first, intake)
    assert submit(first, intake)['id'] == original['id']
    with pytest.raises(HubError, match='tracking_draft_capacity'):
        submit(second, intake)
    with pytest.raises(HubError, match='tracking_draft_capacity'):
        call(env, first, 'hub_tracking_draft_propose', lambda actor: env.repo.propose_tracking_draft(
            actor, PROJECT, 'Other', 'other-key', secrets.token_urlsafe(32)))
    assert submit(first, intake)['id'] == original['id']


def test_v2_wire_write_only_projection_and_scope(unified):
    from types import SimpleNamespace
    from tests.test_session_hub_bridge import OWNER, data, sdk
    client, app, bus, _ = unified
    scope = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(scope, name='intake-wire', caps=('tracking.propose',))
    invoke, _ = sdk(client, token)
    body = dict(project=PROJECT, title='Private', client_request_id='wire-key', intake_id=secrets.token_urlsafe(32))
    receipt = data(invoke('hub_tracking_draft_propose', body))
    assert set(receipt) == {'id', 'response_scope', 'current_state_included'}
    assert receipt['response_scope'] == 'mutation_receipt' and not receipt['current_state_included']
    assert data(invoke('hub_tracking_draft_propose', body)) == receipt
    _, replacement, _ = pair(scope, name='intake-wire-replacement', caps=('tracking.propose',))
    replacement_invoke, _ = sdk(client, replacement)
    assert data(replacement_invoke('hub_tracking_draft_propose', body)) == receipt
    changed = dict(body, title='Changed')
    assert replacement_invoke('hub_tracking_draft_propose', changed)['isError']
    _, foreign, _ = pair(scope, name='intake-foreign', caps=('tracking.propose',), projects=('project-alpha',))
    other, _ = sdk(client, foreign)
    assert other('hub_tracking_draft_propose', body)['isError']
    scope.bridge.grant_revoke(OWNER, installation, 1)
    assert invoke('hub_tracking_draft_propose', body)['isError']


def test_v2_origin_requires_scope_and_live_dispatch_read(env):
    from tests.test_dispatch_coordination import setup_dispatch
    _, _, _, _, _, _, request, _ = setup_dispatch(env)
    _, allowed, _ = pair(env, name='origin-allowed', caps=('tracking.propose', 'dispatch.read'))
    _, denied, _ = pair(env, name='origin-denied', caps=('tracking.propose',))
    identity = secrets.token_urlsafe(32)
    def submit(token, request_id):
        return call(env, token, 'hub_tracking_draft_propose', lambda actor: env.repo.propose_tracking_draft(
            actor, PROJECT, 'Origin', 'origin-key', identity, origin_request_id=request_id))
    task = submit(allowed, request['id'])
    assert submit(allowed, request['id'])['id'] == task['id']
    with pytest.raises(HubError):
        submit(denied, request['id'])
    with pytest.raises(HubError):
        submit(allowed, '00000000-0000-4000-8000-000000000000')


def test_v2_original_creator_family_cannot_verify_replayed_claim(env):
    _, first, _ = pair(env, name='review-original', family='codex', caps=('tracking.propose', 'tracking.update'))
    _, replacement, _ = pair(env, name='review-replacement', family='claude', caps=('tracking.propose',))
    _, sibling, _ = pair(env, name='review-sibling', family='codex', caps=('tracking.review',))
    _, reviewer, _ = pair(env, name='review-independent', family='claude', caps=('tracking.review',))
    identity = secrets.token_urlsafe(32)
    def submit(token):
        return call(env, token, 'hub_tracking_draft_propose', lambda actor: env.repo.propose_tracking_draft(
            actor, PROJECT, 'Review', 'review-key', identity))
    task = submit(first)
    assert submit(replacement)['created_by'] == task['created_by']
    links = [{'label': 'Evidence', 'url': 'https://example.com/evidence'}]
    task = call(env, first, 'hub_tracking_task_update', lambda actor: env.repo.update_tracking_task(
        actor, task['id'], 1, 'claim', progress='Done', evidence_links=links, completion_claim=True))
    def verify(token):
        return call(env, token, 'hub_tracking_task_review', lambda actor: env.repo.review_tracking_task(
            actor, task['id'], 2, 'review', verdict='verify', note='Reviewed', evidence_links=links))
    with pytest.raises(HubError, match='independent_review_required'):
        verify(sibling)
    assert verify(reviewer)['status'] == 'completion_verified'


def test_v2_commit_guard_failure_rolls_back_all_intake_state(env, monkeypatch):
    _, token, _ = pair(env, name='intake-expiry', caps=('tracking.propose',))
    from agent_message_bus import session_hub_context as session_context
    before = session_context.before_commit
    def expired(path, db):
        raise HubError('hub_grant_required', 403)
    monkeypatch.setattr(session_context, 'before_commit', expired)
    with pytest.raises(HubError, match='hub_grant_required'):
        call(env, token, 'hub_tracking_draft_propose', lambda actor: env.repo.propose_tracking_draft(
            actor, PROJECT, 'Denied', 'denied-key', secrets.token_urlsafe(32)))
    monkeypatch.setattr(session_context, 'before_commit', before)
    with env.repo._connection() as db:
        assert not db.execute("SELECT 1 FROM hub_entities WHERE kind IN ('tracking_task','tracking_intake_receipt')").fetchone()
        assert not db.execute("SELECT 1 FROM hub_requests WHERE key='denied-key'").fetchone()
        assert not db.execute("SELECT 1 FROM hub_audit WHERE action='tracking_task.proposed'").fetchone()

