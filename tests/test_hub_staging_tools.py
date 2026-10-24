"""Actual MCP transport preserves identity and closed staging input contracts."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.mcp import build_hub_mcp, HubMCPAuthMiddleware
from engineering_hub.repository import HubRepository, HubError


@pytest.fixture
def fixture(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    calls = []

    async def status(actor):
        calls.append(('status', actor))
        return {'configured': True, 'reachable': False}

    async def submit(actor, data):
        calls.append(('submit', actor, data))
        raise HubError('staging_forbidden', 403)

    controller = SimpleNamespace(status=status, submit=submit)
    auth = AuthSettings('fixture-hermes', 'fixture-codex', claude_token='fixture-claude')
    mcp = build_hub_mcp(repo, auth, staging_controller=controller)
    child = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount('/mcp', HubMCPAuthMiddleware(child, auth))
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        yield client, calls


def rpc(client, method, params=None, token='fixture-codex'):
    return client.post('/mcp/', headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})


def call(client, name, arguments):
    response = rpc(client, 'tools/call', {'name': name, 'arguments': arguments})
    assert response.status_code == 200
    return response.json()['result']


def test_staging_tools_are_closed_and_use_authenticated_agent(fixture):
    client, calls = fixture
    tools = rpc(client, 'tools/list').json()['result']['tools']
    names = {tool['name'] for tool in tools}
    assert {'hub_staging_service_status', 'hub_staging_build_submit',
            'hub_staging_build_get', 'hub_staging_resources',
            'hub_staging_resource_keep', 'hub_staging_cleanup_plan',
            'hub_staging_cleanup_apply'} <= names
    assert not any('shell' in name or 'deploy' in name for name in names)
    assert rpc(client, 'tools/list', token='bad').status_code == 401
    result = call(client, 'hub_staging_service_status', {})
    assert not result.get('isError') and calls == [('status', 'codex')]
    for tool in tools:
        assert 'actor' not in tool['inputSchema'].get('properties', {})


def test_submit_validation_never_exposes_source_or_trusts_caller_authority(fixture):
    client, calls = fixture
    payload = dict(request_id=str(uuid.uuid4()), project='engineering-hub', recipe='static',
                   source_commit='a' * 40, source_digest='b' * 64,
                   source_zip_base64='private-source-fixture')
    for extra in ({'actor': 'claude'}, {'command': 'sh'}, {'image': 'latest'},
                  {'resource_ids': {}}, {'worker_url': 'http://untrusted'}):
        result = call(client, 'hub_staging_build_submit', {'payload': dict(payload, **extra)})
        assert result['isError']
        assert 'private-source-fixture' not in json.dumps(result)
        assert not calls
    result = call(client, 'hub_staging_build_submit', {'payload': payload})
    assert result['isError'] and 'staging_forbidden' in json.dumps(result)
    assert calls[0][0:2] == ('submit', 'codex')
