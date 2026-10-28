"""Public bootstrap bytes do not create identity or relax session authorization."""
import hashlib
from importlib.resources import files
from io import BytesIO

from fastapi.testclient import TestClient
import httpx
from mcp.server.transport_security import TransportSecuritySettings
import pytest

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus import agent_sessions_api
from engineering_hub.app import Settings, create_app as create_hub
from engineering_hub.auth import AuthConfig
from engineering_hub.frontends import HubFrontends


PREFIX = '/api/agent-sessions/v1'
MANIFEST = PREFIX + '/client/manifest'
HELPER = PREFIX + '/client/agent_session_client.py'
OWNER = 'human:42'


def application(tmp_path, *, enabled=True, allowed_ips=frozenset()):
    config = AgentSessionsConfig(tmp_path / 'sessions.sqlite3', OWNER, 'http://127.0.0.1') if enabled else None
    return create_app(db_path=tmp_path / 'messages.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=AuthSettings('download-hermes', 'download-codex', allowed_ips=allowed_ips),
        startup_cleanup=False, push_events_operational=False, agent_sessions_config=config)


def test_public_manifest_download_and_head_match_packaged_bytes_without_enrollment(tmp_path):
    app = application(tmp_path)
    repository = app.state.agent_sessions
    expected = files('agent_message_bus').joinpath('agent_session_client.py').read_bytes()
    assert repository.list_installations(OWNER) == {'installations': []}
    assert repository.audit(OWNER)['events'] == []
    with TestClient(app, base_url='http://127.0.0.1') as client:
        manifest = client.get(MANIFEST)
        assert manifest.status_code == 200
        assert manifest.json() == {'schema_version': 1, 'helper_version': '0.6', 'url': HELPER,
                                   'bytes': len(expected), 'sha256': hashlib.sha256(expected).hexdigest()}
        download = client.get(manifest.json()['url'])
        assert download.status_code == 200 and download.content == expected
        assert download.headers['content-type'] == 'application/octet-stream'
        assert download.headers['content-disposition'] == 'attachment; filename="agent_session_client.py"'
        for path, get in ((MANIFEST, manifest), (HELPER, download)):
            head = client.head(path)
            assert head.status_code == 200 and head.content == b''
            assert int(head.headers['content-length']) == len(get.content)
            assert head.headers['content-type'] == get.headers['content-type']
            for response in (get, head):
                assert response.headers['cache-control'] == 'no-store'
                assert response.headers['x-content-type-options'] == 'nosniff'
                assert 'set-cookie' not in response.headers
        # A caller cannot select a different local file with query parameters.
        assert client.get(HELPER + '?path=../../settings.json').content == expected
    assert repository.list_installations(OWNER) == {'installations': []}
    assert repository.audit(OWNER)['events'] == []


@pytest.mark.parametrize('path', [MANIFEST, HELPER])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_download_absent_when_sessions_disabled(tmp_path, path, method):
    app = application(tmp_path, enabled=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.request(method, path).status_code == 404
    assert not (tmp_path / 'sessions.sqlite3').exists()


@pytest.mark.parametrize('path', [MANIFEST, HELPER])
def test_public_download_retains_host_and_actual_socket_ip_guards(tmp_path, path):
    app = application(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.get(path, headers={'Host': 'other.test'}).status_code == 403
    denied = application(tmp_path / 'denied', allowed_ips=frozenset({'192.0.2.1'}))
    with TestClient(denied, base_url='http://127.0.0.1') as client:
        assert client.get(path, headers={'X-Forwarded-For': '192.0.2.1'}).status_code == 403


@pytest.mark.parametrize('suffix', ['/client/manifest/', '/client/other.py', '/identity', '/messages', '/enrollments'])
def test_unauthenticated_exemption_does_not_cover_neighbors(tmp_path, suffix):
    with TestClient(application(tmp_path), base_url='http://127.0.0.1') as client:
        assert client.get(PREFIX + suffix, follow_redirects=False).status_code == 401


@pytest.mark.parametrize('method', ['POST', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('path', [MANIFEST, HELPER])
def test_asset_exemption_is_read_only(tmp_path, method, path):
    with TestClient(application(tmp_path), base_url='http://127.0.0.1') as client:
        assert client.request(method, path, json={}).status_code == 401


@pytest.mark.parametrize('suffix', ['/client/%6danifest', '/client/%61gent_session_client.py',
                                  '/client/%2e%2e/enrollments', '/client//manifest'])
def test_encoded_and_ambiguous_asset_paths_still_refused(tmp_path, suffix):
    with TestClient(application(tmp_path), base_url='http://127.0.0.1') as client:
        assert client.get(PREFIX + suffix, follow_redirects=False).status_code == 403


@pytest.mark.parametrize('payload', [b'', b'x' * 65537])
def test_missing_or_oversized_package_asset_fails_closed(tmp_path, monkeypatch, payload):
    class FixedAsset:
        def joinpath(self, name):
            assert name == 'agent_session_client.py'
            return self
        def open(self, mode):
            assert mode == 'rb'
            return BytesIO(payload)
    app = application(tmp_path)
    monkeypatch.setattr(agent_sessions_api, 'files', lambda package: FixedAsset() if package == 'agent_message_bus' else None)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get(HELPER)
        assert response.status_code == 503
        assert response.json() == {'detail': {'code': 'agent_client_unavailable'}}
        assert response.headers['cache-control'] == 'no-store'


def paired_application(tmp_path, *, enabled=True):
    origin = 'https://hub.test'
    auth = AuthSettings('paired-download-hermes', 'paired-download-codex',
                        allowed_ips=frozenset({'127.0.0.1'}))
    config = AgentSessionsConfig(tmp_path / 'sessions.sqlite3', OWNER, origin) if enabled else None
    embedded = create_app(db_path=tmp_path / 'messages.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False,
        push_events_operational=False, agent_sessions_config=config,
        transport_security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=['hub.test'], allowed_origins=[origin]))
    human = AuthConfig(origin, human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://hub-test.cloudflareaccess.com', cloudflare_audience='test',
        cloudflare_allowed_emails=('owner@example.test',))
    core = create_hub(Settings(tmp_path / 'hub.sqlite3', auth, human,
                               agent_allowed_hosts=('hub.test',)), bus_app=embedded)
    return HubFrontends(core), embedded


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('path', [MANIFEST, HELPER])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_paired_frontdoor_downloads_and_browser_denial(tmp_path, enabled, path, method):
    pair, embedded = paired_application(tmp_path, enabled=enabled)
    async with pair.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents,
                client=('127.0.0.1', 43123)), base_url='https://hub.test') as agents:
            response = await agents.request(method, path)
            assert response.status_code == (200 if enabled else 404)
            if enabled:
                assert response.headers['cache-control'] == 'no-store'
                assert response.headers['x-content-type-options'] == 'nosniff'
                if method == 'HEAD':
                    assert response.content == b'' and int(response.headers['content-length']) > 0
                elif path == MANIFEST:
                    helper = await agents.get(response.json()['url'])
                    assert helper.status_code == 200
                    assert response.json()['sha256'] == hashlib.sha256(helper.content).hexdigest()
                    assert response.json()['bytes'] == len(helper.content)
                else:
                    assert response.content == files('agent_message_bus').joinpath('agent_session_client.py').read_bytes()
            # Public asset reads do not open the rest of the session namespace.
            assert (await agents.get(PREFIX + '/identity')).status_code == (401 if enabled else 404)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.browser,
                client=('127.0.0.1', 43124)), base_url='https://hub.test') as browser:
            assert (await browser.request(method, path)).status_code == 403
            assert (await browser.request(method, path,
                headers={'Authorization': 'Bearer paired-download-codex'})).status_code == 403
        if enabled:
            assert embedded.state.agent_sessions.list_installations(OWNER) == {'installations': []}
            assert embedded.state.agent_sessions.audit(OWNER)['events'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('path', [MANIFEST, HELPER])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_paired_downloads_keep_host_and_actual_socket_ip_guards(tmp_path, path, method):
    pair, embedded = paired_application(tmp_path)
    async with pair.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents,
                client=('127.0.0.1', 43125)), base_url='https://hub.test') as allowed:
            assert (await allowed.request(method, path, headers={'Host': 'other.test'})).status_code == 403
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents,
                client=('192.0.2.9', 43126)), base_url='https://hub.test') as denied:
            assert (await denied.request(method, path,
                headers={'X-Forwarded-For': '127.0.0.1', 'X-Real-IP': '127.0.0.1'})).status_code == 403
        assert embedded.state.agent_sessions.list_installations(OWNER) == {'installations': []}
