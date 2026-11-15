"""Synthetic EHS cross-project shared-only coordination; no peer delivery."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from tests.test_session_hub_bridge import OWNER, PROJECT, Clock, data, pair, sdk, unified


@pytest.fixture
def cross(tmp_path):
    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(allowed_hosts=['staging.test'], allowed_origins=[origin]))
    app = create_app(Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(origin, beau_github_id=42),
                              seat_foundation_enabled=True, cross_requests_enabled=True), bus_app=bus)
    bus.state.agent_sessions.clock = Clock()
    with TestClient(app, base_url=origin) as client:
        cookie = app.state.sessions.create_session(42); client.cookies.set('hub_session', cookie['cookie_token'])
        csrf = {'Origin': origin, 'X-Hub-CSRF': cookie['csrf_token']}
        env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
        yield client, app, env, csrf


def new_seat(env, project, topic, role, caps, family='codex'):
    authority = env.bridge.seat_authority
    try: authority.create_topic(OWNER, project, topic, topic)
    except Exception as error:
        if getattr(error, 'code', None) != 'seat_topic_exists': raise
    seat = authority.create_seat(OWNER, project, topic, role)
    installation, token, _ = pair(env, name=topic + '-' + role, family=family,
        projects=(project,), caps=('cross.seat.read', 'cross.seat.handoff', *caps))
    session = env.sessions.authenticate_session(token).session_id
    bound = authority.bind(OWNER, seat['id'], 1, session)
    return bound, installation, token


def test_disabled_no_cross_request_tools_or_owner_route(unified):
    client, app, bus, csrf = unified
    assert not any('cross.request' in value for value in app.state.session_hub_bridge.capabilities)
    assert client.put('/api/hub/v1/cross-request-route', headers=csrf, json={}).status_code == 404
    assert 'hub_cross_request_create' not in bus.state.agent_sessions._hub_bridge_tools


def test_shared_request_seats_triage_and_explicit_conversation(cross):
    client, app, env, csrf = cross
    source, _, source_token = new_seat(env, PROJECT, 'source', 'writer', ('cross.request.submit', 'cross.request.read', 'cross.request.message'))
    source_peer, _, source_peer_token = new_seat(env, PROJECT, 'source-other', 'peer', ('cross.request.read',))
    target, _, target_token = new_seat(env, 'project-alpha', 'target', 'lead', ('cross.request.read', 'cross.request.triage', 'cross.request.message'), 'claude')
    target_peer, _, target_peer_token = new_seat(env, 'project-alpha', 'target', 'peer', ('cross.request.read',), 'codex')
    outsider, _, outsider_token = new_seat(env, 'ledge-top-ops', 'other', 'peer', ('cross.request.read',))
    policy = dict(source_project=PROJECT, target_project='project-alpha', target_topic='target',
                  participants=[target['id'], target_peer['id']], triager=target['id'], active=True,
                  share_with_source_project=False, expected_revision=0)
    assert client.put('/api/hub/v1/cross-request-route', json=policy).status_code == 403
    route = client.put('/api/hub/v1/cross-request-route', headers=csrf, json=policy).json()
    source_call, _ = sdk(client, source_token); source_peer_call, _ = sdk(client, source_peer_token)
    target_call, _ = sdk(client, target_token); target_peer_call, _ = sdk(client, target_peer_token)
    outsider_call, _ = sdk(client, outsider_token)
    routes = data(source_call('hub_cross_routes', {'generation': 1}))['items']
    assert routes == [{'id': route['id'], 'target_project': 'project-alpha', 'target_topic': 'target', 'revision': 1}]
    request = {'generation': 1, 'route_id': route['id'], 'title': 'Coordinate', 'client_request_id': 'stable'}
    receipt = data(source_call('hub_cross_request_create', request)); identity = receipt['id']
    assert receipt['response_scope'] == 'mutation_receipt' and not receipt['current_state_included']
    assert data(source_call('hub_cross_request_create', request)) == receipt
    assert source_call('hub_cross_request_create', dict(request, title='Changed'))['isError']
    assert source_peer_call('hub_cross_request_get', {'generation': 1, 'request_id': identity})['isError']
    assert outsider_call('hub_cross_request_get', {'generation': 1, 'request_id': identity})['isError']
    assert data(target_peer_call('hub_cross_request_get', {'generation': 1, 'request_id': identity}))['status'] == 'open'
    triage = {'generation': 1, 'request_id': identity, 'expected_revision': 1, 'status': 'accepted', 'client_request_id': 'accept'}
    assert target_peer_call('hub_cross_request_transition', triage)['isError']
    assert source_call('hub_cross_request_transition', triage)['isError']
    assert data(target_call('hub_cross_request_transition', triage))['id'] == identity
    assert data(target_call('hub_cross_request_transition', triage))['id'] == identity
    sent = data(source_call('hub_cross_message_append', {'generation': 1, 'request_id': identity, 'text': 'Shared text', 'client_request_id': 'msg'}))
    assert sent['response_scope'] == 'mutation_receipt'
    assert target_peer_call('hub_cross_message_append', {'generation': 1, 'request_id': identity, 'text': 'No grant', 'client_request_id': 'deny'})['isError']
    messages = data(target_call('hub_cross_message_list', {'generation': 1, 'request_id': identity}))['items']
    assert [message['text'] for message in messages] == ['Shared text']
    policy.update(expected_revision=1, share_with_source_project=True)
    assert client.put('/api/hub/v1/cross-request-route', headers=csrf, json=policy).status_code == 200
    assert data(source_peer_call('hub_cross_request_get', {'generation': 1, 'request_id': identity}))['id'] == identity
    policy.update(expected_revision=2, active=False)
    assert client.put('/api/hub/v1/cross-request-route', headers=csrf, json=policy).status_code == 200
    assert source_call('hub_cross_request_get', {'generation': 1, 'request_id': identity})['isError']
    assert target_call('hub_cross_message_list', {'generation': 1, 'request_id': identity})['isError']
    with app.state.repository._connection() as db:
        assert not db.execute("SELECT 1 FROM hub_outbox WHERE action LIKE 'cross_%'").fetchone()
        assert not db.execute("SELECT 1 FROM hub_entities WHERE kind IN ('task','tracking_task','role_dispatch')").fetchone()


def test_commit_scope_recheck_rolls_back_request_and_audit(cross, monkeypatch):
    from agent_message_bus import session_hub_context
    from engineering_hub.repository import HubError
    from tests.test_session_hub_bridge import call
    _, app, env, _ = cross
    source, _, source_token = new_seat(env, PROJECT, 'source-commit', 'writer', ('cross.request.submit',))
    target, _, _ = new_seat(env, 'project-alpha', 'target-commit', 'lead', ('cross.request.read',))
    service = env.bridge.cross_requests
    route = service.set_route(OWNER, PROJECT, 'project-alpha', 'target-commit', [target['id']], target['id'], True, False, 0)
    original_save = service.repo._save
    def deny(db, actor, action, kind, item, *args, **kwargs):
        result = original_save(db, actor, action, kind, item, *args, **kwargs)
        if action == 'cross_request.created':
            operation = session_hub_context.current.get()
            assert PROJECT in operation.used_projects
            operation.projects = ()
        return result
    monkeypatch.setattr(service.repo, '_save', deny)
    with pytest.raises(HubError, match='hub_project_forbidden'):
        call(env, source_token, 'hub_cross_request_create', lambda actor:
             service.create(actor, 1, route['id'], 'Denied', None, None, 'commit-denied'))
    with app.state.repository._connection() as db:
        assert not db.execute("SELECT 1 FROM hub_entities WHERE kind='cross_request'").fetchone()
        assert not db.execute("SELECT 1 FROM hub_audit WHERE action='cross_request.created'").fetchone()
        assert not db.execute("SELECT 1 FROM hub_requests WHERE key='commit-denied'").fetchone()


def test_changed_policy_and_target_transfer_revoke_old_access(cross):
    client, _, env, csrf = cross
    source, _, source_token = new_seat(env, PROJECT, 'source-revoke', 'writer', ('cross.request.submit', 'cross.request.read'))
    target, _, target_token = new_seat(env, 'project-alpha', 'target-revoke', 'lead', ('cross.request.read', 'cross.request.triage'))
    other, _, other_token = new_seat(env, 'project-alpha', 'target-revoke', 'other', ('cross.request.read',), 'claude')
    policy = dict(source_project=PROJECT, target_project='project-alpha', target_topic='target-revoke',
                  participants=[target['id']], triager=target['id'], active=True, share_with_source_project=False, expected_revision=0)
    route = client.put('/api/hub/v1/cross-request-route', headers=csrf, json=policy).json()
    src, _ = sdk(client, source_token); dest, _ = sdk(client, target_token); peer, _ = sdk(client, other_token)
    identity = data(src('hub_cross_request_create', {'generation': 1, 'route_id': route['id'], 'title': 'Work', 'client_request_id': 'one'}))['id']
    assert dest('hub_cross_request_get', {'generation': 1, 'request_id': identity}).get('isError') is not True
    assert peer('hub_cross_request_get', {'generation': 1, 'request_id': identity})['isError']
    policy.update(expected_revision=1, participants=[other['id']], triager=other['id'])
    assert client.put('/api/hub/v1/cross-request-route', headers=csrf, json=policy).status_code == 200
    assert dest('hub_cross_request_get', {'generation': 1, 'request_id': identity})['isError']
    assert data(peer('hub_cross_request_get', {'generation': 1, 'request_id': identity}))['id'] == identity
    # Explicit owner unbinding invalidates the previously authorized seat.
    env.bridge.seat_authority.unbind(OWNER, other['id'], 2)
    assert peer('hub_cross_request_get', {'generation': 1, 'request_id': identity})['isError']


def test_capacity_and_clock_fail_closed_but_accepted_retry_survives(cross, monkeypatch):
    from engineering_hub import cross_requests
    from engineering_hub.repository import HubError
    from tests.test_session_hub_bridge import call
    _, _, env, _ = cross
    source, _, token = new_seat(env, PROJECT, 'source-cap', 'writer', ('cross.request.submit',))
    target, _, _ = new_seat(env, 'project-alpha', 'target-cap', 'lead', ('cross.request.read',))
    service = env.bridge.cross_requests
    route = service.set_route(OWNER, PROJECT, 'project-alpha', 'target-cap', [target['id']], target['id'], True, False, 0)
    def create(key):
        return call(env, token, 'hub_cross_request_create', lambda actor:
                    service.create(actor, 1, route['id'], 'Work', None, None, key))
    monkeypatch.setattr(cross_requests, '_MAX_REQUESTS', 1)
    result = create('first')
    assert create('first') == result
    with pytest.raises(HubError, match='cross_request_capacity'):
        create('second')
    monkeypatch.setattr(cross_requests, '_MAX_REQUESTS', 4096)
    env.sessions.clock.value -= 1
    assert create('first') == result
    with pytest.raises(HubError, match='cross_request_clock_untrusted'):
        create('third')


def test_message_capacity_and_accepted_retry(cross, monkeypatch):
    from engineering_hub import cross_requests
    from engineering_hub.repository import HubError
    from tests.test_session_hub_bridge import call
    _, _, env, _ = cross
    source, _, token = new_seat(env, PROJECT, 'source-message-cap', 'writer', ('cross.request.submit', 'cross.request.message'))
    target, _, _ = new_seat(env, 'project-alpha', 'target-message-cap', 'lead', ('cross.request.read',))
    service = env.bridge.cross_requests
    route = service.set_route(OWNER, PROJECT, 'project-alpha', 'target-message-cap', [target['id']], target['id'], True, False, 0)
    created = call(env, token, 'hub_cross_request_create', lambda actor:
                   service.create(actor, 1, route['id'], 'Work', None, None, 'create'))
    def append(key, text='Message'):
        return call(env, token, 'hub_cross_message_append', lambda actor:
                    service.append(actor, 1, created['id'], text, key))
    monkeypatch.setattr(cross_requests, '_MAX_MESSAGES', 1)
    first = append('one')
    assert append('one') == first
    with pytest.raises(HubError, match='cross_request_capacity'):
        append('two')
    with pytest.raises(HubError, match='idempotency_conflict'):
        append('one', 'Changed')


def test_legacy_denied_and_source_project_grant_replacement_blocks_read(cross):
    client, _, env, _ = cross
    source, installation, source_token = new_seat(env, PROJECT, 'source-grant', 'writer', ('cross.request.submit', 'cross.request.read'))
    target, _, _ = new_seat(env, 'project-alpha', 'target-grant', 'lead', ('cross.request.read',))
    route = env.bridge.cross_requests.set_route(OWNER, PROJECT, 'project-alpha', 'target-grant', [target['id']], target['id'], True, False, 0)
    src, _ = sdk(client, source_token)
    identity = data(src('hub_cross_request_create', {'generation': 1, 'route_id': route['id'], 'title': 'Work', 'client_request_id': 'request'}))['id']
    legacy, _ = sdk(client, 'synthetic-codex')
    assert legacy('hub_cross_request_get', {'generation': 1, 'request_id': identity})['isError']
    env.bridge.grant_put(OWNER, installation, 1, ['ledge-top-ops'], ['cross.seat.read', 'cross.seat.handoff', 'cross.request.read'])
    assert src('hub_cross_request_get', {'generation': 1, 'request_id': identity})['isError']


def test_response_recheck_denies_if_policy_revoked_after_snapshot(cross, monkeypatch):
    client, _, env, csrf = cross
    source, _, token = new_seat(env, PROJECT, 'source-recheck', 'writer', ('cross.request.submit', 'cross.request.read'))
    target, _, _ = new_seat(env, 'project-alpha', 'target-recheck', 'lead', ('cross.request.read',))
    service = env.bridge.cross_requests
    policy = dict(source_project=PROJECT, target_project='project-alpha', target_topic='target-recheck',
                  participants=[target['id']], triager=target['id'], active=True, share_with_source_project=False, expected_revision=0)
    route = client.put('/api/hub/v1/cross-request-route', headers=csrf, json=policy).json()
    invoke, _ = sdk(client, token)
    identity = data(invoke('hub_cross_request_create', {'generation': 1, 'route_id': route['id'],
                    'title': 'Private shared request', 'client_request_id': 'request'}))['id']
    original = service.get
    calls = 0
    def revoke_after_snapshot(actor, generation, request_id):
        nonlocal calls
        result = original(actor, generation, request_id)
        calls += 1
        if calls == 1:
            from agent_message_bus import session_hub_context
            marker = session_hub_context.current.set(None)
            try:
                service.set_route(OWNER, PROJECT, 'project-alpha', 'target-recheck', [target['id']],
                                  target['id'], False, False, 1)
            finally:
                session_hub_context.current.reset(marker)
        return result
    monkeypatch.setattr(service, 'get', revoke_after_snapshot)
    result = invoke('hub_cross_request_get', {'generation': 1, 'request_id': identity})
    assert result['isError'] and 'Private shared request' not in str(result)


def test_human_owner_triage_is_csrf_guarded_and_idempotent(cross):
    client, _, env, csrf = cross
    source, _, token = new_seat(env, PROJECT, 'source-owner', 'writer', ('cross.request.submit',))
    target, _, _ = new_seat(env, 'project-alpha', 'target-owner', 'lead', ('cross.request.read',))
    route = env.bridge.cross_requests.set_route(OWNER, PROJECT, 'project-alpha', 'target-owner',
                                               [target['id']], target['id'], True, False, 0)
    invoke, _ = sdk(client, token)
    identity = data(invoke('hub_cross_request_create', {'generation': 1, 'route_id': route['id'],
                    'title': 'Owner triage', 'client_request_id': 'request'}))['id']
    path = '/api/hub/v1/cross-requests/' + identity + '/transition'
    body = {'expected_revision': 1, 'status': 'accepted', 'client_request_id': 'owner-accept'}
    assert client.post(path, json=body).status_code == 403
    first = client.post(path, headers=csrf, json=body)
    assert first.status_code == 200 and first.json()['id'] == identity
    assert client.post(path, headers=csrf, json=body).json() == first.json()
    assert client.post(path, headers=csrf, json={**body, 'status': 'declined'}).status_code == 409
    assert client.get('/api/hub/v1/cross-requests/' + identity).json()['status'] == 'accepted'
    inbound = client.get('/api/hub/v1/projects/project-alpha/cross-requests?direction=inbound').json()
    assert [item['id'] for item in inbound['items']] == [identity]


def test_mixed_project_records_are_filtered_before_pagination(cross):
    from tests.test_session_hub_bridge import call
    _, _, env, _ = cross
    source, _, token = new_seat(env, PROJECT, 'source-pages', 'writer', ('cross.request.submit', 'cross.request.read'))
    target, _, _ = new_seat(env, 'project-alpha', 'target-pages', 'lead', ('cross.request.read',))
    other, _, other_token = new_seat(env, 'ledge-top-ops', 'other-pages', 'writer', ('cross.request.submit',))
    service = env.bridge.cross_requests
    route = service.set_route(OWNER, PROJECT, 'project-alpha', 'target-pages', [target['id']], target['id'], True, False, 0)
    other_route = service.set_route(OWNER, 'ledge-top-ops', 'project-alpha', 'target-pages', [target['id']], target['id'], True, False, 0)
    own = []
    for index in range(58):
        if index and index % 10 == 0: env.sessions.clock.value += 61
        result = call(env, token, 'hub_cross_request_create', lambda actor: service.create(
            actor, 1, route['id'], 'Visible', None, None, f'visible-{index}'))
        own.append(result['id'])
        if index < 3:
            call(env, other_token, 'hub_cross_request_create', lambda actor: service.create(
                actor, 1, other_route['id'], 'Private other source', None, None, f'other-{index}'))
    cursor = None; found = []
    while True:
        page = call(env, token, 'hub_cross_request_list', lambda actor: service.list(actor, 1, 17, cursor))
        found.extend(item['id'] for item in page['items'])
        assert all(item['title'] == 'Visible' for item in page['items'])
        cursor = page['next_after']
        if cursor is None: break
    assert found == sorted(own)


def test_target_transfer_serializes_against_triage(cross):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from engineering_hub.repository import HubError
    from tests.test_session_hub_bridge import call
    _, _, env, _ = cross
    source, _, source_token = new_seat(env, PROJECT, 'source-race', 'writer', ('cross.request.submit',))
    target, _, target_token = new_seat(env, 'project-alpha', 'target-race', 'lead', ('cross.request.triage',))
    _, replacement_token, _ = pair(env, name='target-replacement', family='claude', projects=('project-alpha',),
        caps=('cross.seat.read', 'cross.seat.handoff', 'cross.request.triage'))
    replacement_session = env.sessions.authenticate_session(replacement_token).session_id
    service = env.bridge.cross_requests
    route = service.set_route(OWNER, PROJECT, 'project-alpha', 'target-race', [target['id']], target['id'], True, False, 0)
    request = call(env, source_token, 'hub_cross_request_create', lambda actor:
                   service.create(actor, 1, route['id'], 'Race', None, None, 'create'))
    barrier = threading.Barrier(2)
    def triage():
        barrier.wait(timeout=5)
        try:
            return call(env, target_token, 'hub_cross_request_transition', lambda actor:
                        service.transition(actor, 1, request['id'], 1, 'accepted', 'race'))
        except HubError as error: return error.code
    def transfer():
        barrier.wait(timeout=5)
        return env.bridge.seat_authority.bind(OWNER, target['id'], 2, replacement_session)
    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(triage); b = executor.submit(transfer)
        result, rebound = a.result(), b.result()
    assert rebound['generation'] == 2
    assert isinstance(result, dict) or result == 'cross_request_unavailable' or result == 'seat_unavailable'
    with pytest.raises(HubError):
        call(env, target_token, 'hub_cross_request_transition', lambda actor:
             service.transition(actor, 1, request['id'], 1, 'accepted', 'after-transfer'))


@pytest.mark.parametrize('dimension', ['source_project', 'target_project'])
def test_project_quota_preserves_exact_retry(cross, dimension):
    import json
    import uuid
    from engineering_hub.repository import HubError
    from tests.test_session_hub_bridge import call
    _, app, env, _ = cross
    _, _, token = new_seat(env, PROJECT, 'source-project-cap', 'writer', ('cross.request.submit',))
    target, _, _ = new_seat(env, 'project-alpha', 'target-project-cap', 'lead', ('cross.request.read',))
    service = env.bridge.cross_requests
    route = service.set_route(OWNER, PROJECT, 'project-alpha', 'target-project-cap', [target['id']], target['id'], True, False, 0)
    def create(key):
        return call(env, token, 'hub_cross_request_create', lambda actor:
                    service.create(actor, 1, route['id'], 'Work', None, None, key))
    original = create('original')
    value = PROJECT if dimension == 'source_project' else 'project-alpha'
    with app.state.repository._transaction() as db:
        db.executemany('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                       [(str(uuid.uuid4()), 'cross_request', json.dumps({dimension: value})) for _ in range(511)])
    assert create('original') == original
    with pytest.raises(HubError, match='cross_request_capacity'):
        create('new')


def test_rate_limit_uses_durable_admission_and_preserves_retry(cross):
    from engineering_hub.repository import HubError
    from tests.test_session_hub_bridge import call
    _, app, env, _ = cross
    _, installation, token = new_seat(env, PROJECT, 'source-rate', 'writer', ('cross.request.submit',))
    target, _, _ = new_seat(env, 'project-alpha', 'target-rate', 'lead', ('cross.request.read',))
    service = env.bridge.cross_requests
    route = service.set_route(OWNER, PROJECT, 'project-alpha', 'target-rate', [target['id']], target['id'], True, False, 0)
    def create(key):
        return call(env, token, 'hub_cross_request_create', lambda actor:
                    service.create(actor, 1, route['id'], 'Work', None, None, key))
    original = create('original')
    with app.state.repository._transaction() as db:
        db.executemany('INSERT INTO hub_cross_rate_events VALUES(?,?,?)',
            [('installation:' + installation, 'cross_request.created', env.sessions._now())] * 19)
    assert create('original') == original
    with pytest.raises(HubError, match='cross_request_rate_limited'):
        create('new')
    env.sessions.clock.value += 61
    assert create('new')['id'] != original['id']
