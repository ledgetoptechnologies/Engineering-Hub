"""Real native MCP planning calls share the existing knowledge store."""
from fastapi.testclient import TestClient

from tests.test_hub_unified_mcp import composed, initialize, content, result, rpc, AUTH, BASE
from tests.test_hub_mcp import hub, call
from engineering_hub.private_gateway import browser_route_allowed

ITEM = {'scope': 'topic', 'project': 'treedar', 'topic': 'TreeDAR:research-v1',
    'kind': 'plan', 'item_id': 'benchmark', 'title': 'Benchmark proposal',
    'content': 'Proposed steps, evidence and open questions.', 'expected_version': 0}


def test_browser_gateway_exposes_only_the_reviewed_planning_surface():
    assert browser_route_allowed('GET', '/api/hub/v1/planning')
    assert browser_route_allowed('GET', '/api/hub/v1/planning/document')
    assert browser_route_allowed('POST', '/api/hub/v1/planning')
    assert not browser_route_allowed('DELETE', '/api/hub/v1/planning')
    assert not browser_route_allowed('POST', '/api/hub/v1/planning/execute')


def test_planning_mcp_store_identity_scope_cas_and_no_work(composed):
    core, bus, _ = composed
    with TestClient(core, base_url=BASE) as client:
        session = initialize(client)
        tools = result(rpc(client, 'tools/list', session=session))['tools']
        tool = next(item for item in tools if item['name'] == 'hub_planning_put')
        assert 'PlanningWrite' in str(tool['inputSchema'])
        saved = content(client, session, 'hub_planning_put', {'payload': ITEM})
        assert saved['version'] == 1 and saved['created_by'] == 'codex'
        assert saved['advisory'] is True and saved['execution_authorization'] is False
        address = {key: ITEM[key] for key in ('scope', 'project', 'topic', 'kind', 'item_id')}
        assert content(client, session, 'hub_planning_get', address) == saved
        raw = content(client, session, 'knowledge_get', {'project': ITEM['project'], 'path': saved['path']})
        assert 'Proposed steps' in raw['content']
        listed = content(client, session, 'hub_planning_list', {
            'scope': 'topic', 'project': 'treedar', 'topic': ITEM['topic']})
        assert [item['item_id'] for item in listed['items']] == ['benchmark']
        assert 'content' not in listed['items'][0]
        conflict = result(rpc(client, 'tools/call', {'name': 'hub_planning_put', 'arguments': {'payload': ITEM}}, session=session))
        assert conflict['isError'] is True
        assert 'planning_version_conflict' in conflict['content'][0]['text']
        eh = content(client, session, 'hub_planning_put', {'payload': {**ITEM, 'scope': 'hub_improvements',
            'project': 'engineering-hub', 'topic': None, 'kind': 'improvement', 'item_id': 'better-status'}})
        assert eh['path'] == 'planning/hub-improvements/improvement/better-status.json'
        assert content(client, session, 'hub_planning_list', {
            'scope': 'topic', 'project': 'engineering-hub', 'topic': ITEM['topic']})['items'] == []
        assert core.state.repository.list_tasks() == []
        assert core.state.repository.list_decisions() == []
        assert core.state.repository.list_runs() == []


def test_planning_tool_unconfigured_fails_closed_without_parallel_store(hub):
    client, repo = hub
    failed = call(client, 'hub_planning_put', {'payload': ITEM})
    assert failed['isError'] is True
    assert 'planning_unavailable' in failed['content'][0]['text']
    with repo._connection() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='knowledge'").fetchone() is None


def test_planning_tools_reject_spoofing_and_boolean_version(composed):
    core, _, _ = composed
    with TestClient(core, base_url=BASE) as client:
        session = initialize(client)
        for patch in ({'author': 'hermes'}, {'expected_version': True},
                      {'scope': 'hub_improvements', 'project': 'treedar', 'topic': None}):
            response = result(rpc(client, 'tools/call', {'name': 'hub_planning_put',
                'arguments': {'payload': {**ITEM, **patch}}}, session=session))
            assert response['isError'] is True
        response = rpc(client, 'tools/call', {'name': 'hub_planning_list',
            'arguments': {'scope': 'hub_improvements', 'project': 'engineering-hub'}},
            token=None, session=session)
        assert response.status_code == 401
