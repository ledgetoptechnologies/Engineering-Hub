from contextlib import asynccontextmanager
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.mcp import HubMCPAuthMiddleware, build_hub_mcp
from engineering_hub.repository import HubRepository


@pytest.fixture
def hub(tmp_path):
    repo = HubRepository(tmp_path / "hub.db")
    auth = AuthSettings("fixture-hermes", "fixture-codex", claude_token="fixture-claude")
    mcp = build_hub_mcp(repo, auth)
    child = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", HubMCPAuthMiddleware(child, auth))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client, repo


def rpc(client, method, params=None, token="fixture-codex"):
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def call(client, name, args=None, token="fixture-codex"):
    response = rpc(client, "tools/call", {"name": name, "arguments": args or {}}, token)
    assert response.status_code == 200
    result = response.json()["result"]
    if not result.get("isError") and "structuredContent" not in result:
        result["structuredContent"] = json.loads(result["content"][0]["text"])
    return result


def test_real_initialize_discovery_and_auth_on_every_request(hub):
    client, _ = hub
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "hub-test", "version": "1"}}
    assert rpc(client, "initialize", init).status_code == 200
    for method in ("initialize", "tools/list", "tools/call"):
        for bad in (None, "invalid-token"):
            assert rpc(client, method, init if method == "initialize" else {}, bad).status_code == 401
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "hub_overview", "hub_project_tasks", "hub_tracking_task_propose", "hub_tracking_task_update", "hub_tracking_task_list", "hub_tracking_task_review", "hub_tracking_task_history", "hub_tracking_task_compact", "hub_task_get", "hub_task_claim", "hub_task_candidate",
        "hub_task_review", "hub_decision_request", "hub_decision_get", "hub_run_list",
        "hub_topic_peers", "hub_topic_preflight", "hub_staging_task_propose",
        "hub_planning_list", "hub_planning_get", "hub_planning_put", "hub_entity_withdraw"}
    for tool in tools:
        assert not {"actor", "reviewer", "identity", "github_id"} & set(tool["inputSchema"].get("properties", {}))


def test_real_transport_task_lifecycle_uses_token_identity(hub):
    client, repo = hub
    task = repo.create_task("human:123", "3d-viewer", "Assigned fixture", "request-1")
    assert call(client, "hub_overview")["structuredContent"]["tasks"][0]["id"] == task["id"]
    claim = call(client, "hub_task_claim", {"task_id": task["id"], "expected_revision": 1})
    assert claim["structuredContent"]["owner"] == "codex"
    candidate = {"task_id": task["id"], "expected_revision": 2, "commit": "a" * 40}
    assert call(client, "hub_task_candidate", candidate)["structuredContent"]["status"] == "in_review"
    review = {"task_id": task["id"], "expected_revision": 3, "commit": "a" * 40, "verdict": "approve"}
    assert call(client, "hub_task_review", review)["isError"] is True
    accepted = call(client, "hub_task_review", review, "fixture-claude")
    assert accepted["structuredContent"]["review"]["actor"] == "claude"
    assert accepted["structuredContent"]["status"] == "verified"
    assert call(client, "hub_task_get", {"task_id": task["id"]})["structuredContent"]["status"] == "verified"


def test_project_tasks_refresh_current_state_and_disclose_truncation(hub):
    client, repo = hub
    first = repo.create_task('human:123', '3d-viewer', 'First task', 'continuity-1')
    second = repo.create_task('human:123', '3d-viewer', 'Second task', 'continuity-2')
    repo.claim_task('codex', first['id'], first['revision'])
    result = call(client, 'hub_project_tasks', {'project': '3d-viewer', 'limit': 1})['structuredContent']
    assert result['truncated'] is True and result['next_after'] == result['tasks'][0]['id']
    page = call(client, 'hub_project_tasks', {'project': '3d-viewer', 'limit': 1,
        'after_task_id': result['next_after']})['structuredContent']
    assert page['truncated'] is False and {result['tasks'][0]['id'], page['tasks'][0]['id']} == {first['id'], second['id']}
    assert result['paused'] is False
    repo.set_candidate('codex', first['id'], 2, 'a' * 40)
    refreshed = call(client, 'hub_project_tasks', {'project': '3d-viewer'})['structuredContent']
    assert refreshed['truncated'] is False
    assert {t['id']: t['status'] for t in refreshed['tasks']} == {first['id']: 'in_review', second['id']: 'assigned'}
    assert call(client, 'hub_project_tasks', {'project': '3d-viewer', 'limit': 0})['isError']
    assert call(client, 'hub_project_tasks', {'project': '3d-viewer', 'after_task_id': 'invalid'})['isError']


def test_tracking_task_idempotency_evidence_and_no_execution_authority(hub):
    client, repo = hub
    args = {'project': '3d-viewer', 'title': 'Track work', 'client_request_id': 'tracking-1'}
    item = call(client, 'hub_tracking_task_propose', args)['structuredContent']
    assert item['status'] == 'open' and item['execution_authorization'] is False
    assert call(client, 'hub_tracking_task_propose', args)['structuredContent']['id'] == item['id']
    assert call(client, 'hub_tracking_task_propose', {**args, 'title': 'Different'})['isError']
    assert repo.task_allows_environment(item['id'], 'staging') is False
    assert call(client, 'hub_task_get', {'task_id': item['id']})['isError']
    update = {'task_id': item['id'], 'expected_revision': 1, 'client_request_id': 'update-1',
              'progress': 'Evidence checked', 'evidence_links': [], 'completion_claim': True}
    assert call(client, 'hub_tracking_task_update', update)['isError']
    update['evidence_links'] = [{'label': 'Evidence', 'url': 'https://example.com/evidence'}]
    claimed = call(client, 'hub_tracking_task_update', update)['structuredContent']
    assert claimed['status'] == 'completion_claimed' and claimed['updates'][0]['actor'] == 'codex'
    assert call(client, 'hub_tracking_task_update', update)['structuredContent']['revision'] == 2
    assert call(client, 'hub_tracking_task_update', {**update, 'client_request_id': 'update-2'})['isError']
    listed = call(client, 'hub_tracking_task_list', {'project': '3d-viewer'})['structuredContent']
    assert [value['id'] for value in listed['tasks']] == [item['id']]
    review = {'task_id': item['id'], 'expected_revision': 2, 'client_request_id': 'review-1',
              'verdict': 'verify', 'note': 'Inspected evidence', 'evidence_links': update['evidence_links']}
    assert call(client, 'hub_tracking_task_review', review)['isError']
    assert call(client, 'hub_tracking_task_review', {**review, 'evidence_links': [{'label':'Other','url':'https://example.com/other'}]}, 'fixture-claude')['isError']
    verified = call(client, 'hub_tracking_task_review', review, 'fixture-claude')['structuredContent']
    assert verified['status'] == 'completion_verified' and verified['execution_authorization'] is False
    assert call(client, 'hub_tracking_task_list', {'project': '3d-viewer', 'unfinished': True})['structuredContent']['tasks'] == []
    assert call(client, 'hub_tracking_task_list', {'project': '3d-viewer'})['structuredContent']['tasks'][0]['id'] == item['id']
    assert call(client, 'hub_tracking_task_list', {'project': '3d-viewer', 'unfinished': 'true'})['isError']
    assert verified['reviews'][0]['claim_revision'] == 2
    assert call(client, 'hub_tracking_task_review', review, 'fixture-claude')['structuredContent']['revision'] == 3
    assert call(client, 'hub_tracking_task_update', {**update, 'expected_revision': 3, 'client_request_id': 'after-review'})['isError']
    reopen = {**review, 'expected_revision': 3, 'client_request_id': 'reopen-1', 'verdict': 'reopen', 'evidence_links': []}
    assert call(client, 'hub_tracking_task_review', reopen, 'fixture-claude')['structuredContent']['status'] == 'open'
    assert call(client, 'hub_tracking_task_list', {'project': '3d-viewer', 'unfinished': True})['structuredContent']['tasks'][0]['id'] == item['id']


def test_decisions_are_requests_not_approval_or_dispatch(hub):
    client, repo = hub
    args = {"project": "3d-viewer", "title": "Choose test layout", "options": ["First", "Second"]}
    requested = call(client, "hub_decision_request", args, "fixture-hermes")
    assert requested["structuredContent"]["status"] == "open"
    protected = call(client, "hub_decision_request", {**args, "protected": True})
    assert protected["structuredContent"]["status"] == "open"
    assert protected["structuredContent"]["execution_authorization"] is False
    assert len(repo.list_decisions()) == 2
    assert call(client, "hub_run_list")["structuredContent"] == {"items": []}


def test_errors_do_not_reflect_peer_input(hub):
    client, _ = hub
    sentinel = "private-peer-text"
    response = call(client, "hub_task_get", {"task_id": sentinel})
    assert response["isError"] is True
    assert sentinel not in str(response)
    assert "not_found" in str(response)



def _seed_run(repo, project, status):
    """A stored run row for presentation filtering; runs are otherwise created only by a reviewed pilot."""
    import json, uuid
    run = {"id": str(uuid.uuid4()), "project": project, "status": status, "revision": 1}
    with repo._transaction() as db:
        db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)", (run["id"], "run", json.dumps(run)))
    return run


def test_overview_filters_keep_active_items_and_count_the_rest(hub):
    client, repo = hub
    tasks = {
        "viewer_open": repo.create_task("human:123", "3d-viewer", "Open", "t1"),
        "viewer_gone": repo.create_task("human:123", "3d-viewer", "Withdrawn", "t2"),
        "treedar_open": repo.create_task("human:123", "treedar", "Other project", "t3"),
    }
    repo.withdraw("human:123", "task", tasks["viewer_gone"]["id"], tasks["viewer_gone"]["revision"], "fixture")
    decisions = {
        "viewer_open": repo.create_decision("human:123", "3d-viewer", "Open choice", ["A", "B"]),
        "viewer_gone": repo.create_decision("human:123", "3d-viewer", "Withdrawn choice", ["A", "B"]),
        "treedar_open": repo.create_decision("human:123", "treedar", "Other choice", ["A", "B"]),
    }
    gone = decisions["viewer_gone"]
    repo.withdraw("human:123", "decision", gone["id"], gone["revision"], "fixture")
    runs = {"viewer_held": _seed_run(repo, "3d-viewer", "held"),
            "viewer_done": _seed_run(repo, "3d-viewer", "succeeded"),
            "treedar_running": _seed_run(repo, "treedar", "running")}

    def ids(result, key):
        return {item["id"] for item in result[key]}

    full = call(client, "hub_overview")["structuredContent"]
    assert "omitted" not in full
    assert (len(full["tasks"]), len(full["decisions"]), len(full["runs"])) == (3, 3, 3)

    scoped = call(client, "hub_overview", {"project": "3d-viewer"})["structuredContent"]
    assert ids(scoped, "tasks") == {tasks["viewer_open"]["id"], tasks["viewer_gone"]["id"]}
    assert ids(scoped, "decisions") == {decisions["viewer_open"]["id"], gone["id"]}
    assert ids(scoped, "runs") == {runs["viewer_held"]["id"], runs["viewer_done"]["id"]}
    assert scoped["omitted"] == {"tasks": 1, "decisions": 1, "runs": 1}

    active = call(client, "hub_overview", {"active_only": True})["structuredContent"]
    assert ids(active, "tasks") == {tasks["viewer_open"]["id"], tasks["treedar_open"]["id"]}
    assert ids(active, "decisions") == {decisions["viewer_open"]["id"], decisions["treedar_open"]["id"]}
    assert ids(active, "runs") == {runs["viewer_held"]["id"], runs["treedar_running"]["id"]}
    assert active["omitted"] == {"tasks": 1, "decisions": 1, "runs": 1}

    both = call(client, "hub_overview", {"active_only": True, "project": "3d-viewer"})["structuredContent"]
    assert ids(both, "tasks") == {tasks["viewer_open"]["id"]}
    assert ids(both, "decisions") == {decisions["viewer_open"]["id"]}
    assert ids(both, "runs") == {runs["viewer_held"]["id"]}
    assert both["omitted"] == {"tasks": 2, "decisions": 2, "runs": 2}
    # Presentation only: global sections are unchanged by the project filter.
    assert both["paused"] == full["paused"]

    nothing = call(client, "hub_overview", {"project": "no-such-project"})["structuredContent"]
    assert (nothing["tasks"], nothing["decisions"], nothing["runs"]) == ([], [], [])
    assert nothing["omitted"] == {"tasks": 3, "decisions": 3, "runs": 3}
    for bad in ({"active_only": "yes"}, {"active_only": 1}, {"project": ""}, {"project": 5}):
        assert call(client, "hub_overview", bad)["isError"] is True
