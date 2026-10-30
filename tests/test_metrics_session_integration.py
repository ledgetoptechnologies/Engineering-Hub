"""Real unified ASGI/MCP session transport, with mock Prometheus only."""
import json
from types import SimpleNamespace

import httpx

from engineering_hub.infrastructure_prometheus import PrometheusOrigin, TARGET_PATH
from tests.test_session_hub_bridge import unified, sdk, data, pair


def test_owner_bulk_classification_and_mcp_read_isolation(unified):
    client, app, bus, csrf = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    first, first_token, _ = pair(env, 'internal-first', caps=())
    second, second_token, _ = pair(env, 'internal-second', caps=())
    first_call, headers = sdk(client, first_token)
    second_call, _ = sdk(client, second_token)
    # Default routing is set without a settings file. Exercise the explicit
    # no-origin reader state below without contacting a real private service.
    assert app.state.infrastructure.metrics.origin.url == 'http://192.168.50.80:30104'
    app.state.infrastructure.metrics.origin = None
    assert client.get('/api/hub/v1/overview').json()['capabilities']['infrastructure']
    assert first_call('hub_infrastructure_metrics', {})['isError']
    # Per-PC agents intentionally use the existing private MCP surface, not new
    # Hub REST access or a human-cookie fallback on an agent-bearing request.
    blocked = client.get('/api/hub/v1/infrastructure/metrics', headers=headers)
    assert blocked.status_code == 403
    assert not {'devices', 'source'} & set(blocked.json())
    path = '/api/hub/v1/agent-connections/installation-scopes'
    body = {'kind': 'internal', 'installations': [
        {'installation_id': first, 'expected_revision': 0},
        {'installation_id': second, 'expected_revision': 0},
    ]}
    assert client.put(path, json=body).status_code == 403
    assert client.put(path, headers=csrf, json=body).status_code == 200
    for call in (first_call, second_call):
        result = data(call('hub_infrastructure_metrics', {}))
        assert result['schema_version'] == 2
        assert result['source']['status'] == 'not_configured' and result['devices'] == []
    scope = '/api/hub/v1/agent-connections/installations/' + first + '/scope'
    assert client.put(scope, headers=csrf, json={'kind': 'project_only', 'expected_revision': 1}).status_code == 200
    assert first_call('hub_infrastructure_metrics', {})['isError']
    assert not second_call('hub_infrastructure_metrics', {}).get('isError')
    # Classification does not confer project grants or legacy family authority.
    assert second_call('hub_project_list', {})['isError']


def test_mcp_classification_revoked_during_fetch_returns_no_metrics(unified):
    client, app, bus, csrf = unified
    env = SimpleNamespace(sessions=bus.state.agent_sessions, bridge=app.state.session_hub_bridge)
    identity, token, _ = pair(env, 'during-fetch', caps=())
    scope = '/api/hub/v1/agent-connections/installations/' + identity + '/scope'
    assert client.put(scope, headers=csrf, json={'kind': 'internal', 'expected_revision': 0}).status_code == 200
    reader = app.state.infrastructure.metrics
    reader.origin = PrometheusOrigin('192.0.2.1', 9090)
    observed = []
    def respond(request):
        if not observed:
            app.state.installation_scopes.set('human:42', identity, 1, 'project_only')
        observed.append(str(request.url.host))
        payload = ({'status': 'success', 'data': {'activeTargets': []}}
                   if request.url.raw_path.decode() == TARGET_PATH else
                   {'status': 'success', 'data': {'resultType': 'vector', 'result': []}})
        return httpx.Response(200, headers={'content-type': 'application/json'},
                              stream=httpx.ByteStream(json.dumps(payload).encode()))
    reader._transport = httpx.MockTransport(respond)
    call, _ = sdk(client, token)
    result = call('hub_infrastructure_metrics', {})
    assert result['isError'] and 'structuredContent' not in result
    assert observed and set(observed) == {'192.0.2.1'}
