import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from engineering_hub.repository import HubError
from engineering_hub.staging_client import StagingConnectionConfig
from engineering_hub.staging_observer import StagingObserver, compose_observer


@pytest.mark.asyncio
async def test_observer_client_authorization_fresh_stale_and_unavailable(tmp_path):
    token = tmp_path / 'token'
    token.write_text('fake-test-token-only')
    if os.name == 'posix':
        token.chmod(0o600)
    config = StagingConnectionConfig(origin='http://127.0.0.1', credential_file=token,
                                     admin_actors=frozenset({'human:1'}), enabled=True)
    now = datetime.now(timezone.utc)
    value = {'schema': 'hub-staging-runtime.v1', 'observed_at': now.isoformat(),
             'sources': [{'id': 'worker', 'state': 'not_configured', 'observed_at': None},
                         {'id': 'host', 'state': 'stale', 'observed_at': (now - timedelta(seconds=100)).isoformat()}],
             'containers': [], 'services': []}
    seen = []
    def response(request):
        seen.append(request)
        return httpx.Response(200, json=value)
    observer = StagingObserver(config, test_transport=httpx.MockTransport(response))
    try:
        with pytest.raises(HubError):
            await observer.runtime('human:2')
        assert not seen
        result = await observer.runtime('human:1')
        assert result['snapshot']['sources'][1]['state'] == 'stale'
        assert seen[0].method == 'GET' and seen[0].url.path == '/v1/runtime'
        value['observed_at'] = (now - timedelta(seconds=100)).isoformat()
        assert (await observer.runtime('human:1'))['state'] == 'unavailable'
    finally:
        await observer.close()


def test_observer_optional_config_fails_closed(tmp_path):
    path = tmp_path / 'staging-observer.json'
    assert compose_observer(path) is None
    path.write_text(json.dumps({'schema': 1, 'enabled': True, 'origin': 'http://127.0.0.1',
        'credential_file': str(tmp_path / 'token'), 'ca_file': None, 'admin_actors': ['human:1']}))
    assert compose_observer(path) is False


def test_disabled_invalid_and_unreadable_are_distinct_from_missing(tmp_path, monkeypatch):
    path = tmp_path / 'staging-observer.json'
    path.write_text(json.dumps({'schema': 1, 'enabled': False, 'origin': 'https://192.168.50.90',
        'credential_file': str(tmp_path / 'token'), 'ca_file': str(tmp_path / 'ca'), 'admin_actors': ['human:2']}))
    assert compose_observer(path) is False
    path.write_text('{')
    assert compose_observer(path) is False
    def denied(*args, **kwargs):
        raise PermissionError()
    monkeypatch.setattr('engineering_hub.staging_observer._read_file', denied)
    assert compose_observer(path) is False


def test_invalid_observer_config_lifespan_shutdown(tmp_path):
    from fastapi.testclient import TestClient
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig

    (tmp_path / 'staging-observer.json').write_text('{')
    settings = Settings(tmp_path / 'hub.sqlite3',
        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
        AuthConfig('https://hub.test', beau_github_id=42))
    with TestClient(create_app(settings), base_url=settings.auth_config.origin) as client:
        assert client.get('/health').status_code == 200
    # The context manager must exit without trying to close the False sentinel.

@pytest.mark.asyncio
async def test_real_loopback_observer_to_hub_client(tmp_path, monkeypatch):
    """Exercise the actual HTTP boundary without provisioning a staging host."""
    import ipaddress
    import threading
    from hub_staging_service import observer as server_observer
    from hub_staging_service.__main__ import BoundedServer
    from hub_staging_service.runtime_inventory import _empty, _source, _timestamp

    token = tmp_path / 'token'
    token.write_text('fake-test-token-only')
    if os.name == 'posix':
        token.chmod(0o600)
    now = datetime.now(timezone.utc)
    host = _empty(now)
    host['sources'] = [_source('host', 'observed', _timestamp(now))]
    monkeypatch.setattr(server_observer, '_read_host', lambda path: host)
    server = BoundedServer(('127.0.0.1', 0), server_observer.handler(
        '/unused', 'fake-test-token-only', {ipaddress.ip_address('127.0.0.1')}))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    config = StagingConnectionConfig(origin=f'http://127.0.0.1:{server.server_address[1]}',
        credential_file=token, admin_actors=frozenset({'human:1'}), enabled=True)
    client = None
    try:
        if os.name == 'nt':
            from engineering_hub import staging_client
            original_read = staging_client._read_file
            monkeypatch.setattr(staging_client, '_read_file',
                lambda path, limit, **kwargs: original_read(path, limit, synthetic_windows=True,
                    **{k: v for k, v in kwargs.items() if k != 'synthetic_windows'}))
        client = StagingObserver(config)
        result = await client.runtime('human:1')
        assert result['state'] == 'ready'
        assert [item['state'] for item in result['snapshot']['sources']] == ['not_configured', 'observed']
        host['observed_at'] = _timestamp(now - timedelta(seconds=100))
        host['sources'][0]['observed_at'] = host['observed_at']
        result = await client.runtime('human:1')
        assert result['snapshot']['sources'][1]['state'] == 'stale'
        host['observed_at'] = _timestamp(now + timedelta(seconds=100))
        host['sources'][0]['observed_at'] = host['observed_at']
        result = await client.runtime('human:1')
        assert result['snapshot']['sources'][1]['state'] == 'unavailable'
    finally:
        if client is not None:
            await client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
