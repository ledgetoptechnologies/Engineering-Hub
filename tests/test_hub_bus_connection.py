"""Publisher-backed transport projection, not model/session wake acceptance."""
from datetime import datetime, timedelta, timezone
import json
import sys

import pytest
from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.auth import AuthSettings
from agent_message_bus.push import PushPublisher
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.bus_connection import project_bus_connections


BASE = '/api/hub/v1'
ORIGIN = 'https://hub.test'
AUTH = AuthSettings('private-hermes-token', 'private-codex-token', claude_token='private-claude-token')
HEADERS = {'Authorization': 'Bearer ' + AUTH.codex_token}


def setup_app(tmp_path, *, embedded=True, **kwargs):
    if embedded:
        if sys.platform != 'linux':
            pytest.skip('Embedded bus file ownership requires Linux')
        from agent_message_bus.app import create_app as create_bus
    settings = Settings(tmp_path / 'hub.db', AUTH, AuthConfig(ORIGIN, beau_github_id=42))
    bus = create_bus(db_path=tmp_path / 'messages.db', audit_path=tmp_path / 'audit.jsonl',
                     file_root=tmp_path / 'files', auth=AUTH, push_events_operational=False,
                     transport_security_settings=TransportSecuritySettings(
                         allowed_hosts=['hub.test'], allowed_origins=[ORIGIN])) if embedded else None
    return create_app(settings, bus_app=bus, **kwargs), bus


def codex(response):
    return next(agent for agent in response if agent['id'] == 'codex')


@pytest.mark.asyncio
async def test_actual_publisher_projection_preserves_stream_subscription_distinction():
    publisher = PushPublisher(enabled=True)
    await publisher.subscribe('private-session', 'codex', object())
    assert project_bus_connections(await publisher.presence_status())['codex']['state'] == 'not_observed'
    stream = await publisher.open_presence_stream('private-session', 'codex')
    observed = project_bus_connections(await publisher.presence_status())['codex']
    assert observed['state'] == 'connected' and observed['subscribed'] is True
    await publisher.record_presence_stream_write(stream)
    assert project_bus_connections(await publisher.presence_status())['codex']['last_stream_write_at'] is not None
    await publisher.unsubscribe('private-session', 'codex')
    observed = project_bus_connections(await publisher.presence_status())['codex']
    assert observed['state'] == 'connected' and observed['subscribed'] is False
    await publisher.close_presence_stream(stream)
    assert project_bus_connections(await publisher.presence_status())['codex']['state'] == 'not_observed'


def test_real_publisher_stream_subscription_drop_project_without_chat_readiness(tmp_path):
    app, bus = setup_app(tmp_path)
    publisher = bus.state.push_publisher
    with TestClient(app, base_url=ORIGIN) as client:
        before = codex(client.get(BASE + '/agents', headers=HEADERS).json()['items'])
        assert before['connection']['state'] == 'not_observed'
        assert before['connection']['active_streams'] == 0
        client.portal.call(publisher.activate)
        stream = client.portal.call(publisher.open_presence_stream, 'private-runtime-session', 'codex')
        opened = codex(client.get(BASE + '/overview', headers=HEADERS).json()['agents'])
        assert opened['connection']['state'] == 'connected'
        assert opened['connection']['subscribed'] is False
        client.portal.call(publisher.subscribe, 'private-runtime-session', 'codex', object())
        client.portal.call(publisher.record_presence_stream_write, stream)
        subscribed = codex(client.get(BASE + '/agents', headers=HEADERS).json()['items'])
        assert subscribed['connection']['active_streams'] == 1
        assert subscribed['connection']['inbox_subscriptions'] == 1
        assert subscribed['connection']['subscribed'] is True
        assert subscribed['connection']['last_stream_write_at'] is not None
        assert subscribed['chat_readiness'] == 'unverified' and subscribed['transport'] == 'unknown'
        assert subscribed['reason'] == 'live_presence_adapter_not_configured'
        observed = datetime.fromisoformat(subscribed['connection']['observed_at'].replace('Z', '+00:00'))
        expiry = datetime.fromisoformat(subscribed['connection']['valid_until'].replace('Z', '+00:00'))
        assert expiry - observed == timedelta(seconds=30)
        assert subscribed['connection']['scope'] == 'this_server_process'
        assert 'private-runtime-session' not in json.dumps(subscribed)
        client.portal.call(publisher.unsubscribe, 'private-runtime-session', 'codex')
        assert codex(client.get(BASE + '/agents', headers=HEADERS).json()['items'])['connection']['subscribed'] is False
        client.portal.call(publisher.close_presence_stream, stream)
        closed = codex(client.get(BASE + '/agents', headers=HEADERS).json()['items'])
        assert closed['connection']['state'] == 'not_observed'  # Not offline.


def test_native_source_failure_does_not_erase_observed_stream(tmp_path):
    app, bus = setup_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        publisher = bus.state.push_publisher
        stream = client.portal.call(publisher.open_presence_stream, 'private-session', 'codex')
        client.portal.call(publisher.fail_source, 'private-source-error')
        response = client.get(BASE + '/agents', headers=HEADERS)
        item = codex(response.json()['items'])['connection']
        assert item['state'] == 'connected' and item['native_push_available'] is False
        assert item['subscribed'] is False and 'private-source-error' not in response.text
        client.portal.call(publisher.close_presence_stream, stream)


def test_api_refresh_cannot_revive_stale_transport_or_claim_chat_readiness(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('agent_message_bus.push.monotonic', lambda: clock[0])
    app, bus = setup_app(tmp_path)
    publisher = bus.state.push_publisher
    with TestClient(app, base_url=ORIGIN) as client:
        client.portal.call(publisher.activate)
        stream = client.portal.call(publisher.open_presence_stream, 'private-session', 'codex')
        client.portal.call(publisher.subscribe, 'private-session', 'codex', object())
        client.portal.call(publisher.record_presence_stream_write, stream)
        assert codex(client.get(BASE + '/agents', headers=HEADERS).json()['items'])['connection']['state'] == 'connected'
        clock[0] += 45
        for path, field in [('/agents', 'items'), ('/overview', 'agents'), ('/agents', 'items')]:
            response = client.get(BASE + path, headers=HEADERS)
            assert response.status_code == 200
            item = codex(response.json()[field])
            assert item['connection']['state'] == 'stale'
            assert item['connection']['stale_streams'] == 1
            assert item['connection']['active_streams'] == item['connection']['inbox_subscriptions'] == 0
            assert item['connection']['subscribed'] is False
            assert item['chat_readiness'] == 'unverified'
            assert 'private-session' not in response.text
        client.portal.call(publisher.close_presence_stream, stream)


@pytest.mark.parametrize('mode', ['error', 'malformed', 'missing'])
def test_unavailable_publisher_is_unknown_and_does_not_leak_errors(tmp_path, monkeypatch, mode):
    app, bus = setup_app(tmp_path)
    async def unavailable():
        if mode == 'error':
            raise RuntimeError('private-runtime-error')
        return {'session_id': 'private-runtime-error', 'agents': []}
    with TestClient(app, base_url=ORIGIN) as client:
        if mode == 'missing':
            bus.state.push_publisher = None
        else:
            monkeypatch.setattr(bus.state.push_publisher, 'presence_status', unavailable)
        response = client.get(BASE + '/agents', headers=HEADERS)
        assert response.status_code == 200
        item = codex(response.json()['items'])['connection']
        assert item['state'] == 'unavailable'
        assert item['active_streams'] is item['subscribed'] is item['observed_at'] is item['valid_until'] is None
        assert 'private-runtime-error' not in response.text


def test_standalone_legacy_unknown_no_connection_and_existing_auth(tmp_path):
    app, _ = setup_app(tmp_path, embedded=False)
    with TestClient(app, base_url=ORIGIN) as client:
        for path in ['/agents', '/overview']:
            assert client.get(BASE + path).status_code == 401
            assert client.get(BASE + path, headers={'Authorization': 'Bearer invalid'}).status_code == 401
        data = client.get(BASE + '/agents', headers=HEADERS).json()
        assert all('connection' not in item and item['chat_readiness'] == 'unverified' for item in data['items'])
        assert not any(token in json.dumps(data) for token in [AUTH.codex_token, AUTH.hermes_token, AUTH.claude_token])


@pytest.mark.parametrize('change', ['stale', 'future', 'duplicate', 'boolean_count', 'subscription', 'private_only'])
def test_malformed_or_stale_snapshot_never_becomes_connected(tmp_path, change):
    now = datetime.now(timezone.utc)
    snapshot = {'observed_at': now.isoformat(), 'scope': 'this_server_process', 'native_push_available': True,
        'agents': [dict(agent=agent, active_streams=1, inbox_subscriptions=0, subscribed=False,
                        last_stream_write_at=None, stale_streams=0, valid_for_seconds=30)
                   for agent in ('codex', 'hermes', 'claude')]}
    assert {item['state'] for item in project_bus_connections(snapshot, now=now).values()} == {'connected'}
    if change == 'stale':
        snapshot['observed_at'] = (now - timedelta(seconds=30)).isoformat()
    elif change == 'future':
        snapshot['observed_at'] = (now + timedelta(seconds=6)).isoformat()
    elif change == 'duplicate':
        snapshot['agents'][1]['agent'] = 'codex'
    elif change == 'boolean_count':
        snapshot['agents'][0]['active_streams'] = True
    elif change == 'subscription':
        snapshot['agents'][0]['subscribed'] = True
    else:
        snapshot = {'session_id': 'private', 'runtime_id': 'private'}
    projected = project_bus_connections(snapshot, now=now)
    assert {item['state'] for item in projected.values()} == {'unavailable'}
    assert 'private' not in json.dumps(projected)


def test_topic_tracking_distinguishes_absence_provider_failure_and_missing_observer(tmp_path):
    for index, provider in enumerate([None, lambda _: {}, lambda _: {'invalid-private-field': 'private'}]):
        directory = tmp_path / str(index)
        directory.mkdir()
        app, _ = setup_app(directory, embedded=False, reachability_provider=provider)
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.get(BASE + '/topics/test/peers', headers=HEADERS)
            assert response.status_code == 200
            assert response.json()['tracking'] == ['not_configured', 'configured', 'unavailable'][index]
            assert {item['status'] for item in response.json()['items']} == {'unknown_stale'}


def test_configured_registry_failure_remains_unavailable_or_forbidden_not_unconfigured(tmp_path):
    import httpx
    from engineering_hub.registry_bridge import CanonicalRegistryBridge
    from engineering_hub.registry_client import RegistryClient
    for status in [403, 503]:
        directory = tmp_path / str(status)
        directory.mkdir()
        bridge = CanonicalRegistryBridge(RegistryClient('https://registry.test',
            transport=httpx.MockTransport(lambda _: httpx.Response(status, text='private-error'))))
        app, _ = setup_app(directory, embedded=False, registry_bridge=bridge)
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.get(BASE + '/topics/test/peers', headers=HEADERS)
            assert response.status_code == status
            assert 'not_configured' not in response.text and 'private-error' not in response.text


def test_mcp_overview_projects_same_embedded_source_without_readiness_claim(tmp_path):
    app, bus = setup_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        stream = client.portal.call(bus.state.push_publisher.open_presence_stream, 'private-session', 'codex')
        headers = {**HEADERS, 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}
        def rpc(method, params):
            response = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
            if response.headers.get('Mcp-Session-Id'):
                headers['Mcp-Session-Id'] = response.headers['Mcp-Session-Id']
            return response
        response = rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                     'clientInfo': {'name': 'connection-test', 'version': '1'}})
        assert response.status_code == 200
        response = rpc('tools/call', {'name': 'hub_overview', 'arguments': {}})
        result = response.json()['result']
        assert not result.get('isError'), result
        data = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        item = codex(data['agents'])
        assert item['connection']['state'] == 'connected' and item['chat_readiness'] == 'unverified'
        assert item['connection']['active_streams'] == 1
        assert 'private-session' not in response.text
        client.portal.call(bus.state.push_publisher.close_presence_stream, stream)
