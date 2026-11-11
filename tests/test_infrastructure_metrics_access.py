"""Actual request admission; no Prometheus or external identity providers."""
import base64
import hashlib
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from agent_message_bus.agent_installation_scope import InstallationScopeRepository, attach_scope_routes
from agent_message_bus.agent_sessions import AgentSessionError, AgentSessionRepository
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.auth import AuthSettings
from engineering_hub.infrastructure import CAPABILITIES, InfrastructurePrincipal, InfrastructureRegistry
from engineering_hub.infrastructure_api import attach_infrastructure_routes
from engineering_hub.infrastructure_discovery_metrics import DiscoveryMetricsReader
from engineering_hub.infrastructure_metrics_access import MetricsAccess, register_metrics_tool
from engineering_hub.infrastructure_service import InfrastructureService
from engineering_hub.private_gateway import browser_route_allowed
from engineering_hub.repository import HubError

OWNER = 'human:42'
PREFIX = '/api/hub/v1/agent-connections'


@pytest.fixture
def setup(tmp_path):
    sessions = AgentSessionRepository(tmp_path / 'sessions.sqlite3', OWNER)
    scopes = InstallationScopeRepository(sessions)
    registry = InfrastructureRegistry(sqlite3.connect(':memory:', check_same_thread=False),
        capabilities={InfrastructurePrincipal('human', OWNER): CAPABILITIES})
    service = InfrastructureService(registry)
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', allowed_ips=frozenset({'192.0.2.50'}))
    config = AgentSessionsConfig(tmp_path / 'sessions.sqlite3', OWNER, 'https://hub.test')

    def human(request, *, mutation=False):
        if request.headers.get('x-fixture-human') != OWNER:
            raise HTTPException(403)
        if mutation and request.headers.get('x-fixture-csrf') != 'valid':
            raise HTTPException(403)
        return OWNER

    access = MetricsAccess(service, auth, human, owner=OWNER, scopes=scopes, connection_config=config)
    yield sessions, scopes, service, access, human
    registry.close()


def request(headers=(), *, source='192.0.2.50', host='hub.test'):
    return Request({'type': 'http', 'method': 'POST', 'path': '/mcp',
                    'headers': [(b'host', host.encode()), *[(k.lower().encode(), v.encode()) for k, v in headers]],
                    'client': (source, 5000), 'query_string': b''})


def enroll(sessions, name):
    secret = 'ehi_' + base64.urlsafe_b64encode(hashlib.sha256(name.encode()).digest()).decode().rstrip('=')
    row = sessions.request_enrollment('codex', name, 'synthetic-pc', hashlib.sha256(secret.encode()).hexdigest())
    row = sessions.approve(OWNER, row['id'], row['revision'], until_revoked=True)
    return row, sessions.exchange(secret)['token']


def test_legacy_bearer_is_validated_without_cookie_fallback(setup):
    _, _, _, access, _ = setup
    access.authorize(request([('Authorization', 'Bearer synthetic-codex')]))
    for headers in [
        [('Authorization', 'Bearer wrong'), ('X-Fixture-Human', OWNER)],
        [('Authorization', 'Bearer synthetic-codex')] * 2,
    ]:
        with pytest.raises(HTTPException):
            access.authorize(request(headers))
    with pytest.raises(HTTPException):
        access.authorize(request([('Authorization', 'Bearer synthetic-codex')], source='192.0.2.51'))


def test_session_needs_classification_exact_host_and_admitted_source(setup):
    sessions, scopes, _, access, _ = setup
    row, token = enroll(sessions, 'internal')
    headers = [('Authorization', 'Bearer ' + token)]
    with pytest.raises(AgentSessionError, match='internal_installation_required'):
        access.authorize(request(headers))
    scopes.set(OWNER, row['id'], 0, 'internal')
    access.authorize(request(headers))
    for options in [{'host': 'public.other'}, {'source': '192.0.2.51'}]:
        with pytest.raises(HubError, match='forbidden'):
            access.authorize(request(headers, **options))
    scopes.set(OWNER, row['id'], 1, 'project_only')
    with pytest.raises(AgentSessionError, match='internal_installation_required'):
        access.authorize(request(headers))


def test_configured_owner_allowed_but_unbound_human_grant_is_insufficient(setup):
    _, _, service, access, human = setup
    access.authorize(request([('X-Fixture-Human', OWNER)]))
    legacy = MetricsAccess(service, access.auth, human)
    with pytest.raises(HubError, match='forbidden'):
        legacy.authorize(request([('X-Fixture-Human', OWNER)]))
    with pytest.raises(HTTPException):
        access.authorize(request([('X-Fixture-Human', 'human:43')]))


def test_owner_scope_api_requires_csrf_and_has_closed_gateway_routes(setup):
    sessions, scopes, _, _, human = setup
    row, _ = enroll(sessions, 'owner-api')
    app = FastAPI()
    attach_scope_routes(app, scopes, human)
    path = PREFIX + '/installations/' + row['id'] + '/scope'
    assert browser_route_allowed('GET', path) and browser_route_allowed('PUT', path)
    assert browser_route_allowed('PUT', PREFIX + '/installation-scopes')
    assert browser_route_allowed('GET', PREFIX + '/installation-scopes')
    assert not browser_route_allowed('POST', path)
    with TestClient(app) as client:
        headers = {'X-Fixture-Human': OWNER}
        assert client.get(path, headers=headers).json()['kind'] == 'unclassified'
        listing = client.get(PREFIX + '/installation-scopes', headers=headers)
        assert listing.status_code == 200 and listing.json()['installations'][0]['revision'] == 0
        assert client.put(path, headers=headers, json={'expected_revision': 0, 'kind': 'internal'}).status_code == 403
        headers['X-Fixture-CSRF'] = 'valid'
        answer = client.put(PREFIX + '/installation-scopes', headers=headers,
            json={'installations': [{'installation_id': row['id'], 'expected_revision': 0}], 'kind': 'internal'})
        assert answer.status_code == 200
        assert answer.json()['installations'][0]['internal_effective']
        assert client.get(path, headers={**headers, 'Authorization': 'Bearer synthetic-codex'}).status_code == 403


@pytest.mark.asyncio
async def test_http_and_registered_mcp_tool_return_same_envelope(setup):
    _, _, service, access, _ = setup
    reader = DiscoveryMetricsReader(service, None, authorize=access.authorize, inventory=access.inventory)
    app = FastAPI()
    def metrics_identity(req):
        access.authorize(req)
        return req
    attach_infrastructure_routes(app, service, lambda req: OWNER, metrics=reader, metrics_identity=metrics_identity)
    with TestClient(app) as client:
        response = client.get('/api/hub/v1/infrastructure/metrics', headers={'X-Fixture-Human': OWNER})
        history = '/api/hub/v1/infrastructure/metrics/history'
        owner = {'X-Fixture-Human': OWNER}
        key = 'host:' + 'a' * 64
        assert client.get(history, params={'host': key, 'range': '1h'}, headers=owner).json()['source'] == {'status': 'not_configured'}
        assert client.get(history, params={'host': key, 'range': '1h'}, headers={'X-Fixture-Human': 'human:other'}).status_code == 403
        for extra in ({'host': key}, {'host': key, 'range': '1h', 'query': 'up'}):
            with pytest.raises(HubError, match='invalid_infrastructure_filter'):
                client.get(history, params=extra, headers=owner)
    assert response.status_code == 200
    mcp = FastMCP('synthetic-metrics')
    register_metrics_tool(mcp, reader)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'hub_infrastructure_metrics')
    context = SimpleNamespace(request_context=SimpleNamespace(request=request([('Authorization', 'Bearer synthetic-codex')])))
    payload = await tool.fn(context)
    assert payload['source'] == response.json()['source'] and payload['devices'] == response.json()['devices']
    assert payload['schema_version'] == response.json()['schema_version'] == 3
    with pytest.raises(ToolError, match='forbidden'):
        await tool.fn(SimpleNamespace(request_context=SimpleNamespace(request=request([('Authorization', 'Bearer wrong')]))))
