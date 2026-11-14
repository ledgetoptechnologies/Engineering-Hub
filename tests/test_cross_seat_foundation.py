"""Feature-off compatibility and synthetic seat-bound handoff admission."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.repository import HubError
from tests.test_session_hub_bridge import OWNER, PROJECT, Clock, call, data, env, pair, sdk, unified


@pytest.fixture
def enabled(tmp_path):
    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(allowed_hosts=['staging.test'], allowed_origins=[origin]))
    app = create_app(Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(origin, beau_github_id=42),
                              seat_foundation_enabled=True), bus_app=bus)
    bus.state.agent_sessions.clock = Clock()
    with TestClient(app, base_url=origin) as client:
        cookie = app.state.sessions.create_session(42)
        client.cookies.set('hub_session', cookie['cookie_token'])
        yield client, app, bus, {'Origin': origin, 'X-Hub-CSRF': cookie['csrf_token']}


def test_feature_off_has_no_seat_schema_capabilities_routes_or_tools(unified):
    client, app, bus, csrf = unified
    assert app.state.session_hub_bridge.seat_authority is None
    assert not any('cross.seat' in value for value in app.state.session_hub_bridge.capabilities)
    with app.state.repository._connection() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_cross_seat_session'").fetchone()
    assert client.get('/api/hub/v1/projects/engineering-hub/cross-seats').status_code == 404
    assert 'hub_cross_seat_current' not in bus.state.agent_sessions._hub_bridge_tools


def test_owner_binding_and_handoff_enforce_session_generation_and_no_publication(enabled):
    client, app, bus, csrf = enabled
    bridge = app.state.session_hub_bridge
    scope = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=bridge)
    aid, a, _ = pair(scope, name='seat-a', caps=('cross.seat.read', 'cross.seat.handoff'))
    bid, b, _ = pair(scope, name='seat-b', family='claude', caps=('cross.seat.read', 'cross.seat.handoff'))
    a_session = scope.sessions.authenticate_session(a).session_id
    b_session = scope.sessions.authenticate_session(b).session_id
    base = '/api/hub/v1/projects/engineering-hub'
    assert client.post(base + '/cross-seat-topics', json={'topic': 'work', 'name': 'Work'}).status_code == 403
    assert client.post(base + '/cross-seat-topics', headers=csrf, json={'topic': 'work', 'name': 'Work'}).status_code == 200
    seat = client.post(base + '/cross-seats', headers=csrf, json={'topic': 'work', 'role': 'backend'}).json()
    assert seat['coordination_only'] is True and seat['execution_authorization'] is False
    path = '/api/hub/v1/cross-seats/' + seat['id'] + '/binding'
    bound = client.put(path, headers=csrf, json={'expected_revision': 1, 'session_id': a_session}).json()
    assert bound['generation'] == 1 and bound['installation_id'] == aid
    first, _ = sdk(client, a); second, _ = sdk(client, b)
    current = data(first('hub_cross_seat_current', {}))
    assert current['id'] == seat['id'] and current['coordination_only'] is True and current['execution_authorization'] is False
    assert second('hub_cross_seat_current', {})['isError']
    receipt = data(first('hub_cross_seat_handoff', {'generation': 1, 'expected_revision': 2, 'text': 'Handoff'}))
    assert receipt == {'id': seat['id'], 'revision': 3, 'generation': 1,
                       'response_scope': 'mutation_receipt', 'execution_authorization': False}
    transferred = client.put(path, headers=csrf, json={'expected_revision': 3, 'session_id': b_session}).json()
    assert transferred['generation'] == 2 and transferred['installation_id'] == bid
    assert first('hub_cross_seat_current', {})['isError']
    assert first('hub_cross_seat_handoff', {'generation': 1, 'expected_revision': 4, 'text': 'Stale'})['isError']
    assert data(second('hub_cross_seat_current', {}))['handoff'] == 'Handoff'
    assert client.post('/api/hub/v1/cross-seats/' + seat['id'] + '/unbind', json={'expected_revision': 4}).status_code == 403
    cleared = client.post('/api/hub/v1/cross-seats/' + seat['id'] + '/unbind', headers=csrf,
                          json={'expected_revision': 4}).json()
    assert cleared['generation'] == 3 and cleared['session_id'] is None
    assert second('hub_cross_seat_current', {})['isError']
    with app.state.repository._connection() as db:
        assert not db.execute("SELECT 1 FROM hub_outbox WHERE action LIKE 'seat.%' OR action LIKE 'seat_topic.%'").fetchone()


def test_unique_binding_revocation_and_commit_guard(enabled):
    client, app, bus, csrf = enabled
    bridge = app.state.session_hub_bridge
    scope = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=bridge)
    identity, token, _ = pair(scope, name='single-seat', caps=('cross.seat.read', 'cross.seat.handoff'))
    session_id = scope.sessions.authenticate_session(token).session_id
    authority = bridge.seat_authority
    authority.create_topic(OWNER, PROJECT, 'topic', 'Topic')
    first = authority.create_seat(OWNER, PROJECT, 'topic', 'first')
    second = authority.create_seat(OWNER, PROJECT, 'topic', 'second')
    authority.bind(OWNER, first['id'], 1, session_id)
    with pytest.raises(HubError, match='seat_session_already_bound'):
        authority.bind(OWNER, second['id'], 1, session_id)
    bridge.grant_revoke(OWNER, identity, 1)
    with pytest.raises(HubError):
        call(scope, token, 'hub_cross_seat_handoff', lambda actor:
             authority.update_handoff(actor, 1, 2, 'Denied'))
    assert authority.owner_list(OWNER, PROJECT, 'cross_seat', 100, None)['items'][0]['handoff'] == ''


def test_transfer_racing_handoff_has_one_current_generation(enabled):
    _, app, bus, _ = enabled
    bridge = app.state.session_hub_bridge
    scope = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=bridge)
    _, a, _ = pair(scope, name='race-a', caps=('cross.seat.read', 'cross.seat.handoff'))
    _, b, _ = pair(scope, name='race-b', caps=('cross.seat.read', 'cross.seat.handoff'))
    a_id, b_id = scope.sessions.authenticate_session(a).session_id, scope.sessions.authenticate_session(b).session_id
    service = bridge.seat_authority
    service.create_topic(OWNER, PROJECT, 'race', 'Race')
    seat = service.create_seat(OWNER, PROJECT, 'race', 'backend')
    service.bind(OWNER, seat['id'], 1, a_id)
    barrier = threading.Barrier(2)
    def write():
        barrier.wait(timeout=5)
        try:
            return call(scope, a, 'hub_cross_seat_handoff', lambda actor: service.update_handoff(actor, 1, 2, 'Before transfer'))
        except HubError as error:
            return error.code
    def transfer():
        barrier.wait(timeout=5)
        try:
            return service.bind(OWNER, seat['id'], 2, b_id)
        except HubError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write); second = pool.submit(transfer)
        results = (first.result(), second.result())
    assert sum(isinstance(result, dict) for result in results) == 1
    current = service.owner_list(OWNER, PROJECT, 'cross_seat', 10, None)['items'][0]
    assert (current['generation'], current['revision']) in ((1, 3), (2, 3))


def test_legacy_cannot_use_feature_tools_and_response_expiry_fails_closed(enabled, monkeypatch):
    client, app, bus, _ = enabled
    bridge = app.state.session_hub_bridge
    scope = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=bridge)
    _, token, _ = pair(scope, name='response-guard', caps=('cross.seat.read', 'cross.seat.handoff'))
    session_id = scope.sessions.authenticate_session(token).session_id
    bridge.seat_authority.create_topic(OWNER, PROJECT, 'guard', 'Guard')
    seat = bridge.seat_authority.create_seat(OWNER, PROJECT, 'guard', 'role')
    bridge.seat_authority.bind(OWNER, seat['id'], 1, session_id)
    # Legacy family bearer has no authenticated EHS session binding.
    legacy, _ = sdk(client, 'synthetic-codex')
    assert legacy('hub_cross_seat_current', {})['isError']
    invoke, _ = sdk(client, token)
    live = bridge._live
    calls = 0
    def expire(db, secret, revision=None, capability=None, *, session_id=None):
        nonlocal calls
        if capability == 'cross.seat.read':
            calls += 1
            if calls >= 4:
                raise HubError('hub_grant_required', 403)
        return live(db, secret, revision, capability, session_id=session_id)
    monkeypatch.setattr(bridge, '_live', expire)
    assert invoke('hub_cross_seat_current', {})['isError'] and calls >= 4


@pytest.mark.parametrize('expired', ['session', 'grant'])
def test_target_expiry_before_binding_commit_rolls_back(enabled, monkeypatch, expired):
    _, app, bus, _ = enabled
    bridge = app.state.session_hub_bridge
    scope = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=bridge)
    _, token, _ = pair(scope, name='expiring-target', caps=('cross.seat.read', 'cross.seat.handoff'))
    principal = scope.sessions.authenticate_session(token)
    authority = bridge.seat_authority
    authority.create_topic(OWNER, PROJECT, 'expiry', 'Expiry')
    seat = authority.create_seat(OWNER, PROJECT, 'expiry', 'role')
    original = authority._target
    checks = 0
    def expire(db, session_id, project):
        nonlocal checks
        checks += 1
        if checks == 2:
            if expired == 'session':
                scope.sessions.clock.value = principal.expires_at + 1
            else:
                db.execute('UPDATE session_hub_grants SET expires_at=? WHERE installation_id=?',
                           (scope.sessions._now(), principal.installation_id))
        return original(db, session_id, project)
    monkeypatch.setattr(authority, '_target', expire)
    with pytest.raises(HubError, match='seat_target_unavailable'):
        authority.bind(OWNER, seat['id'], 1, principal.session_id)
    current = authority.owner_list(OWNER, PROJECT, 'cross_seat', 10, None)['items'][0]
    assert current['revision'] == 1 and current['generation'] == 0 and current['session_id'] is None


def test_disable_and_reenable_preserves_rows_and_grants(env):
    from engineering_hub.session_hub_bridge import SessionHubBridge
    env.bridge.close()
    env.bridge = SessionHubBridge(env.sessions, env.repo, env.projects, env.components, env.knowledge,
                                  seat_foundation_enabled=True)
    identity, token, _ = pair(env, name='restart-seat', caps=('cross.seat.read', 'cross.seat.handoff'))
    principal = env.sessions.authenticate_session(token)
    authority = env.bridge.seat_authority
    authority.create_topic(OWNER, PROJECT, 'restart', 'Restart')
    seat = authority.create_seat(OWNER, PROJECT, 'restart', 'role')
    authority.bind(OWNER, seat['id'], 1, principal.session_id)
    env.bridge.close()
    env.bridge = SessionHubBridge(env.sessions, env.repo, env.projects, env.components, env.knowledge)
    assert env.bridge.seat_authority is None and 'cross.seat.read' not in env.bridge.capabilities
    with pytest.raises(HubError):
        call(env, token, 'hub_cross_seat_current', lambda actor: None)
    with env.repo._connection() as db:
        assert db.execute("SELECT 1 FROM hub_entities WHERE id=? AND kind='cross_seat'", (seat['id'],)).fetchone()
    env.bridge.close()
    env.bridge = SessionHubBridge(env.sessions, env.repo, env.projects, env.components, env.knowledge,
                                  seat_foundation_enabled=True)
    assert call(env, token, 'hub_cross_seat_current', lambda actor: env.bridge.seat_authority.current(actor))['id'] == seat['id']
    assert env.bridge.grant_get(OWNER, identity)['state'] == 'active'
