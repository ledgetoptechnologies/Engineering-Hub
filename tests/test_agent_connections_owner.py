"""Owner controls on the real unified Hub; only synthetic local credentials."""
import hashlib
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings
import pytest

from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig


OWNER = '/api/hub/v1/agent-connections'
AGENT = '/api/agent-sessions/v1'


@pytest.fixture
def owner_app(tmp_path):
    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    config = AgentSessionsConfig((tmp_path / 'connections.sqlite3').absolute(), 'human:42', origin)
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
                     file_root=tmp_path / 'files', auth=auth, push_events_operational=False,
                     transport_security_settings=TransportSecuritySettings(
                         allowed_hosts=['staging.test'], allowed_origins=[origin]),
                     agent_sessions_config=config)
    settings = Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(origin, beau_github_id=42))
    app = create_app(settings, bus_app=bus)
    bus.state.agent_sessions.clock = lambda: 1793142000.0
    with TestClient(app, base_url=origin) as client:
        cookie = app.state.sessions.create_session(42)
        client.cookies.set(settings.auth_config.cookie_name, cookie['cookie_token'])
        headers = {'Origin': origin, 'X-Hub-CSRF': cookie['csrf_token']}
        yield client, app, bus.state.agent_sessions, headers


def test_agent_activity_is_wired_for_the_owner_only(owner_app):
    client, app, repository, _ = owner_app
    app.state.embedded_bus.state.service.repository.insert(
        sender='codex', recipient='claude', message_type='request', body={'topic': 'live topic', 'text': 'never returned'})
    path = '/api/hub/v1/agents/activity'
    response = client.get(path)
    assert response.status_code == 200 and 'never returned' not in response.text
    assert response.json()['topics'][0]['pending_by_agent'] == {'claude': 1}
    assert client.get(path, headers={'Authorization': 'Bearer synthetic-codex'}).status_code == 403
    repository.owner = 'human:someone-else'
    assert client.get(path).json()['detail']['code'] == 'agent_activity_owner_required'


def pending(repository):
    secret = 'ehi_' + 'A' * 43
    record = repository.request_enrollment('codex', 'Synthetic processing PC', 'fixture',
                                           hashlib.sha256(secret.encode()).hexdigest())
    return secret, record


def test_real_owner_pairing_cas_revoke_and_wire(owner_app):
    client, _, repository, headers = owner_app
    secret, record = pending(repository)
    capabilities = client.get(OWNER + '/capabilities')
    assert capabilities.status_code == 200
    assert capabilities.json() == {'enabled': True, 'environment': 'staging', 'owner': True}
    listed_pending = client.get(OWNER + '/installations').json()
    assert listed_pending['installations'][0]['pairing_code'] == record['pairing_code']
    approve_path = OWNER + '/installations/' + record['id'] + '/approve'
    assert client.post(approve_path, json={'expected_revision': record['revision']}).status_code == 403
    approved = client.post(approve_path, headers=headers,
                           json={'expected_revision': record['revision'], 'lifetime_seconds': 86400})
    assert approved.status_code == 200
    assert approved.json()['state'] == 'active'
    conflict = client.patch(OWNER + '/installations/' + record['id'], headers=headers,
                            json={'expected_revision': record['revision'], 'name': 'stale'})
    assert conflict.status_code == 409 and conflict.json()['detail']['code'] == 'revision_conflict'
    exchange = client.post(AGENT + '/exchange', headers={'Authorization': 'Bearer ' + secret},
                           json={'label': 'fixture session'})
    assert exchange.status_code == 200
    issued = exchange.json()
    sessions = client.get(OWNER + '/installations/' + record['id'] + '/sessions')
    assert sessions.status_code == 200 and len(sessions.json()['sessions']) == 1
    session = sessions.json()['sessions'][0]
    revoked = client.post(OWNER + '/sessions/' + session['id'] + '/revoke', headers=headers,
                          json={'expected_revision': session['revision']})
    assert revoked.status_code == 200 and revoked.json()['state'] == 'revoked'
    assert client.get(AGENT + '/identity', headers={'Authorization': 'Bearer ' + issued['token']}).status_code == 401
    audit = client.get(OWNER + '/audit').json()
    wire = {'schema': 'agent-connections-wire-v1', 'capabilities': capabilities.json(),
            'pending': listed_pending, 'approved': approved.json(), 'sessions': sessions.json(),
            'revoked_session': revoked.json(), 'revision_conflict': conflict.json(), 'audit': audit}
    rendered = json.dumps(wire)
    assert secret not in rendered and issued['token'] not in rendered
    assert 'credential_hash' not in rendered and 'claim_handle' not in rendered
    destination = os.environ.get('EH_AGENT_CONNECTIONS_WIRE_OUTPUT')
    if destination:
        # Owned isolated-test output only; never points at production state.
        path = Path(destination)
        with path.open('x', encoding='utf-8') as output:
            output.write(json.dumps(wire, indent=2, sort_keys=True) + '\n')


def test_owner_endpoints_reject_agent_cookie_fallback_and_bad_csrf(owner_app):
    client, _, repository, headers = owner_app
    _, record = pending(repository)
    path = OWNER + '/installations/' + record['id'] + '/approve'
    for authorization in ('', 'Bearer synthetic-codex', 'Basic malformed', 'Bearer ehs_' + 'z' * 43):
        assert client.get(OWNER + '/installations', headers={'Authorization': authorization}).status_code == 403
    assert client.post(path, headers={**headers, 'Origin': 'https://foreign.test'},
                       json={'expected_revision': record['revision']}).status_code == 403
    bad = client.post(path, headers=headers, json={'expected_revision': True, 'forged_owner': 'human:42'})
    assert bad.status_code == 422 and bad.json() == {'detail': {'code': 'invalid_input'}}


def test_owner_persistent_approval_and_conversion_do_not_bypass_security(owner_app):
    client, _, repository, headers = owner_app
    _, record = pending(repository)
    path = OWNER + '/installations/' + record['id']
    assert client.post(path + '/approve', headers=headers, json={
        'expected_revision': 1, 'until_revoked': True, 'lifetime_seconds': 86400}).status_code == 422
    assert client.post(path + '/approve', headers=headers, json={
        'expected_revision': 1, 'until_revoked': 'true'}).status_code == 422
    approved = client.post(path + '/approve', headers=headers, json={'expected_revision': 1})
    assert approved.status_code == 200 and approved.json()['expires_at'] is not None
    conversion = path + '/keep-approved'
    assert client.post(conversion, json={'expected_revision': 2}).status_code == 403
    assert client.post(conversion, headers={**headers, 'Authorization': 'Bearer synthetic-codex'},
                       json={'expected_revision': 2}).status_code == 403
    assert client.post(conversion, headers=headers, json={'expected_revision': 1}).status_code == 409
    result = client.post(conversion, headers=headers, json={'expected_revision': 2})
    assert result.status_code == 200 and result.json()['expires_at'] is None


def test_owner_explicit_until_revoked_approval(owner_app):
    client, _, repository, headers = owner_app
    _, record = pending(repository)
    result = client.post(OWNER + '/installations/' + record['id'] + '/approve', headers=headers,
                         json={'expected_revision': 1, 'until_revoked': True})
    assert result.status_code == 200 and result.json()['state'] == 'active'
    assert result.json()['expires_at'] is None


def test_nonowner_allowlisted_human_is_not_connection_owner(tmp_path):
    # Owner binding is independent of the current singleton GitHub allowlist.
    origin = 'https://staging.test'
    auth = AuthSettings('fixture-hermes', 'fixture-codex')
    config = AgentSessionsConfig((tmp_path / 'connections.sqlite3').absolute(), 'human:99', origin)
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
                     file_root=tmp_path / 'files', auth=auth, push_events_operational=False,
                     transport_security_settings=TransportSecuritySettings(
                         allowed_hosts=['staging.test'], allowed_origins=[origin]),
                     agent_sessions_config=config)
    settings = Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(origin, beau_github_id=42))
    app = create_app(settings, bus_app=bus)
    with TestClient(app, base_url=origin) as client:
        cookie = app.state.sessions.create_session(42)
        client.cookies.set(settings.auth_config.cookie_name, cookie['cookie_token'])
        response = client.get(OWNER + '/capabilities')
        assert response.status_code == 403
        assert response.json()['detail']['code'] == 'agent_connections_owner_required'


def test_disabled_owner_feature_has_no_routes_or_session_database(tmp_path):
    auth = AuthSettings('fixture-hermes', 'fixture-codex')
    settings = Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig('https://staging.test', beau_github_id=42))
    app = create_app(settings)
    with TestClient(app, base_url='https://staging.test') as client:
        assert client.get(OWNER + '/capabilities').status_code == 404
    assert not (tmp_path / 'connections.sqlite3').exists()


@pytest.mark.parametrize('hardlink', [False, True])
def test_session_database_cannot_alias_hub_database(tmp_path, hardlink):
    origin = 'https://staging.test'
    auth = AuthSettings('fixture-hermes', 'fixture-codex')
    sessions_path = (tmp_path / 'connections.sqlite3').absolute()
    config = AgentSessionsConfig(sessions_path, 'human:42', origin)
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
                     file_root=tmp_path / 'files', auth=auth, push_events_operational=False,
                     transport_security_settings=TransportSecuritySettings(
                         allowed_hosts=['staging.test'], allowed_origins=[origin]),
                     agent_sessions_config=config)
    hub_path = tmp_path / 'hub.sqlite3' if hardlink else sessions_path
    if hardlink:
        os.link(sessions_path, hub_path)
    before = sessions_path.read_bytes()
    settings = Settings(hub_path, auth, AuthConfig(origin, beau_github_id=42))
    with pytest.raises(ValueError, match='origin_and_storage_binding_required'):
        create_app(settings, bus_app=bus)
    assert sessions_path.read_bytes() == before
