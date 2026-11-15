"""Direct task retrieval must use the caller's current project-scoped grant."""
import uuid
from types import SimpleNamespace

import pytest

from engineering_hub.repository import HubError
from tests.test_session_hub_bridge import OWNER, PROJECT, call, data, env, pair, sdk, unified


def test_current_record_and_direct_scope(env):
    _, token, _ = pair(env, name='direct-get', caps=('tracking.propose', 'tracking.read', 'tracking.update'))
    item = call(env, token, 'hub_tracking_task_propose', lambda actor:
        env.repo.propose_tracking_task(actor, PROJECT, 'Exact work', 'get-task'))
    updated = call(env, token, 'hub_tracking_task_update', lambda actor:
        env.repo.update_tracking_task(actor, item['id'], 1, 'progress', progress='Evidence pending', evidence_links=[]))
    got = call(env, token, 'hub_tracking_task_get', lambda actor: env.repo.get_tracking_task(actor, item['id']))
    assert got['id'] == item['id'] and got['revision'] == updated['revision'] == 2
    assert got['updates'][-1]['progress'] == 'Evidence pending'
    for identity in ('not-a-uuid', str(uuid.uuid4()), env.repo.create_task(OWNER, PROJECT, 'Other kind', 'kind')['id']):
        with pytest.raises(HubError, match='hub_tracking_task_unavailable'):
            call(env, token, 'hub_tracking_task_get', lambda actor: env.repo.get_tracking_task(actor, identity))
    with pytest.raises(HubError, match='hub_tracking_task_unavailable'):
        env.repo.get_tracking_task('codex', item['id'])


def test_real_mcp_project_isolation_and_expiry(unified, monkeypatch):
    client, app, bus, _ = unified
    session_env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    repo = app.state.repository
    owner_id, writer, _ = pair(session_env, 'direct-writer', caps=('tracking.propose', 'tracking.read', 'tracking.update'))
    _, outsider, _ = pair(session_env, 'direct-outsider', caps=('tracking.read',), projects=('project-alpha',))
    writer_call, _ = sdk(client, writer)
    outsider_call, _ = sdk(client, outsider)
    item = data(writer_call('hub_tracking_task_propose', {'project': PROJECT, 'title': 'Private fixture', 'client_request_id': 'fixture'}))
    item = data(writer_call('hub_tracking_task_update', {'task_id': item['id'], 'expected_revision': item['revision'],
        'client_request_id': 'progress', 'progress': 'Pending', 'evidence_links': []}))
    got = data(writer_call('hub_tracking_task_get', {'task_id': item['id']}))
    assert got['id'] == item['id'] and got['revision'] == item['revision'] and got['status'] == 'open'
    for task_id in (item['id'], str(uuid.uuid4())):
        denied = outsider_call('hub_tracking_task_get', {'task_id': task_id})
        assert denied.get('isError') and 'Private fixture' not in str(denied)
    assert writer_call('hub_tracking_task_get', {'task_id': item['id'].upper()}).get('isError')
    legacy_call, _ = sdk(client, 'synthetic-codex')
    assert legacy_call('hub_tracking_task_get', {'task_id': item['id']}).get('isError')

    # Expire the exact session after the row read; the transaction's before-
    # commit guard must deny before any task content reaches the caller.
    original = repo._get
    def expire_after_read(db, identity, kind):
        value = original(db, identity, kind)
        if kind == 'tracking_task' and db.in_transaction:
            session_env.sessions.clock.value += 1000
        return value
    monkeypatch.setattr(repo, '_get', expire_after_read)
    denied = writer_call('hub_tracking_task_get', {'task_id': item['id']})
    assert denied.get('isError') and 'Private fixture' not in str(denied)


def test_archived_project_and_missing_grant_denied(env):
    project_id = 'archive-case'
    env.projects.create_project(OWNER, project_id, 'Archive case', 'create-archive-case')
    installation, token, _ = pair(env, name='archive-read', caps=('tracking.propose', 'tracking.read'), projects=(project_id,))
    item = call(env, token, 'hub_tracking_task_propose', lambda actor:
        env.repo.propose_tracking_task(actor, project_id, 'Do not expose', 'archive'))
    env.bridge.grant_put(OWNER, installation, 1, [project_id], ['tracking.propose'])
    with pytest.raises(HubError):
        call(env, token, 'hub_tracking_task_get', lambda actor: env.repo.get_tracking_task(actor, item['id']))
    env.bridge.grant_put(OWNER, installation, 2, [project_id], ['tracking.propose', 'tracking.read'])
    project = env.projects.get_project(project_id)
    env.projects.archive_project(OWNER, project_id, project['revision'])
    with pytest.raises(HubError, match='hub_tracking_task_unavailable'):
        call(env, token, 'hub_tracking_task_get', lambda actor: env.repo.get_tracking_task(actor, item['id']))
