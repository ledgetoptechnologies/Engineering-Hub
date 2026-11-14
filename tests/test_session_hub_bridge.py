"""Synthetic source-only authorization, commit ordering and recovery acceptance."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from agent_message_bus.agent_sessions import AgentSessionRepository, AgentSessionError
from agent_message_bus.knowledge import KnowledgeRepository
from agent_message_bus.auth import AuthSettings
from engineering_hub.components import ProjectComponentRepository
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubRepository, HubError
from engineering_hub.session_hub_bridge import SessionHubBridge, CAPABILITIES, TOOLS
from engineering_hub import planning


OWNER = 'human:42'
PROJECT = 'engineering-hub'


class Clock:
    value = 1700000000.0
    def __call__(self):
        return self.value


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    sessions = AgentSessionRepository(tmp_path / 'sessions.sqlite3', OWNER, clock)
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    projects = ProjectRepository(repo)
    components = ProjectComponentRepository(repo)
    knowledge = KnowledgeRepository(tmp_path / 'knowledge.sqlite3')
    knowledge.configure_write_admission(projects.knowledge_write_admission)
    bridge = SessionHubBridge(sessions, repo, projects, components, knowledge)
    yield SimpleNamespace(clock=clock, sessions=sessions, repo=repo, projects=projects,
        components=components, knowledge=knowledge, bridge=bridge)
    bridge.close()


def pair(env, name='pc', family='codex', caps=CAPABILITIES, projects=(PROJECT,)):
    secret = 'ehi_' + secrets.token_urlsafe(32)
    item = env.sessions.request_enrollment(family, name, 'synthetic', hashlib.sha256(secret.encode()).hexdigest())
    env.sessions.approve(OWNER, item['id'], 1)
    issued = env.sessions.exchange(secret)
    if caps:
        env.bridge.grant_put(OWNER, item['id'], 0, list(projects), list(caps))
    return item['id'], issued['token'], secret


def call(env, token, tool, action):
    with env.bridge.admit(token, tool) as op:
        return action(op.principal.actor)


def proposal(env, token, key='key'):
    return call(env, token, 'hub_staging_task_propose', lambda actor:
        env.repo.propose_staging_task(actor, PROJECT, 'Synthetic task', key))


def test_persistent_installation_does_not_extend_sessions_or_hub_grants(env):
    identity, _, secret = pair(env, caps=())
    grant = env.bridge.grant_put(OWNER, identity, 0, [PROJECT], ['project.read'], lifetime_seconds=60)
    converted = env.sessions.keep_approved(OWNER, identity, 2)
    assert converted['expires_at'] is None
    assert env.bridge.grant_get(OWNER, identity)['expires_at'] == grant['expires_at']
    issued = env.sessions.exchange(secret)
    call(env, issued['token'], 'hub_project_get', lambda _: None)
    env.clock.value += 60
    with pytest.raises(HubError, match='hub_grant_required'):
        call(env, issued['token'], 'hub_project_get', lambda _: None)
    env.clock.value += 8 * 86400
    fresh = env.sessions.exchange(secret)
    with pytest.raises(HubError, match='hub_grant_required'):
        call(env, fresh['token'], 'hub_project_get', lambda _: None)
    renewed = env.bridge.grant_put(OWNER, identity, 1, [PROJECT], ['project.read'], lifetime_seconds=60)
    assert renewed['state'] == 'active'
    principal = env.sessions.authenticate_session(fresh['token'])
    assert principal.expires_at == env.clock.value + 900
    call(env, fresh['token'], 'hub_project_get', lambda _: None)


def test_explicit_grants_cas_scope_and_no_implicit_capabilities(env):
    installation, token, _ = pair(env, caps=())
    assert env.bridge.grant_get(OWNER, installation)['state'] == 'absent'
    with pytest.raises(HubError, match='hub_grant_required'):
        proposal(env, token)
    grant = env.bridge.grant_put(OWNER, installation, 0, [PROJECT], ['task.propose'])
    assert grant['revision'] == 1 and grant['state'] == 'active'
    with pytest.raises(HubError, match='grant_revision_conflict'):
        env.bridge.grant_put(OWNER, installation, 0, [PROJECT], ['task.propose'])
    with pytest.raises(HubError, match='hub_capability_forbidden'):
        call(env, token, 'hub_project_get', lambda _: None)
    proposal(env, token)
    with pytest.raises(HubError, match='hub_project_forbidden'):
        call(env, token, 'hub_staging_task_propose', lambda a:
            env.repo.propose_staging_task(a, 'project-alpha', 'Other', 'other'))
    revoked = env.bridge.grant_revoke(OWNER, installation, 1)
    assert revoked['state'] == 'revoked' and revoked['capabilities'] == ['task.propose']
    with pytest.raises(HubError, match='hub_grant_required'):
        proposal(env, token)
    env.sessions.revoke_installation(OWNER, installation, 2)
    assert env.bridge.grant_get(OWNER, installation)['state'] == 'inactive'


def test_tracking_and_project_enumeration_require_distinct_grants(env):
    installation, token, _ = pair(env, caps=())
    env.bridge.grant_put(OWNER, installation, 0, [PROJECT], ['task.read'])
    for tool in ('hub_project_tasks', 'hub_tracking_task_list', 'hub_tracking_task_propose', 'hub_tracking_task_update', 'hub_tracking_task_review'):
        with pytest.raises(HubError, match='hub_capability_forbidden'):
            call(env, token, tool, lambda _: None)
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], ['tracking.propose', 'tracking.read', 'tracking.update', 'task.list'])
    item = call(env, token, 'hub_tracking_task_propose', lambda actor:
        env.repo.propose_tracking_task(actor, PROJECT, 'Inert test', 'tracking-key'))
    with pytest.raises(HubError, match='hub_project_forbidden'):
        call(env, token, 'hub_tracking_task_list', lambda _: env.repo.tracking_tasks('project-alpha', 100, None))
    with pytest.raises(HubError, match='hub_project_forbidden'):
        call(env, token, 'hub_tracking_task_list', lambda _: env.repo.tracking_tasks('project-alpha', 100, None, True))
    assert call(env, token, 'hub_tracking_task_list', lambda _: env.repo.tracking_tasks(PROJECT, 100, None))[0]['id'] == item['id']
    assert call(env, token, 'hub_tracking_task_list', lambda _: env.repo.tracking_tasks(PROJECT, 100, None, True))[0]['id'] == item['id']
    env.bridge.grant_revoke(OWNER, installation, 2)
    with pytest.raises(HubError, match='hub_grant_required'):
        call(env, token, 'hub_tracking_task_list', lambda _: None)


def test_tracking_review_requires_independent_agent_family(env):
    _, author, _ = pair(env, 'tracking-author', family='codex')
    _, sibling, _ = pair(env, 'tracking-sibling', family='codex')
    _, reviewer, _ = pair(env, 'tracking-reviewer', family='claude')
    item = call(env, author, 'hub_tracking_task_propose', lambda actor:
        env.repo.propose_tracking_task(actor, PROJECT, 'Test claim', 'new-tracking'))
    links = [{'label': 'Evidence', 'url': 'https://example.com/evidence'}]
    item = call(env, author, 'hub_tracking_task_update', lambda actor:
        env.repo.update_tracking_task(actor, item['id'], 1, 'claim-tracking', progress='Done',
                                      evidence_links=links, completion_claim=True))
    with pytest.raises(HubError, match='independent_review_required'):
        call(env, sibling, 'hub_tracking_task_review', lambda actor:
            env.repo.review_tracking_task(actor, item['id'], 2, 'same-family', verdict='verify', note='Read', evidence_links=links))
    verified = call(env, reviewer, 'hub_tracking_task_review', lambda actor:
        env.repo.review_tracking_task(actor, item['id'], 2, 'other-family', verdict='verify', note='Read', evidence_links=links))
    assert verified['status'] == 'completion_verified'


@pytest.mark.parametrize('projects,caps', [([], ['task.propose']), ([PROJECT], []), (['*'], ['task.propose']),
    ([PROJECT], ['*']), ([PROJECT, PROJECT], ['task.propose']), ([PROJECT], ['task.propose', 'task.propose'])])
def test_reject_wildcard_empty_duplicate_grants(env, projects, caps):
    installation, _, _ = pair(env, caps=())
    with pytest.raises(HubError):
        env.bridge.grant_put(OWNER, installation, 0, projects, caps)


def test_separate_pc_idempotency_exact_session_claim_and_family_review(env):
    _, a, asecret = pair(env, 'a')
    _, b, _ = pair(env, 'b')
    first, second = proposal(env, a), proposal(env, b)
    assert first['id'] != second['id'] and first['proposed_by'] != second['proposed_by']
    assert proposal(env, a)['id'] == first['id']
    task = call(env, a, 'hub_task_claim', lambda actor: env.repo.claim_task(actor, first['id'], 1))
    sibling = env.sessions.exchange(asecret)['token']
    with pytest.raises(HubError, match='forbidden'):
        call(env, sibling, 'hub_task_candidate', lambda actor: env.repo.set_candidate(actor, task['id'], 2, 'a' * 40))
    task = call(env, a, 'hub_task_candidate', lambda actor: env.repo.set_candidate(actor, task['id'], 2, 'a' * 40))
    for reviewer in (b, sibling):
        with pytest.raises(HubError, match='independent_review_required'):
            call(env, reviewer, 'hub_task_review', lambda actor: env.repo.review_task(actor, task['id'], 3, 'a' * 40, 'approve'))
    with pytest.raises(HubError, match='independent_review_required'):
        env.repo.review_task('codex', task['id'], 3, 'a' * 40, 'approve')
    _, reviewer, _ = pair(env, 'reviewer', 'claude')
    reviewed = call(env, reviewer, 'hub_task_review', lambda actor: env.repo.review_task(actor, task['id'], 3, 'a' * 40, 'approve'))
    assert reviewed['status'] == 'verified'
    with env.repo._connection() as db:
        rows = db.execute('SELECT * FROM hub_session_provenance ORDER BY audit_sequence').fetchall()
    assert len(rows) == 5 and all(row['session_id'] for row in rows)
    with pytest.raises(HubError, match='forbidden'):
        env.repo.claim_task(first['proposed_by'], second['id'], 1)


def test_component_scope_cas_references_and_own_withdrawal(env):
    _, token, _ = pair(env)
    item = call(env, token, 'hub_component_create', lambda a: env.components.create_component(a, PROJECT, 'pkg', 'Package', 'pkg'))
    with pytest.raises(HubError, match='hub_project_forbidden'):
        call(env, token, 'hub_component_update', lambda a: env.components.update_component(a, item['id'], 1, legacy_project_id='project-alpha'))
    item = call(env, token, 'hub_component_retire', lambda a: env.components.retire_component(a, item['id'], 1))
    assert item['status'] == 'retired'
    with pytest.raises(HubError, match='revision_conflict'):
        call(env, token, 'hub_component_retire', lambda a: env.components.retire_component(a, item['id'], 1))
    own = proposal(env, token)
    other = env.repo.create_task(OWNER, PROJECT, 'Human task', 'human')
    with pytest.raises(HubError, match='forbidden'):
        call(env, token, 'hub_entity_withdraw', lambda a: env.repo.withdraw(a, 'task', other['id'], 1, 'not mine'))
    assert call(env, token, 'hub_entity_withdraw', lambda a: env.repo.withdraw(a, 'task', own['id'], 1, 'done'))['status'] == 'withdrawn'


@pytest.mark.parametrize('store', ['hub', 'knowledge'])
def test_expiry_at_actual_store_commit_rolls_back(env, monkeypatch, store):
    _, token, _ = pair(env)
    original = env.bridge._live
    counter = 0
    def expiring(*args, **kwargs):
        nonlocal counter
        counter += 1
        if counter == 2:
            env.clock.value += 901
        return original(*args, **kwargs)
    monkeypatch.setattr(env.bridge, '_live', expiring)
    with pytest.raises(AgentSessionError, match='invalid_session'):
        if store == 'hub':
            proposal(env, token)
        else:
            call(env, token, 'hub_planning_put', lambda a: planning.put_item(env.knowledge, author=a,
                scope='topic', project=PROJECT, topic='synthetic', kind='plan', item_id='item',
                title='Plan', content='Synthetic', status='proposed', expected_version=0))
    assert env.repo.list_tasks() == []
    assert env.knowledge.list(project=PROJECT) == []
    with env.knowledge._connection() as db:
        assert db.execute('SELECT count(*) FROM knowledge_session_provenance').fetchone()[0] == 0


def test_revoke_orders_after_committed_mutation_and_denies_replay(env, monkeypatch):
    installation, token, _ = pair(env)
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    original = env.repo._save
    def paused(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(env.repo, '_save', paused)
    def revoke():
        env.bridge.grant_revoke(OWNER, installation, 1)
        revoked.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation = pool.submit(proposal, env, token)
        assert entered.wait(5)
        revocation = pool.submit(revoke)
        assert not revoked.wait(.05)
        release.set()
        task = mutation.result(5); revocation.result(5)
    assert env.repo.get_task(task['id'])['revision'] == 1
    with pytest.raises(HubError, match='hub_grant_required'):
        proposal(env, token)


def test_planning_commit_records_exact_identity_and_legacy_is_compatible(env):
    _, token, _ = pair(env)
    result = call(env, token, 'hub_planning_put', lambda a: planning.put_item(env.knowledge, author=a,
        scope='topic', project=PROJECT, topic='synthetic', kind='plan', item_id='item',
        title='Plan', content='Synthetic', status='proposed', expected_version=0))
    with env.knowledge._connection() as db:
        row = db.execute('SELECT * FROM knowledge_session_provenance').fetchone()
    assert row['project'] == PROJECT and row['path'].endswith('/item.json') and row['version'] == 1
    task = env.repo.create_task(OWNER, PROJECT, 'Legacy', 'legacy')
    task = env.repo.claim_task('codex', task['id'], 1)
    task = env.repo.set_candidate('codex', task['id'], 2, 'b' * 40)
    task = env.repo.review_task('hermes', task['id'], 3, 'b' * 40, 'approve')
    assert task['owner'] == 'codex' and task['status'] == 'verified'


def test_owner_recovery_retains_candidate_review_and_exact_dedup(env):
    _, token, secret = pair(env)
    task = proposal(env, token)
    task = call(env, token, 'hub_task_claim', lambda a: env.repo.claim_task(a, task['id'], 1))
    task = call(env, token, 'hub_task_candidate', lambda a: env.repo.set_candidate(a, task['id'], 2, 'a' * 40))
    env.repo.review_task('claude', task['id'], 3, 'a' * 40, 'approve')
    sibling = env.sessions.exchange(secret)
    target = sibling['session']['id']
    with pytest.raises(HubError, match='claim_session_active'):
        env.bridge.recover_claim(OWNER, task['id'], 4, target, 'Synthetic reason', 'recover')
    old = env.sessions.authenticate_session(token)
    env.sessions.revoke_session(OWNER, old.session_id, 1)
    recovered = env.bridge.recover_claim(OWNER, task['id'], 4, target, 'Synthetic reason', 'recover')
    assert not recovered['replayed'] and recovered['task']['revision'] == 5
    assert recovered['task']['candidate_commit'] == 'a' * 40 and recovered['task']['review'] is None
    assert recovered['recovery']['prior_review']['actor'] == 'claude'
    again = env.bridge.recover_claim(OWNER, task['id'], 4, target, 'Synthetic reason', 'recover')
    assert again['replayed'] and again['recovery'] == recovered['recovery']
    assert len(env.bridge.claim_history(OWNER, task['id'])['recoveries']) == 1
    with pytest.raises(HubError, match='idempotency_conflict'):
        env.bridge.recover_claim(OWNER, task['id'], 4, target, 'Changed', 'recover')
    with pytest.raises(AgentSessionError):
        env.bridge.recover_claim('codex', task['id'], 4, target, 'Changed', 'recover')


def test_owner_claim_recovery_survives_more_than_256_later_sessions(env):
    installation, token, secret = pair(env)
    approved = env.sessions.keep_approved(OWNER, installation, 2)
    task = proposal(env, token)
    task = call(env, token, 'hub_task_claim', lambda actor: env.repo.claim_task(actor, task['id'], 1))
    prior_session = task['owner_session_id']
    env.sessions.revoke_session(OWNER, prior_session, 1)
    env.clock.value += 30 * 86400
    target = None
    for index in range(257):
        if index:
            env.clock.value += 901
        target = env.sessions.exchange(secret)['session']['id']
    assert env.sessions.list_installations(OWNER)['installations'][0]['revision'] == approved['revision']
    assert all(item['id'] != prior_session
               for item in env.sessions.list_sessions(OWNER, installation)['sessions'])
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], ['task.claim'])
    recovered = env.bridge.recover_claim(OWNER, task['id'], 2, target, 'Long-term recovery', 'after-window')
    assert not recovered['replayed'] and recovered['task']['owner_session_id'] == target
    assert recovered['recovery']['prior_session_id'] == prior_session
    with env.sessions._transaction() as db:
        assert db.execute('SELECT count(*) FROM agent_sessions WHERE id=?', (prior_session,)).fetchone()[0] == 1


def test_cross_installation_recovery_and_execution_are_denied(env):
    _, token, secret = pair(env)
    _, other, _ = pair(env, 'other')
    task = proposal(env, token)
    task = call(env, token, 'hub_task_claim', lambda a: env.repo.claim_task(a, task['id'], 1))
    env.sessions.revoke_session(OWNER, env.sessions.authenticate_session(token).session_id, 1)
    with pytest.raises(HubError, match='invalid_recovery_target'):
        env.bridge.recover_claim(OWNER, task['id'], 2, env.sessions.authenticate_session(other).session_id, 'Reason', 'key')
    target = env.sessions.exchange(secret)['session']['id']
    with env.repo._transaction() as db:
        db.execute('CREATE TABLE hub_work_jobs (target TEXT, state TEXT)')
        db.execute('INSERT INTO hub_work_jobs VALUES(?,?)', (json.dumps({'task_id': task['id']}), 'completed'))
    with pytest.raises(HubError, match='task_execution_present'):
        env.bridge.recover_claim(OWNER, task['id'], 2, target, 'Reason', 'key')
    with pytest.raises(HubError, match='forbidden'):
        call(env, other, 'hub_task_claim', lambda a: env.repo.create_run(a, task['id'], 2, 'a' * 64, 'target'))


def test_second_bridge_owner_refused(env):
    with pytest.raises(OSError):
        SessionHubBridge(env.sessions, env.repo, env.projects, env.components, env.knowledge)
    assert env.sessions._hub_bridge_tools == tuple(TOOLS)


@pytest.mark.parametrize('exception', [RuntimeError, asyncio.CancelledError])
def test_context_is_reset_after_error_and_not_shared_with_other_thread(env, exception):
    from agent_message_bus.session_hub_context import current
    _, token, _ = pair(env)
    with pytest.raises(exception):
        with env.bridge.admit(token, 'hub_task_claim') as operation:
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(current.get).result(5) is None
                with pytest.raises(HubError, match='forbidden'):
                    pool.submit(env.repo._actor, operation.principal.actor).result(5)
            raise exception('synthetic')
    assert current.get() is None
    assert env.repo._actor('codex') is None


def test_archived_project_removes_effective_rights_and_blocks_withdraw(env):
    project = 'project-alpha'
    installation, token, _ = pair(env, projects=(project,))
    task = call(env, token, 'hub_staging_task_propose', lambda a: env.repo.propose_staging_task(a, project, 'Task', 'key'))
    env.projects.archive_project(OWNER, project, 1)
    grant = env.bridge.grant_get(OWNER, installation)
    assert grant['state'] == 'active'
    assert grant['effective'] == dict(state='inactive', reason='no_active_projects',
        projects=[], capabilities=[], expires_at=None)
    with pytest.raises(HubError, match='project_archived'):
        call(env, token, 'hub_entity_withdraw', lambda a: env.repo.withdraw(a, 'task', task['id'], 1, 'Reason'))


@pytest.fixture
def unified(tmp_path):
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from mcp.server.transport_security import TransportSecuritySettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(allowed_hosts=['staging.test'], allowed_origins=[origin]))
    app = create_app(Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(origin, beau_github_id=42)), bus_app=bus)
    bus.state.agent_sessions.clock = Clock()
    with TestClient(app, base_url=origin) as client:
        cookie = app.state.sessions.create_session(42)
        client.cookies.set('hub_session', cookie['cookie_token'])
        csrf = {'Origin': origin, 'X-Hub-CSRF': cookie['csrf_token']}
        yield client, app, bus, csrf


def sdk(client, token):
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': '2025-06-18'}
    response = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'synthetic', 'version': '1'}}})
    assert response.status_code == 200
    headers['Mcp-Session-Id'] = response.headers['Mcp-Session-Id']
    client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    def invoke(tool, arguments):
        result = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': tool, 'arguments': arguments}}).json()
        return result['result']
    return invoke, headers


def data(result):
    assert not result.get('isError')
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


def test_tracking_mutation_does_not_disclose_cross_project_or_archived_ids(unified):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, caps=('tracking.propose', 'tracking.update', 'tracking.review'),
                                  projects=(PROJECT, '3d-viewer'))
    invoke, _ = sdk(client, token)
    repo = app.state.repository
    private = repo.propose_tracking_task('codex', 'project-alpha', 'Private', 'private-tracking')
    archived = repo.propose_tracking_task('codex', '3d-viewer', 'Archived', 'archived-tracking')
    env.bridge.projects.archive_project(OWNER, '3d-viewer', 1)
    valid = data(invoke('hub_tracking_task_propose', {'project': PROJECT, 'title': 'Valid',
                                                      'client_request_id': 'valid-tracking'}))
    assert valid == {'id': valid['id'], 'response_scope': 'mutation_receipt', 'current_state_included': False}
    missing = '00000000-0000-4000-8000-000000000000'
    for tool, args in (
        ('hub_tracking_task_update', {'expected_revision': 1, 'client_request_id': 'update-test',
                                      'progress': 'Progress', 'evidence_links': [], 'completion_claim': False}),
        ('hub_tracking_task_review', {'expected_revision': 1, 'client_request_id': 'review-test',
                                      'verdict': 'reopen', 'note': 'Review', 'evidence_links': []}),
    ):
        results = [invoke(tool, {**args, 'task_id': task_id}) for task_id in
                   (private['id'], archived['id'], missing)]
        assert all(result['isError'] for result in results)
        assert len({result['content'][0]['text'] for result in results}) == 1
        assert 'hub_tracking_task_unavailable' in results[0]['content'][0]['text']
    updated = data(invoke('hub_tracking_task_update', {'task_id': valid['id'], 'expected_revision': 1,
        'client_request_id': 'valid-update', 'progress': 'Progress', 'evidence_links': [], 'completion_claim': False}))
    assert updated == valid
    links = [{'label': 'Evidence', 'url': 'https://example.com/evidence'}]
    claim = data(invoke('hub_tracking_task_update', {'task_id': valid['id'], 'expected_revision': 2,
        'client_request_id': 'valid-claim', 'progress': 'Done', 'evidence_links': links, 'completion_claim': True}))
    _, reviewer, _ = pair(env, name='reviewer', family='claude', caps=('tracking.review',))
    review, _ = sdk(client, reviewer)
    verified = data(review('hub_tracking_task_review', {'task_id': valid['id'], 'expected_revision': 3,
        'client_request_id': 'valid-review', 'verdict': 'verify', 'note': 'Checked', 'evidence_links': links}))
    assert claim == valid and verified == valid
    assert repo._read(valid['id'], 'tracking_task')['status'] == 'completion_verified'
    env.bridge.grant_revoke(OWNER, installation, 1)
    assert invoke('hub_tracking_task_update', {'task_id': valid['id'], 'expected_revision': 4,
        'client_request_id': 'revoked', 'progress': 'No', 'evidence_links': [], 'completion_claim': False})['isError']
    assert repo._read(valid['id'], 'tracking_task')['revision'] == 4


def test_tracking_write_only_receipts_do_not_disclose_later_history(unified):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, creator, _ = pair(env, name='tracking-propose-only', caps=('tracking.propose',))
    _, updater, _ = pair(env, name='tracking-update-only', caps=('tracking.update',))
    _, reviewer, _ = pair(env, name='tracking-review-only', family='claude', caps=('tracking.review',))
    create, _ = sdk(client, creator)
    update, _ = sdk(client, updater)
    review, _ = sdk(client, reviewer)
    proposal = {'project': PROJECT, 'title': 'Public task title', 'client_request_id': 'stable-proposal'}
    receipt = data(create('hub_tracking_task_propose', proposal))
    assert receipt == {'id': receipt['id'], 'response_scope': 'mutation_receipt', 'current_state_included': False}
    identity = receipt['id']

    # Another authorized actor adds private evidence after the original write.
    # A retry must not become a read of their current task or review history.
    private = 'private-progress-not-for-write-only'
    repo = app.state.repository
    item = repo.update_tracking_task('claude', identity, 1, 'private-progress',
        progress=private, evidence_links=[])
    assert private in json.dumps(item)
    assert data(create('hub_tracking_task_propose', proposal)) == receipt

    progress = {'task_id': identity, 'expected_revision': item['revision'], 'client_request_id': 'update',
                'progress': 'Another update', 'evidence_links': [], 'completion_claim': False}
    assert data(update('hub_tracking_task_update', progress)) == receipt
    item = repo._read(identity, 'tracking_task')
    compact = {'task_id': identity, 'expected_revision': item['revision'], 'client_request_id': 'compact'}
    assert data(update('hub_tracking_task_compact', compact)) == receipt
    item = repo._read(identity, 'tracking_task')
    links = [{'label': 'Evidence', 'url': 'https://example.com/evidence'}]
    claim = {'task_id': identity, 'expected_revision': item['revision'], 'client_request_id': 'claim',
             'progress': 'Claimed', 'evidence_links': links, 'completion_claim': True}
    assert data(update('hub_tracking_task_update', claim)) == receipt
    item = repo._read(identity, 'tracking_task')
    verified = {'task_id': identity, 'expected_revision': item['revision'], 'client_request_id': 'review',
                'verdict': 'verify', 'note': 'private-review-note', 'evidence_links': links}
    assert data(review('hub_tracking_task_review', verified)) == receipt
    assert data(review('hub_tracking_task_review', verified)) == receipt
    assert data(create('hub_tracking_task_propose', proposal)) == receipt

    # Granting read enables the existing full response; revoking just that
    # capability restores the receipt even for an idempotent retry.
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], ['tracking.propose', 'tracking.read'])
    full = data(create('hub_tracking_task_propose', proposal))
    assert full['status'] == 'completion_verified' and 'private-review-note' in json.dumps(full)
    env.bridge.grant_put(OWNER, installation, 2, [PROJECT], ['tracking.propose'])
    assert data(create('hub_tracking_task_propose', proposal)) == receipt


def test_tracking_read_capability_loss_at_response_returns_only_receipt(unified, monkeypatch):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    _, token, _ = pair(env, name='response-read-expiry', caps=('tracking.propose', 'tracking.read'))
    invoke, _ = sdk(client, token)
    original_live = env.bridge._live
    def expire_read(db, secret, revision=None, capability=None, *, session_id=None):
        if capability == 'tracking.read':
            raise HubError('hub_grant_required', 403)
        return original_live(db, secret, revision, capability, session_id=session_id)
    monkeypatch.setattr(env.bridge, '_live', expire_read)
    result = data(invoke('hub_tracking_task_propose', {'project': PROJECT, 'title': 'Committed safely',
                                                       'client_request_id': 'expiry-after-commit'}))
    assert result == {'id': result['id'], 'response_scope': 'mutation_receipt', 'current_state_included': False}
    assert app.state.repository._read(result['id'], 'tracking_task')['title'] == 'Committed safely'


def test_task_and_component_mutations_without_read_return_only_id(unified):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, name='write-only-tasks',
        caps=('task.propose', 'task.claim', 'task.candidate', 'component.write'))
    _, reviewer, _ = pair(env, name='write-only-task-reviewer', family='claude', caps=('task.review',))
    invoke, _ = sdk(client, token); review, _ = sdk(client, reviewer)
    repo, components = app.state.repository, env.bridge.components
    def receipt(identity):
        return {'id': identity, 'response_scope': 'mutation_receipt', 'current_state_included': False}

    proposal = {'project': PROJECT, 'title': 'Work', 'client_request_id': 'staging-proposal'}
    task = data(invoke('hub_staging_task_propose', proposal)); tid = task['id']
    assert task == receipt(tid)
    assert data(invoke('hub_task_claim', {'task_id': tid, 'expected_revision': 1})) == receipt(tid)
    assert data(invoke('hub_task_candidate', {'task_id': tid, 'expected_revision': 2, 'commit': 'a' * 40})) == receipt(tid)
    assert data(review('hub_task_review', {'task_id': tid, 'expected_revision': 3,
        'commit': 'a' * 40, 'verdict': 'approve'})) == receipt(tid)
    assert data(invoke('hub_staging_task_propose', proposal)) == receipt(tid)
    assert repo.get_task(tid)['status'] == 'verified'

    cp = {'payload': {'project': PROJECT, 'slug': 'bounded-component', 'name': 'Original',
                      'client_request_id': 'component-proposal'}}
    component = data(invoke('hub_component_create', cp)); cid = component['id']
    assert component == receipt(cid)
    components.update_component(OWNER, cid, 1, description='private-metadata')
    assert data(invoke('hub_component_create', cp)) == receipt(cid)
    assert data(invoke('hub_component_update', {'payload': {'component_id': cid,
        'expected_revision': 2, 'name': 'Original'}})) == receipt(cid)
    assert components.get_component(cid)['revision'] == 2  # No-op stays a no-op.
    assert data(invoke('hub_component_update', {'payload': {'component_id': cid,
        'expected_revision': 2, 'name': 'New'}})) == receipt(cid)
    assert data(invoke('hub_component_retire', {'payload': {'component_id': cid,
        'expected_revision': 3}})) == receipt(cid)
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT],
        ['task.propose', 'task.claim', 'task.candidate', 'task.read', 'component.write', 'component.read'])
    assert data(invoke('hub_staging_task_propose', proposal))['status'] == 'verified'
    assert data(invoke('hub_component_create', cp))['description'] == 'private-metadata'
    env.bridge.grant_put(OWNER, installation, 2, [PROJECT],
        ['task.propose', 'task.claim', 'task.candidate', 'component.write'])
    assert data(invoke('hub_staging_task_propose', proposal)) == receipt(tid)
    assert data(invoke('hub_component_create', cp)) == receipt(cid)


def test_withdraw_uses_the_matching_entity_read_grant(unified):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, name='withdraw-write-only',
        caps=('entity.withdraw', 'task.propose', 'decision.request'))
    invoke, _ = sdk(client, token)
    task = data(invoke('hub_staging_task_propose', {'project': PROJECT, 'title': 'Task',
                                                   'client_request_id': 'withdraw-proposal'}))
    decision = data(invoke('hub_decision_request', {'project': PROJECT, 'title': 'Decision',
                                                    'options': ['Yes', 'No']}))
    def receipt(identity):
        return {'id': identity, 'response_scope': 'mutation_receipt', 'current_state_included': False}
    task_request = {'kind': 'task', 'entity_id': task['id'], 'expected_revision': 1, 'reason': 'No longer needed'}
    decision_request = {'kind': 'decision', 'entity_id': decision['id'], 'expected_revision': 1, 'reason': 'No longer needed'}
    assert data(invoke('hub_entity_withdraw', task_request)) == receipt(task['id'])
    assert data(invoke('hub_entity_withdraw', decision_request)) == receipt(decision['id'])
    assert data(invoke('hub_entity_withdraw', task_request)) == receipt(task['id'])
    assert data(invoke('hub_entity_withdraw', decision_request)) == receipt(decision['id'])
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT],
                         ['entity.withdraw', 'task.propose', 'decision.request', 'task.read'])
    assert data(invoke('hub_entity_withdraw', task_request))['status'] == 'withdrawn'
    assert data(invoke('hub_entity_withdraw', decision_request)) == receipt(decision['id'])
    env.bridge.grant_put(OWNER, installation, 2, [PROJECT],
                         ['entity.withdraw', 'task.propose', 'decision.request', 'decision.read'])
    assert data(invoke('hub_entity_withdraw', task_request)) == receipt(task['id'])
    assert data(invoke('hub_entity_withdraw', decision_request))['status'] == 'withdrawn'


def test_role_lease_only_receipts_do_not_disclose_handoff_or_release_reason(unified):
    from engineering_hub.roles import RoleRepository
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    first_id, first, _ = pair(env, name='role-first', caps=('role.lease', 'role.claim', 'tracking.read'))
    second_id, second, _ = pair(env, name='role-second', family='claude',
                                caps=('role.lease', 'role.claim', 'tracking.read'))
    roles = RoleRepository(app.state.repository)
    roles.configure(OWNER, 'bounded-privacy', 0, [PROJECT], [first_id, second_id], ['private/path'],
                    'private-standing-rules', [])
    task = app.state.repository.propose_tracking_task('codex', PROJECT, 'Work', 'role-task')
    a, _ = sdk(client, first); b, _ = sdk(client, second)
    lease = data(a('hub_role_acquire', {'role': 'bounded-privacy', 'expected_revision': 1, 'lease_seconds': 60}))
    assert lease['response_scope'] == 'coordination_receipt' and lease['generation'] == 1
    assert 'private-standing-rules' not in json.dumps(lease) and 'handoff' not in lease
    claim = data(a('hub_role_claim', {'role': 'bounded-privacy', 'generation': 1, 'task_id': task['id'],
                                     'client_request_id': 'first-claim'}))
    assert claim['response_scope'] == 'coordination_receipt'
    released_claim = data(a('hub_role_claim_release', {'role': 'bounded-privacy', 'generation': 1,
        'task_id': task['id'], 'expected_revision': claim['revision'], 'reason': 'private-release-reason'}))
    assert 'private-release-reason' not in json.dumps(released_claim)
    released = data(a('hub_role_release', {'role': 'bounded-privacy', 'expected_revision': lease['revision'],
        'generation': 1, 'expected_handoff_revision': lease['handoff_revision'], 'handoff': 'private-handoff'}))
    acquired = data(b('hub_role_acquire', {'role': 'bounded-privacy', 'expected_revision': released['revision'],
                                          'lease_seconds': 60}))
    assert acquired['takeover'] == 'handoff_recorded' and acquired['handoff_revision'] == 1
    assert 'private-handoff' not in json.dumps(acquired)
    reclaimed = data(b('hub_role_claim', {'role': 'bounded-privacy', 'generation': acquired['generation'],
        'task_id': task['id'], 'client_request_id': 'second-claim'}))
    assert reclaimed['revision'] > claim['revision'] and 'private-release-reason' not in json.dumps(reclaimed)
    renewed = data(b('hub_role_renew', {'role': 'bounded-privacy', 'expected_revision': acquired['revision'],
        'generation': acquired['generation'], 'lease_seconds': 60}))
    assert renewed['lease_expires_at'] and 'private-handoff' not in json.dumps(renewed)
    env.bridge.grant_put(OWNER, second_id, 1, [PROJECT], ['role.lease', 'role.claim', 'role.read', 'tracking.read'])
    full = data(b('hub_role_renew', {'role': 'bounded-privacy', 'expected_revision': renewed['revision'],
        'generation': renewed['generation'], 'lease_seconds': 60}))
    assert full['handoff']['text'] == 'private-handoff'
    env.bridge.grant_put(OWNER, second_id, 2, [PROJECT], ['role.lease', 'role.claim', 'tracking.read'])
    limited = data(b('hub_role_renew', {'role': 'bounded-privacy', 'expected_revision': full['revision'],
        'generation': full['generation'], 'lease_seconds': 60}))
    assert limited['response_scope'] == 'coordination_receipt' and 'private-handoff' not in json.dumps(limited)
    roles.configure(OWNER, 'mixed-scope', 0, [PROJECT, '3d-viewer'], [first_id], [], 'private-mixed', [])
    denied = a('hub_role_acquire', {'role': 'mixed-scope', 'expected_revision': 1, 'lease_seconds': 60})
    assert denied['isError'] and 'private-mixed' not in json.dumps(denied)
    assert roles.owner_get(OWNER, 'mixed-scope')['lease'] is None


def test_tracking_history_and_compaction_obey_live_project_grants(unified):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, caps=('tracking.read', 'tracking.update'), projects=(PROJECT, '3d-viewer'))
    invoke, _ = sdk(client, token)
    repo = app.state.repository
    valid = repo.propose_tracking_task('codex', PROJECT, 'Long work', 'history-valid')
    for i in range(51):
        valid = repo.update_tracking_task('codex', valid['id'], valid['revision'], f'history-{i}',
                                          progress=f'Update {i}', evidence_links=[])
    private = repo.propose_tracking_task('codex', 'project-alpha', 'Private', 'history-private')
    archived = repo.propose_tracking_task('codex', '3d-viewer', 'Archived', 'history-archived')
    env.bridge.projects.archive_project(OWNER, '3d-viewer', 1)
    missing = '00000000-0000-4000-8000-000000000000'
    for tool, args in (
        ('hub_tracking_task_history', {'kind': 'updates'}),
        ('hub_tracking_task_compact', {'expected_revision': 1, 'client_request_id': 'compact-private'}),
    ):
        results = [invoke(tool, {**args, 'task_id': task_id}) for task_id in (private['id'], archived['id'], missing)]
        assert all(result['isError'] for result in results)
        assert len({result['content'][0]['text'] for result in results}) == 1
        assert 'hub_tracking_task_unavailable' in results[0]['content'][0]['text']
    page = data(invoke('hub_tracking_task_history', {'task_id': valid['id'], 'kind': 'updates'}))
    assert page['records'][0]['progress'] == 'Update 0'
    env.bridge.grant_revoke(OWNER, installation, 1)
    assert invoke('hub_tracking_task_history', {'task_id': valid['id'], 'kind': 'updates'})['isError']
    assert invoke('hub_tracking_task_compact', {'task_id': valid['id'], 'expected_revision': valid['revision'],
                  'client_request_id': 'after-revoke'})['isError']


def test_tracking_history_guidance_is_visible_in_session_tool_discovery(unified):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    _, token, _ = pair(env, caps=('tracking.read', 'tracking.update', 'tracking.review'), projects=(PROJECT,))
    _, headers = sdk(client, token)
    result = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 3,
                         'method': 'tools/list', 'params': {}})
    assert result.status_code == 200
    tools = result.json()['result']['tools']
    descriptions = {tool['name']: tool.get('description', '') for tool in tools}
    for name in ('hub_tracking_task_list', 'hub_tracking_task_update', 'hub_tracking_task_review', 'hub_tracking_task_compact'):
        assert all(word in descriptions[name] for word in ('recent windows', 'history_segments', 'hub_tracking_task_history', 'next_before'))
    assert all(word in descriptions['hub_tracking_task_history'] for word in ('task_id', 'kind', 'updates', 'reviews', 'before', 'next_before'))


def test_session_id_tools_do_not_disclose_unavailable_entities(unified):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    _, token, _ = pair(env, projects=(PROJECT, '3d-viewer'))
    invoke, _ = sdk(client, token)
    repo, components = app.state.repository, env.bridge.components
    private_task = repo.create_task(OWNER, 'project-alpha', 'Private task', 'oracle-private-task')
    archived_task = repo.create_task(OWNER, '3d-viewer', 'Archived task', 'oracle-archived-task')
    own_task = repo.create_task(OWNER, PROJECT, 'Own task', 'oracle-own-task')
    private_component = components.create_component(OWNER, 'project-alpha', 'private', 'Private', 'oracle-private-comp')
    archived_component = components.create_component(OWNER, '3d-viewer', 'archived', 'Archived', 'oracle-archived-comp')
    own_component = components.create_component(OWNER, PROJECT, 'own', 'Own', 'oracle-own-comp')
    private_decision = repo.create_decision(OWNER, 'project-alpha', 'Private', ['A', 'B'])
    archived_decision = repo.create_decision(OWNER, '3d-viewer', 'Archived', ['A', 'B'])
    own_decision = data(invoke('hub_decision_request', {'project': PROJECT, 'title': 'Own',
        'options': ['A', 'B'], 'client_request_id': 'oracle-own-decision'}))
    env.bridge.projects.archive_project(OWNER, '3d-viewer', 1)
    missing = '00000000-0000-4000-8000-000000000000'
    cases = [
        ('hub_task_get', lambda i: {'task_id': i}, private_task['id'], archived_task['id'], 'hub_task_unavailable'),
        ('hub_task_claim', lambda i: {'task_id': i, 'expected_revision': 1}, private_task['id'], archived_task['id'], 'hub_task_unavailable'),
        ('hub_task_candidate', lambda i: {'task_id': i, 'expected_revision': 1, 'commit': 'a' * 40}, private_task['id'], archived_task['id'], 'hub_task_unavailable'),
        ('hub_task_review', lambda i: {'task_id': i, 'expected_revision': 1, 'commit': 'a' * 40, 'verdict': 'approve'}, private_task['id'], archived_task['id'], 'hub_task_unavailable'),
        ('hub_component_get', lambda i: {'component_id': i}, private_component['id'], archived_component['id'], 'hub_component_unavailable'),
        ('hub_component_update', lambda i: {'payload': {'component_id': i, 'expected_revision': 1, 'name': 'New'}}, private_component['id'], archived_component['id'], 'hub_component_unavailable'),
        ('hub_component_retire', lambda i: {'payload': {'component_id': i, 'expected_revision': 1}}, private_component['id'], archived_component['id'], 'hub_component_unavailable'),
        ('hub_entity_withdraw', lambda i: {'kind': 'task', 'entity_id': i, 'expected_revision': 1, 'reason': 'Done'}, private_task['id'], archived_task['id'], 'hub_entity_unavailable'),
        ('hub_entity_withdraw', lambda i: {'kind': 'decision', 'entity_id': i, 'expected_revision': 1, 'reason': 'Done'}, private_decision['id'], archived_decision['id'], 'hub_entity_unavailable'),
    ]
    for tool, args, private, archived, error in cases:
        results = [invoke(tool, args(i)) for i in (private, archived, missing)]
        assert all(result['isError'] for result in results), tool
        assert len({result['content'][0]['text'] for result in results}) == 1, tool
        assert error in results[0]['content'][0]['text']
    assert data(invoke('hub_task_get', {'task_id': own_task['id']}))['id'] == own_task['id']
    claimed = data(invoke('hub_task_claim', {'task_id': own_task['id'], 'expected_revision': 1}))
    candidate = data(invoke('hub_task_candidate', {'task_id': own_task['id'], 'expected_revision': claimed['revision'], 'commit': 'a' * 40}))
    assert candidate['candidate_commit'] == 'a' * 40
    _, reviewer, _ = pair(env, name='task-reviewer', family='claude', caps=('task.review',))
    review, _ = sdk(client, reviewer)
    assert data(review('hub_task_review', {'task_id': own_task['id'], 'expected_revision': candidate['revision'],
        'commit': 'a' * 40, 'verdict': 'approve'})) == {
            'id': own_task['id'], 'response_scope': 'mutation_receipt', 'current_state_included': False}
    assert repo.get_task(own_task['id'])['review']['verdict'] == 'approve'
    assert data(invoke('hub_component_get', {'component_id': own_component['id']}))['id'] == own_component['id']
    edited = data(invoke('hub_component_update', {'payload': {'component_id': own_component['id'], 'expected_revision': 1, 'name': 'New'}}))
    assert data(invoke('hub_component_retire', {'payload': {'component_id': own_component['id'], 'expected_revision': edited['revision']}}))['status'] == 'retired'
    assert data(invoke('hub_entity_withdraw', {'kind': 'decision', 'entity_id': own_decision['id'], 'expected_revision': 1, 'reason': 'Done'}))['status'] == 'withdrawn'
    own_withdraw = data(invoke('hub_staging_task_propose', {'project': PROJECT, 'title': 'Withdraw',
        'client_request_id': 'oracle-own-withdraw'}))
    assert data(invoke('hub_entity_withdraw', {'kind': 'task', 'entity_id': own_withdraw['id'],
        'expected_revision': 1, 'reason': 'Done'}))['status'] == 'withdrawn'
    bad = invoke('hub_component_get', {'component_id': 'not-a-uuid'})
    assert bad['isError'] and 'invalid_component_id' in bad['content'][0]['text']
    bad = invoke('hub_entity_withdraw', {'kind': 'invalid', 'entity_id': missing, 'expected_revision': 1, 'reason': 'Done'})
    assert bad['isError'] and 'invalid_kind' in bad['content'][0]['text']


@pytest.mark.parametrize('kind', ['task', 'decision'])
def test_withdraw_replacement_does_not_disclose_foreign_status(unified, kind):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, projects=(PROJECT, '3d-viewer'))
    invoke, _ = sdk(client, token)
    repo = app.state.repository
    def create(project, label):
        if kind == 'task':
            return repo.create_task(OWNER, project, label, 'replace-' + label)
        return repo.create_decision(OWNER, project, label, ['A', 'B'])
    private = create('project-alpha', 'private')
    private_withdrawn = create('project-alpha', 'private-withdrawn')
    repo.withdraw(OWNER, kind, private_withdrawn['id'], 1, 'old')
    archived = create('3d-viewer', 'archived')
    own_replacement = create(PROJECT, 'own-replacement')
    own_withdrawn = create(PROJECT, 'own-withdrawn')
    repo.withdraw(OWNER, kind, own_withdrawn['id'], 1, 'old')
    if kind == 'task':
        own = data(invoke('hub_staging_task_propose', {'project': PROJECT, 'title': 'Own',
            'client_request_id': 'withdraw-source'}))
    else:
        own = data(invoke('hub_decision_request', {'project': PROJECT, 'title': 'Own', 'options': ['A', 'B'],
            'client_request_id': 'withdraw-source'}))
    env.bridge.projects.archive_project(OWNER, '3d-viewer', 1)
    missing = '00000000-0000-4000-8000-000000000000'
    args = {'kind': kind, 'entity_id': own['id'], 'expected_revision': 1, 'reason': 'Replaced'}
    errors = [invoke('hub_entity_withdraw', {**args, 'superseded_by': identity}) for identity in
              (private['id'], private_withdrawn['id'], archived['id'], missing)]
    assert all(result['isError'] for result in errors)
    assert len({result['content'][0]['text'] for result in errors}) == 1
    assert 'hub_replacement_unavailable' in errors[0]['content'][0]['text']
    assert repo._read(own['id'], kind)['status'] != 'withdrawn'
    invalid = invoke('hub_entity_withdraw', {**args, 'superseded_by': own_withdrawn['id']})
    assert invalid['isError'] and 'invalid_replacement' in invalid['content'][0]['text']
    invalid = invoke('hub_entity_withdraw', {**args, 'superseded_by': 'invalid'})
    assert invalid['isError'] and 'invalid_replacement' in invalid['content'][0]['text']
    valid = {**args, 'superseded_by': own_replacement['id']}
    result = data(invoke('hub_entity_withdraw', valid))
    assert result['status'] == 'withdrawn' and result['superseded_by'] == own_replacement['id']
    assert data(invoke('hub_entity_withdraw', valid)) == result


def test_withdraw_retry_rechecks_replacement_grant(unified):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, projects=(PROJECT, '3d-viewer'))
    invoke, _ = sdk(client, token)
    original = data(invoke('hub_staging_task_propose', {'project': PROJECT, 'title': 'Original',
        'client_request_id': 'retry-source'}))
    replacement = app.state.repository.create_task(OWNER, '3d-viewer', 'Replacement', 'retry-replacement')
    args = {'kind': 'task', 'entity_id': original['id'], 'expected_revision': 1,
            'reason': 'Replaced', 'superseded_by': replacement['id']}
    result = data(invoke('hub_entity_withdraw', args))
    assert data(invoke('hub_entity_withdraw', args)) == result
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], list(CAPABILITIES))
    denied = invoke('hub_entity_withdraw', args)
    assert denied['isError'] and 'hub_replacement_unavailable' in denied['content'][0]['text']
    assert app.state.repository._read(original['id'], 'task')['revision'] == result['revision']


@pytest.mark.parametrize('expiry', ['grant', 'session'])
def test_project_list_rechecks_expiry_after_enumeration(unified, monkeypatch, expiry):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, caps=('project.read',))
    if expiry == 'grant':
        env.bridge.grant_put(OWNER, installation, 1, [PROJECT], ['project.read'], lifetime_seconds=60)
    invoke, _ = sdk(client, token)
    assert [item['id'] for item in data(invoke('hub_project_list', {}))['items']] == [PROJECT]
    original = env.bridge.projects.get_project
    def expire_during_enumeration(project):
        result = original(project)
        bus.state.agent_sessions.clock.value += 61 if expiry == 'grant' else 901
        return result
    monkeypatch.setattr(env.bridge.projects, 'get_project', expire_during_enumeration)
    denied = invoke('hub_project_list', {})
    assert denied['isError']
    assert not denied.get('structuredContent')


def test_project_list_serializes_with_grant_revocation(unified, monkeypatch):
    client, app, bus, _ = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, caps=('project.read',))
    invoke, _ = sdk(client, token)
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    original = env.bridge.projects.get_project
    def block_during_enumeration(project):
        result = original(project)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(env.bridge.projects, 'get_project', block_during_enumeration)
    def revoke():
        try:
            return env.bridge.grant_revoke(OWNER, installation, 1)
        finally:
            revoked.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(invoke, 'hub_project_list', {})
        try:
            assert entered.wait(5)
            revoke_future = pool.submit(revoke)
            assert not revoked.wait(.05)
        finally:
            release.set()
        assert [item['id'] for item in data(read_future.result(timeout=5))['items']] == [PROJECT]
        revoke_future.result(timeout=5)
    assert invoke('hub_project_list', {})['isError']


def test_session_decision_read_cross_surface_and_project_grants(unified):
    client, app, bus, csrf = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, _ = pair(env, caps=('decision.request', 'decision.read'))
    _, no_read, _ = pair(env, name='no-read', caps=('decision.request',))
    invoke, _ = sdk(client, token); denied, _ = sdk(client, no_read)
    request = {'project': PROJECT, 'title': 'Shared question', 'options': ['A', 'B'],
               'client_request_id': 'shared-question-1'}
    created = data(invoke('hub_decision_request', request))
    assert data(invoke('hub_decision_request', request))['id'] == created['id']
    assert invoke('hub_decision_request', {**request, 'title': 'Changed'})['isError']
    overview = client.get('/api/hub/v1/overview').json()
    assert next(row for row in overview['decisions'] if row['id']==created['id'])['status']=='open'
    response = client.post('/api/hub/v1/decisions/'+created['id']+'/answer', headers=csrf,
                           json={'expected_revision':1,'answer':'B'})
    assert response.status_code==200
    answer = data(invoke('hub_decision_get', {'decision_id':created['id']}))
    assert answer['status']=='answered' and answer['answer']=='B' and answer['execution_authorization'] is False
    request_only = {**request, 'client_request_id': 'request-only'}
    created_without_read = data(denied('hub_decision_request', request_only))
    response = client.post('/api/hub/v1/decisions/'+created_without_read['id']+'/answer', headers=csrf,
                           json={'expected_revision':1,'answer':'B'})
    assert response.status_code == 200
    assert data(denied('hub_decision_request', request_only)) == created_without_read
    assert denied('hub_decision_get', {'decision_id':created['id']})['isError']
    other = app.state.repository.create_decision(OWNER, 'project-alpha', 'Private scope', ['A','B'])
    cross=invoke('hub_decision_get', {'decision_id':other['id']})
    missing=invoke('hub_decision_get', {'decision_id':'00000000-0000-4000-8000-000000000000'})
    assert cross['isError'] and missing['isError'] and cross['content'][0]['text']==missing['content'][0]['text']
    app.state.session_hub_bridge.grant_revoke(OWNER, installation, 1)
    assert invoke('hub_decision_get', {'decision_id':created['id']})['isError']


def test_decision_read_denies_expired_grant_and_archived_project(env):
    installation, token, _ = pair(env, caps=('decision.read',), projects=('project-alpha',))
    item=env.repo.create_decision(OWNER, 'project-alpha', 'Question', ['A','B'])
    def scoped():
        with env.bridge.admit(token, 'hub_decision_get') as op:
            env.bridge._scope('hub_decision_get', {'decision_id':item['id']}, op)
            result=env.repo.get_decision(item['id'])
            op.before_commit(env.repo.path, None)
            return result
    assert scoped()['id']==item['id']
    env.projects.archive_project(OWNER, 'project-alpha', 1)
    with pytest.raises(HubError, match='hub_decision_unavailable'):
        scoped()
    env.clock.value += 86401
    with pytest.raises((HubError, AgentSessionError)):
        scoped()


def test_decision_read_holds_session_writer_until_result_is_authorized(env):
    installation, token, _ = pair(env, caps=('decision.read',))
    item=env.repo.create_decision(OWNER, PROJECT, 'Question', ['A','B'])
    entered=threading.Event(); release=threading.Event(); revoked=threading.Event()
    def read():
        with env.bridge.admit(token, 'hub_decision_get') as op:
            env.bridge._scope('hub_decision_get', {'decision_id':item['id']}, op)
            value=env.repo.get_decision(item['id'])
            entered.set(); assert release.wait(5)
            op.before_commit(env.repo.path, None)
            return value
    def revoke():
        env.bridge.grant_revoke(OWNER, installation, 1)
        revoked.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future=pool.submit(read)
        try:
            assert entered.wait(5)
            revoke_future=pool.submit(revoke)
            assert not revoked.wait(.05)
        finally:
            release.set()
        assert read_future.result(timeout=5)['id']==item['id']
        revoke_future.result(timeout=5)
    with pytest.raises(HubError, match='hub_grant_required'):
        with env.bridge.admit(token, 'hub_decision_get'):
            pass


@pytest.mark.parametrize('kind', ['session', 'grant'])
def test_decision_read_expiry_after_fetch_denies_result(env, kind):
    installation, token, _ = pair(env, caps=('decision.read',))
    if kind == 'grant':
        env.bridge.grant_put(OWNER, installation, 1, [PROJECT], ['decision.read'], 60)
    item = env.repo.create_decision(OWNER, PROJECT, 'Question', ['A', 'B'])
    returned = []
    with pytest.raises(AgentSessionError if kind == 'session' else HubError):
        with env.bridge.admit(token, 'hub_decision_get') as op:
            env.bridge._scope('hub_decision_get', {'decision_id': item['id']}, op)
            value = env.repo.get_decision(item['id'])
            env.clock.value += 901 if kind == 'session' else 61
            op.before_commit(env.repo.path, None)
            returned.append(value)
    assert returned == []


def test_decision_read_serializes_session_revocation(env):
    _, token, _ = pair(env, caps=('decision.read',))
    session_id = env.sessions.authenticate_session(token).session_id
    item = env.repo.create_decision(OWNER, PROJECT, 'Question', ['A', 'B'])
    entered, release, attempting, revoked = (threading.Event() for _ in range(4))
    def read():
        with env.bridge.admit(token, 'hub_decision_get') as op:
            env.bridge._scope('hub_decision_get', {'decision_id': item['id']}, op)
            value = env.repo.get_decision(item['id'])
            entered.set(); assert release.wait(5)
            op.before_commit(env.repo.path, None)
            return value
    def revoke():
        attempting.set()
        env.sessions.revoke_session(OWNER, session_id, 1)
        revoked.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(read)
        try:
            assert entered.wait(5)
            revoke_future = pool.submit(revoke)
            assert attempting.wait(5)
            assert not revoked.wait(.05)
        finally:
            release.set()
        assert read_future.result(timeout=5)['id'] == item['id']
        revoke_future.result(timeout=5)
    with pytest.raises(AgentSessionError):
        with env.bridge.admit(token, 'hub_decision_get'):
            pass


def test_real_owner_api_and_existing_mcp_tools_unified(unified):
    client, app, bus, csrf = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, secret = pair(env, caps=())
    prefix = '/api/hub/v1/agent-connections'
    path = prefix + '/installations/' + installation + '/hub-grant'
    assert client.get(prefix + '/capabilities').json() == {'enabled': True, 'environment': 'staging', 'owner': True}
    assert client.get(prefix + '/hub-capabilities').json() == dict(enabled=True, capabilities=list(CAPABILITIES), features=dict(task_recovery=True))
    absent = client.get(path).json()
    assert absent['state'] == 'absent'
    payload = dict(expected_revision=0, projects=[PROJECT], capabilities=list(CAPABILITIES), lifetime_seconds=86400)
    assert client.put(path, json=payload).status_code == 403
    assert client.put(path, headers={**csrf, 'Origin': 'https://foreign.test'}, json=payload).status_code == 403
    assert client.put(path, headers={'Authorization': 'Bearer synthetic-codex', **csrf}, json=payload).status_code == 403
    assert client.put(path, headers=csrf, json=payload).status_code == 200
    assert client.put(path, headers=csrf, json=payload).status_code == 409
    invoke, headers = sdk(client, token)
    projects = data(invoke('hub_project_list', {}))
    assert [item['id'] for item in projects['items']] == [PROJECT]
    proposed = data(invoke('hub_staging_task_propose', {'project': PROJECT, 'title': 'Transport task', 'client_request_id': 'transport'}))
    claimed = data(invoke('hub_task_claim', {'task_id': proposed['id'], 'expected_revision': 1}))
    assert claimed['owner'].startswith('installation:') and claimed['owner_session_id']
    assert data(invoke('hub_project_tasks', {'project': PROJECT}))['tasks'][0]['id'] == claimed['id']
    assert invoke('hub_project_tasks', {'project': 'project-alpha'})['isError']
    created = data(invoke('hub_component_create', {'payload': {'project': PROJECT, 'slug': 'transport', 'name': 'Transport', 'client_request_id': 'component'}}))
    assert created['created_by'] == claimed['owner']
    planned = data(invoke('hub_planning_put', {'payload': {'scope': 'topic', 'project': PROJECT,
        'topic': 'synthetic', 'kind': 'plan', 'item_id': 'item', 'title': 'Plan', 'content': 'Synthetic', 'expected_version': 0}}))
    assert planned['updated_by'] == claimed['owner']
    assert invoke('hub_project_get', {'project_id': 'project-alpha'})['isError']
    assert invoke('hub_overview', {})['isError']
    assert invoke('hub_run_list', {})['isError']
    assert invoke('knowledge_get', {'project': PROJECT, 'path': 'anything'})['isError']
    assert invoke('receive_messages', {})['isError']
    resources = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'resources/list', 'params': {}}).json()
    assert resources['error']['message'] == 'session_method_not_available'
    # Strict public DTO accepts installation attribution; the owner UI must do so too.
    overview = client.get('/api/hub/v1/overview')
    assert overview.status_code == 200 and overview.json()['tasks'][0]['owner'] == claimed['owner']
    claims = client.get(prefix + '/installations/' + installation + '/task-claims').json()
    assert claims['items'][0]['claim_state'] == 'active'
    assert claims['items'][0]['recovery']['reason'] == 'claim_session_active'
    assert client.get(path, headers={'Authorization': 'Bearer ' + token}).status_code == 403
    assert client.post('/api/hub/v1/projects', headers={'Authorization': 'Bearer ' + token},
        json={'project_id': 'forbidden', 'name': 'Forbidden', 'client_request_id': 'forbidden'}).status_code == 403
    wire = json.dumps([client.get(path).json(), claims, overview.json()])
    assert token not in wire
    requested_decision = data(invoke('hub_decision_request', {'project': PROJECT, 'title': 'Synthetic decision', 'options': ['A', 'B']}))
    withdrawn_decision = data(invoke('hub_entity_withdraw', {'kind': 'decision', 'entity_id': requested_decision['id'], 'expected_revision': 1, 'reason': 'Synthetic withdrawal'}))
    active_grant = client.get(path).json()
    candidate = data(invoke('hub_task_candidate', {'task_id': claimed['id'], 'expected_revision': 2, 'commit': 'a' * 40}))
    _, review_token, _ = pair(env, 'review-pc', family='claude', caps=('task.review',))
    review_invoke, _ = sdk(client, review_token)
    installation_review = data(review_invoke('hub_task_review', {'task_id': claimed['id'],
        'expected_revision': 3, 'commit': 'a' * 40, 'verdict': 'approve'}))
    assert installation_review == {'id': claimed['id'], 'response_scope': 'mutation_receipt',
                                   'current_state_included': False}
    actual_review = app.state.repository.get_task(claimed['id'])['review']
    assert actual_review['family'] == 'claude'
    assert actual_review['actor'].startswith('installation:')
    sibling = env.sessions.exchange(secret)
    target = sibling['session']['id']
    recovery_path = prefix + '/tasks/' + claimed['id'] + '/recover-claim'
    recovery_body = dict(expected_revision=4, target_session_id=target, reason='Synthetic owner recovery', client_request_id='recover-wire')
    assert client.post(recovery_path, json=recovery_body).status_code == 403
    assert client.post(recovery_path, headers={**csrf, 'Authorization': 'Bearer synthetic-codex'}, json=recovery_body).status_code == 403
    assert client.post(recovery_path, headers=csrf, json=recovery_body).status_code == 409
    assert client.post(prefix + '/sessions/' + claimed['owner_session_id'] + '/revoke', headers=csrf, json={'expected_revision': 1}).status_code == 200
    ready_claims = client.get(prefix + '/installations/' + installation + '/task-claims').json()
    assert ready_claims['items'][0]['recovery']['state'] == 'recoverable'
    recovered = client.post(recovery_path, headers=csrf, json=recovery_body)
    assert recovered.status_code == 200
    replay = client.post(recovery_path, headers=csrf, json=recovery_body).json()
    assert replay['replayed'] and replay['task']['revision'] == 5
    history = client.get(prefix + '/tasks/' + claimed['id'] + '/claim-recoveries').json()
    assert len(history['recoveries']) == 1
    revoked = client.post(path + '/revoke', headers=csrf, json={'expected_revision': 1}).json()
    assert revoked['effective']['capabilities'] == []
    fixture = dict(schema='session-hub-bridge-wire-v1', catalog=client.get(prefix + '/hub-capabilities').json(),
        active_projects=client.get('/api/hub/v1/projects').json(), absent_grant=absent, active_grant=active_grant,
        revoked_grant=revoked, active_claims=claims, recoverable_claims=ready_claims, recovery=recovered.json(),
        recovery_replay=replay, recovery_history=history, overview=overview.json(),
        candidate=candidate, audit=client.get(prefix + '/audit').json(),
        installation_review=installation_review, mcp_withdrawn_decision=withdrawn_decision,
        mcp_project_list=projects, mcp_planning=planned, mcp_component=created)
    rendered = json.dumps(fixture, sort_keys=True, indent=2)
    assert token not in rendered and secret not in rendered and sibling['token'] not in rendered
    destination = os.environ.get('EH_SESSION_HUB_WIRE_OUTPUT')
    if destination:
        assert destination == '/tmp/hub-bridge-wire.json'
        with Path(destination).open('x', encoding='utf-8') as output:
            output.write(rendered + '\n')


def test_finite_browser_gateway_paths_only():
    from engineering_hub.private_gateway import browser_route_allowed
    prefix = '/api/hub/v1/agent-connections'
    identity = '01234567-1234-1234-1234-0123456789ab'
    for method, path in [('GET', '/hub-capabilities'), ('GET', '/installations/' + identity + '/hub-grant'),
        ('PUT', '/installations/' + identity + '/hub-grant'), ('GET', '/installations/' + identity + '/task-claims'),
        ('POST', '/tasks/' + identity + '/recover-claim'), ('POST', '/installations/' + identity + '/hub-grant/revoke')]:
        assert browser_route_allowed(method, prefix + path)
    for method, path in [('DELETE', '/installations/' + identity), ('POST', '/hub-capabilities'),
        ('PUT', '/installations/' + identity + '/arbitrary'), ('POST', '/installations/' + identity + '/grant-everything')]:
        assert not browser_route_allowed(method, prefix + path)
    assert not browser_route_allowed('POST', '/api/agent-sessions/v1/exchange')


@pytest.mark.asyncio
async def test_real_public_private_gateway_transport_owner_grants(unified):
    import httpx
    import time
    from engineering_hub.auth import AuthConfig
    from engineering_hub.cloudflare_access import CloudflareAccessVerifier, CloudflareIdentity
    from engineering_hub.private_gateway import PublicBrowserGateway, PrivateBrowserGateway
    from tests.test_hub_private_gateway import exchange, status
    client, app, bus, csrf = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, _, _ = pair(env, caps=())
    config = AuthConfig('https://staging.test', human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://fixture.cloudflareaccess.com', cloudflare_audience='fixture',
        cloudflare_allowed_emails=('owner@example.com',)).cloudflare_config
    verifier = CloudflareAccessVerifier(config)
    async def verify(assertion):
        assert assertion == 'synthetic-access-assertion'
        return CloudflareIdentity('synthetic', 'owner@example.com', 'human:cf:' + 'a' * 64, int(time.time()) + 60)
    verifier.verify = verify
    receiver = PrivateBrowserGateway(app, agent_auth=app.state.settings.agent_auth,
        cloudflare_config=config, access_verifier=verifier)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver)) as upstream:
        gateway = PublicBrowserGateway(upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url='https://staging.test') as browser:
            prefix = '/api/hub/v1/agent-connections'
            path = prefix + '/installations/' + installation + '/hub-grant'
            h = {'Cookie': 'hub_session=' + client.cookies.get('hub_session'),
                'Cf-Access-Jwt-Assertion': 'synthetic-access-assertion', **csrf}
            assert (await browser.get(prefix + '/hub-capabilities')).status_code == 401
            catalog = await browser.get(prefix + '/hub-capabilities', headers=h)
            assert catalog.status_code == 200 and catalog.json()['features']['task_recovery']
            absent = await browser.get(path, headers=h)
            assert absent.status_code == 200 and absent.json()['state'] == 'absent'
            body = dict(expected_revision=0, projects=[PROJECT], capabilities=['project.read'], lifetime_seconds=60)
            no_csrf = {key: value for key, value in h.items() if key != 'X-Hub-CSRF'}
            assert (await browser.put(path, headers=no_csrf, json=body)).status_code == 403
            active = await browser.put(path, headers=h, json=body)
            assert active.status_code == 200 and active.json()['effective']['capabilities'] == ['project.read']
            assert (await browser.post('/api/agent-sessions/v1/exchange', headers=h, json={})).status_code == 403
            assert (await browser.get(path, headers={**h, 'Authorization': 'Bearer synthetic-codex'})).status_code == 403
            for invalid in (path.replace('/installations/', '//installations/'), path + '/arbitrary',
                            prefix + '/installations/%2e%2e/hub-grant'):
                assert status(await exchange(gateway, invalid)) in {400, 403}
            assert status(await exchange(gateway, path, headers=[(b'cookie', b'a'), (b'cookie', b'b')])) == 400
            if Path('/tmp/hub-bridge-wire.json').exists():
                fixture = json.loads(Path('/tmp/hub-bridge-wire.json').read_text())
                fixture['gateway'] = dict(catalog=catalog.json(), absent_grant=absent.json(), active_grant=active.json(),
                    scope='in_process_public_private_gateway_synthetic_Access_verifier')
                Path('/tmp/hub-bridge-wire.json').write_text(json.dumps(fixture, indent=2, sort_keys=True) + '\n')


@pytest.mark.parametrize('kind', ['session', 'grant'])
@pytest.mark.parametrize('store', ['hub', 'knowledge'])
def test_final_expiry_kind_at_each_store_commit(env, monkeypatch, kind, store):
    installation, token, _ = pair(env)
    if kind == 'grant':
        env.bridge.grant_put(OWNER, installation, 1, [PROJECT], list(CAPABILITIES), 60)
    original = env.bridge._live
    def expiring(*args, **kwargs):
        if args[2:] or kwargs.get('revision') is not None:
            env.clock.value += 61 if kind == 'grant' else 901
        return original(*args, **kwargs)
    monkeypatch.setattr(env.bridge, '_live', expiring)
    error = HubError if kind == 'grant' else AgentSessionError
    with pytest.raises(error):
        if store == 'hub':
            proposal(env, token)
        else:
            call(env, token, 'hub_planning_put', lambda a: planning.put_item(env.knowledge, author=a,
                scope='topic', project=PROJECT, topic='synthetic', kind='plan', item_id='item',
                title='Plan', content='Synthetic', status='proposed', expected_version=0))
    assert not env.repo.list_tasks() and not env.knowledge.list(project=PROJECT)


@pytest.mark.parametrize('store', ['hub', 'knowledge'])
def test_revoked_grant_wins_before_either_store_mutates(env, store):
    installation, token, _ = pair(env)
    env.bridge.grant_revoke(OWNER, installation, 1)
    with pytest.raises(HubError, match='hub_grant_required'):
        if store == 'hub':
            proposal(env, token)
        else:
            call(env, token, 'hub_planning_put', lambda _: pytest.fail('must not reach knowledge'))
    assert not env.repo.list_tasks() and not env.knowledge.list(project=PROJECT)


@pytest.mark.parametrize('failure', ['expired_target', 'revoked_target', 'revoked_grant', 'history_full', 'held_run'])
def test_recovery_closed_cases_and_target_commit_expiry(env, monkeypatch, failure):
    installation, token, secret = pair(env)
    task = proposal(env, token)
    task = call(env, token, 'hub_task_claim', lambda a: env.repo.claim_task(a, task['id'], 1))
    env.sessions.revoke_session(OWNER, env.sessions.authenticate_session(token).session_id, 1)
    target = env.sessions.exchange(secret)['session']['id']
    if failure == 'revoked_target':
        env.sessions.revoke_session(OWNER, target, 1)
    elif failure == 'revoked_grant':
        env.bridge.grant_revoke(OWNER, installation, 1)
    elif failure == 'expired_target':
        original = env.bridge._live
        def expires(*args, **kwargs):
            if args[2:]:
                env.clock.value += 901
            return original(*args, **kwargs)
        monkeypatch.setattr(env.bridge, '_live', expires)
    elif failure == 'history_full':
        with env.repo._transaction() as db:
            for i in range(128):
                db.execute('INSERT INTO hub_claim_recoveries VALUES(?,?,?,?,?,?)', (str(i), task['id'], OWNER, 'seed-' + str(i), 'seed', '{}'))
    elif failure == 'held_run':
        with env.repo._transaction() as db:
            db.execute('INSERT INTO hub_entities VALUES(?,?,?)', ('run', 'run', json.dumps({'task_id': task['id'], 'status': 'held'})))
    with pytest.raises((AgentSessionError, HubError)):
        env.bridge.recover_claim(OWNER, task['id'], 2, target, 'Reason', 'recovery')
    unchanged = env.repo.get_task(task['id'])
    assert unchanged['revision'] == 2 and unchanged['owner_session_id'] == task['owner_session_id']


@pytest.mark.parametrize('failure', ['inactive_connection', 'expired_target', 'revoked_target', 'expires_at_commit'])
def test_owner_state_errors_preserve_human_session(unified, monkeypatch, failure):
    client, app, bus, csrf = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    installation, token, secret = pair(env)
    invoke, _ = sdk(client, token)
    task = data(invoke('hub_staging_task_propose', {'project': PROJECT, 'title': 'Task', 'client_request_id': 'key'}))
    task = data(invoke('hub_task_claim', {'task_id': task['id'], 'expected_revision': 1}))
    env.sessions.revoke_session(OWNER, task['owner_session_id'], 1)
    target = env.sessions.exchange(secret)['session']['id']
    prefix = '/api/hub/v1/agent-connections'
    path = prefix + '/tasks/' + task['id'] + '/recover-claim'
    body = dict(expected_revision=2, target_session_id=target, reason='Reason', client_request_id='recover')
    if failure == 'inactive_connection':
        env.sessions.revoke_installation(OWNER, installation, 2)
        response = client.put(prefix + '/installations/' + installation + '/hub-grant', headers=csrf,
            json=dict(expected_revision=1, projects=[PROJECT], capabilities=['task.claim']))
        assert response.status_code == 409 and response.json()['detail']['code'] == 'installation_inactive'
    else:
        if failure == 'revoked_target':
            env.sessions.revoke_session(OWNER, target, 1)
        elif failure == 'expired_target':
            env.sessions.clock.value += 901
        else:
            original = env.bridge._live
            def expires(*args, **kwargs):
                if args[2:]:
                    env.sessions.clock.value += 901
                return original(*args, **kwargs)
            monkeypatch.setattr(env.bridge, '_live', expires)
        response = client.post(path, headers=csrf, json=body)
        assert response.status_code == 422 and response.json()['detail']['code'] == 'invalid_recovery_target'
    assert client.get('/api/hub/v1/auth/me').json()['authenticated']
    client.cookies.clear()
    assert client.get(prefix + '/hub-capabilities').status_code == 401


def test_current_image_bridge_off_contains_records_and_foundation_dto_is_incompatible(tmp_path):
    import sys
    from types import ModuleType
    from pydantic import ValidationError
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from mcp.server.transport_security import TransportSecuritySettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    settings = Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(origin, beau_github_id=42))
    def build(enabled):
        bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
            file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
            agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=enabled),
            transport_security_settings=TransportSecuritySettings(allowed_hosts=['staging.test'], allowed_origins=[origin]))
        app = create_app(settings, bus_app=bus)
        bus.state.agent_sessions.clock = Clock()
        return app, bus
    app, bus = build(True)
    with TestClient(app, base_url=origin) as client:
        env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
        installation, token, _ = pair(env)
        invoke, _ = sdk(client, token)
        task = data(invoke('hub_staging_task_propose', {'project': PROJECT, 'title': 'Persisted', 'client_request_id': 'key'}))
        task = data(invoke('hub_task_claim', {'task_id': task['id'], 'expected_revision': 1}))
        decision = data(invoke('hub_decision_request', {'project': PROJECT, 'title': 'Decision', 'options': ['A', 'B']}))
        decision = data(invoke('hub_entity_withdraw', {'kind': 'decision', 'entity_id': decision['id'], 'expected_revision': 1, 'reason': 'Done'}))
    # Exact baseline source is a pinned immutable test fixture, executed only
    # inside the approved isolated test container. It is never a rollback target.
    baseline = Path(__file__).parent / 'fixtures/foundation-dto-f8379d8.py.txt'
    source = baseline.read_bytes()
    assert hashlib.sha256(source).hexdigest() == '3503494a2864784512cb4b0ce7e82d09419ff6099f0463eda8fa713cf1231981'
    module = ModuleType('engineering_hub._foundation_dto_fixture')
    module.__package__ = 'engineering_hub'
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, str(baseline), 'exec'), module.__dict__)
        with pytest.raises(ValidationError):
            module.TaskDTO.model_validate(task)
        with pytest.raises(ValidationError):
            module.DecisionDTO.model_validate(decision)
    finally:
        sys.modules.pop(module.__name__, None)
    contained, _ = build(False)
    with TestClient(contained, base_url=origin) as client:
        cookie = contained.state.sessions.create_session(42)
        client.cookies.set('hub_session', cookie['cookie_token'])
        assert client.get('/api/hub/v1/overview').status_code == 200
        assert client.get('/api/hub/v1/tasks').json()['items'][0]['owner'] == task['owner']
        assert client.get('/api/hub/v1/tasks/' + task['id']).json()['owner_session_id'] == task['owner_session_id']
        assert client.get('/api/hub/v1/decisions').json()['items'][0]['withdrawn_by'] == decision['withdrawn_by']
        assert client.get('/api/hub/v1/agent-connections/hub-capabilities').status_code == 404
        path = '/api/hub/v1/agent-connections/installations/' + installation + '/hub-grant'
        assert client.put(path, json={'expected_revision': 1}).status_code == 404
        invoke, _ = sdk(client, token)
        assert invoke('hub_task_get', {'task_id': task['id']})['isError']
        assert not invoke('session_identity', {})['isError']


def test_withdrawal_replay_reauthorizes_secondary_project_after_grant_narrowing(env):
    installation, token, _ = pair(env, projects=(PROJECT, 'project-alpha'))
    first = call(env, token, 'hub_decision_request', lambda a:
        env.repo.create_decision(a, PROJECT, 'Original', ['A', 'B']))
    replacement = call(env, token, 'hub_decision_request', lambda a:
        env.repo.create_decision(a, 'project-alpha', 'Replacement', ['A', 'B']))
    def withdraw(actor):
        return env.repo.withdraw(actor, 'decision', first['id'], 1, 'Superseded', replacement['id'])
    withdrawn = call(env, token, 'hub_entity_withdraw', withdraw)
    with env.repo._connection() as db:
        count = db.execute('SELECT count(*) FROM hub_audit').fetchone()[0]
    # Identical authorized replay is idempotent, including its audit record.
    assert call(env, token, 'hub_entity_withdraw', withdraw) == withdrawn
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_audit').fetchone()[0] == count
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT], list(CAPABILITIES))
    with pytest.raises(HubError, match='hub_replacement_unavailable'):
        call(env, token, 'hub_entity_withdraw', withdraw)
    assert env.repo._read(first['id'], 'decision') == withdrawn
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_audit').fetchone()[0] == count


def test_legacy_withdrawal_replay_retains_later_replacement_lifecycle_semantics(env):
    first = env.repo.create_decision(OWNER, PROJECT, 'Original', ['A', 'B'])
    replacement = env.repo.create_decision(OWNER, 'project-alpha', 'Replacement', ['A', 'B'])
    withdrawn = env.repo.withdraw(OWNER, 'decision', first['id'], 1, 'Superseded', replacement['id'])
    env.repo.withdraw(OWNER, 'decision', replacement['id'], 1, 'Later withdrawn')
    env.projects.archive_project(OWNER, 'project-alpha', 1)
    with env.repo._connection() as db:
        count = db.execute('SELECT count(*) FROM hub_audit').fetchone()[0]
    assert env.repo.withdraw(OWNER, 'decision', first['id'], 1, 'Superseded', replacement['id']) == withdrawn
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_audit').fetchone()[0] == count

def test_tracking_review_serializes_with_session_revocation(env):
    _, reviewer, _ = pair(env, 'race-reviewer', family='claude')
    session_id = env.sessions.authenticate_session(reviewer).session_id
    item = env.repo.propose_tracking_task('codex', PROJECT, 'Race claim', 'race-propose')
    links = [{'label':'Evidence', 'url':'https://example.com/evidence'}]
    env.repo.update_tracking_task('codex', item['id'], 1, 'race-claim', progress='Done', evidence_links=links, completion_claim=True)
    entered, release, attempting, revoked = (threading.Event() for _ in range(4))
    def review():
        with env.bridge.admit(reviewer, 'hub_tracking_task_review') as op:
            env.bridge._scope('hub_tracking_task_review', {'task_id': item['id']}, op)
            entered.set()
            assert release.wait(5)
            return env.repo.review_tracking_task(op.principal.actor, item['id'], 2, 'race-review',
                verdict='verify', note='Checked', evidence_links=links)
    def revoke():
        attempting.set()
        env.sessions.revoke_session(OWNER, session_id, 1)
        revoked.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(review)
        try:
            assert entered.wait(5)
            second = pool.submit(revoke)
            assert attempting.wait(5)
            assert not revoked.wait(.1)
        finally:
            release.set()
        assert first.result(timeout=5)['status'] == 'completion_verified'
        second.result(timeout=5)
    with pytest.raises(AgentSessionError):
        call(env, reviewer, 'hub_tracking_task_review', lambda actor: None)
    stored = env.repo._read(item['id'], 'tracking_task')
    assert len(stored['reviews']) == 1 and stored['reviews'][0]['claim_revision'] == 2

def test_tracking_review_revoked_session_cannot_mutate_claim(env):
    _, reviewer, _ = pair(env, 'revoked-reviewer', family='claude')
    session_id = env.sessions.authenticate_session(reviewer).session_id
    item = env.repo.propose_tracking_task('codex', PROJECT, 'Revoked claim', 'revoked-propose')
    links = [{'label':'Evidence', 'url':'https://example.com/evidence'}]
    env.repo.update_tracking_task('codex', item['id'], 1, 'revoked-claim', progress='Done', evidence_links=links, completion_claim=True)
    env.sessions.revoke_session(OWNER, session_id, 1)
    with pytest.raises(AgentSessionError):
        call(env, reviewer, 'hub_tracking_task_review', lambda actor:
            env.repo.review_tracking_task(actor, item['id'], 2, 'revoked-review', verdict='verify', note='Checked', evidence_links=links))
    stored = env.repo._read(item['id'], 'tracking_task')
    assert stored['status'] == 'completion_claimed' and stored['revision'] == 2 and not stored.get('reviews')
