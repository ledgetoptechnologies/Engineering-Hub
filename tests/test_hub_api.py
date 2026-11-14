"""HTTP integration boundaries for Hub, with no live credentials or services."""
import json

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig


BASE = "/api/hub/v1"
COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def hub(tmp_path):
    settings = Settings(
        tmp_path / "hub.sqlite3",
        AuthSettings("test-hermes", "test-codex", claude_token="test-claude"),
        AuthConfig("https://hub.test", beau_github_id=42),
    )
    app = create_app(settings)
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        yield client, app, settings


def agent_headers(name="codex"):
    return {"Authorization": f"Bearer test-{name}"}


def human_headers(client, app, settings):
    # Internal fixture only: production sessions are minted by GitHub callback.
    session = app.state.sessions.create_session(42)
    client.cookies.set(settings.auth_config.cookie_name, session["cookie_token"])
    return {"Origin": settings.auth_config.origin, "X-Hub-CSRF": session["csrf_token"]}


def new_task(client, headers, key="pilot"):
    response = client.post(BASE + "/tasks", headers=headers, json={
        "project": "3d-viewer", "title": "Staging pilot", "client_request_id": key,
    })
    assert response.status_code == 201, response.text
    return response.json()


def claim_candidate(client, item):
    response = client.post(f"{BASE}/tasks/{item['id']}/claim", headers=agent_headers(),
                           json={"expected_revision": item["revision"]})
    assert response.status_code == 200, response.text
    claimed = response.json()
    response = client.post(f"{BASE}/tasks/{item['id']}/candidate", headers=agent_headers(),
                           json={"expected_revision": claimed["revision"], "commit": COMMIT})
    assert response.status_code == 200, response.text
    return response.json()


def test_overview_requires_identity_and_has_explicit_unknown_readiness(hub):
    client, _, _ = hub
    assert client.get(BASE + "/overview").status_code == 401
    response = client.get(BASE + "/overview", headers=agent_headers("hermes"))
    assert response.status_code == 200
    overview = response.json()
    assert overview["capabilities"] == {
        # A configured owner enables discovery for authenticated internal agents;
        # this navigation capability does not enable any executor.
        "infrastructure": True,
        "production": False, "host_dispatch": False, "github_merge": False,
        "processing": False, "fixture_dispatch": False, "container_pilot_dispatch": False,
        "merge_gate_strength": "not_enforced",
    }
    assert all(not project["executor_enabled"] and project["health"] == "unknown"
               for project in overview["projects"])
    assert all(agent["transport"] == "unknown" and agent["chat_readiness"] == "unverified"
               for agent in overview["agents"])
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_image_versions_are_authenticated_advisory_reads(hub, monkeypatch):
    client, _, _ = hub
    calls = []
    from engineering_hub import app as application
    from engineering_hub.image_version_reports import ImageVersionsSummary
    def source(_):
        calls.append(True)
        return ImageVersionsSummary(configured=False, reason='not_configured').model_dump(mode='json')
    monkeypatch.setattr(application, 'image_versions_for', source)
    assert client.get(BASE + '/image-versions').status_code == 401
    assert calls == []
    response = client.get(BASE + '/image-versions', headers=agent_headers())
    assert response.status_code == 200 and response.json()['advisory_only'] is True
    assert response.json()['reason'] == 'not_configured'
    overview = client.get(BASE + '/overview', headers=agent_headers()).json()
    assert overview['image_versions'] == response.json()
    assert overview['capabilities']['production'] is False
    assert overview['runs'] == [] and len(calls) == 2
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize("name", ["hermes", "codex", "claude"])
@pytest.mark.parametrize("path", ["overview", "projects", "agents", "tasks", "decisions", "runs"])
def test_existing_bus_agent_identities_can_read(hub, name, path):
    client, _, _ = hub
    assert client.get(f"{BASE}/{path}", headers=agent_headers(name)).status_code == 200


def test_agent_cannot_mint_human_actions(hub):
    client, app, settings = hub
    human = human_headers(client, app, settings)
    decision = client.post(BASE + "/decisions", headers=human, json={
        "project": "3d-viewer", "title": "Release?", "options": ["Yes", "No"], "protected": True,
    }).json()
    client.cookies.clear()
    for name in ("hermes", "codex", "claude"):
        bearer = agent_headers(name)
        response = client.post(BASE + "/tasks", headers=bearer, json={
            "project": "3d-viewer", "title": "Unassigned work", "client_request_id": name,
        })
        assert response.status_code == 403
        response = client.post(f"{BASE}/decisions/{decision['id']}/answer", headers=bearer,
                               json={"expected_revision": 1, "answer": "Yes"})
        assert response.status_code == 403
        assert client.post(BASE + "/pause", headers=bearer, json={"paused": True}).status_code == 403
    assert app.state.repository.list_tasks() == []
    assert app.state.repository.list_decisions()[0]["status"] == "open"
    assert app.state.repository.get_paused() is False


@pytest.mark.parametrize("authorization", ["Bearer invalid", "Basic invalid", "Bearer"])
def test_supplied_invalid_bearer_does_not_fall_back_to_human_cookie(hub, authorization):
    client, app, settings = hub
    human = human_headers(client, app, settings)
    headers = human | {"Authorization": authorization}
    assert client.get(BASE + "/overview", headers=headers).status_code == 401
    assert client.post(BASE + "/decisions", headers=headers, json={
        "project": "3d-viewer", "title": "Question", "options": ["Yes", "No"],
    }).status_code == 401
    assert client.post(BASE + "/tasks", headers=headers, json={
        "project": "3d-viewer", "title": "Spoof", "client_request_id": "mixed-principal",
    }).status_code == 403
    assert client.post(BASE + "/pause", headers=headers, json={"paused": True}).status_code == 403
    assert app.state.repository.list_tasks() == []
    assert app.state.repository.list_decisions() == []
    assert app.state.repository.get_paused() is False


def test_human_mutations_require_matching_csrf_and_origin(hub):
    client, app, settings = hub
    headers = human_headers(client, app, settings)
    payload = {"project": "3d-viewer", "title": "Pilot", "client_request_id": "csrf"}
    for rejected in ({}, {"Origin": settings.auth_config.origin},
                     headers | {"Origin": "https://evil.test"},
                     headers | {"X-Hub-CSRF": "incorrect"}):
        assert client.post(BASE + "/tasks", headers=rejected, json=payload).status_code == 403
    item = new_task(client, headers)
    assert item["status"] == "assigned" and item["owner"] is None
    assert app.state.repository.list_audit()[0]["actor"] == "human:42"


def test_claim_candidate_independent_review_is_exact_and_revision_bound(hub):
    client, app, settings = hub
    item = new_task(client, human_headers(client, app, settings))
    candidate = claim_candidate(client, item)
    assert candidate["owner"] == "codex" and candidate["status"] == "in_review"
    path = f"{BASE}/tasks/{item['id']}/review"
    payload = {"expected_revision": candidate["revision"], "commit": COMMIT, "verdict": "approve"}
    assert client.post(path, headers=agent_headers(), json=payload).status_code == 403
    assert client.post(path, headers=agent_headers("claude"), json=payload | {"commit": "c" * 40}).status_code == 409
    assert client.post(path, headers=agent_headers("claude"), json=payload | {"expected_revision": 1}).status_code == 409
    response = client.post(path, headers=agent_headers("claude"), json=payload)
    assert response.status_code == 200
    reviewed = response.json()
    assert reviewed["status"] == "verified"
    assert reviewed["review"] == {"actor": "claude", "commit": COMMIT, "verdict": "approve"}
    changed = client.post(f"{BASE}/tasks/{item['id']}/candidate", headers=agent_headers(),
                          json={"expected_revision": reviewed["revision"], "commit": "d" * 40})
    assert changed.status_code == 200
    assert changed.json()["review"] is None
    assert changed.json()["status"] == "in_review"


@pytest.mark.parametrize("field", ["actor", "reviewer", "owner", "approved_by"])
def test_spoofed_identity_fields_rejected_without_echo(hub, field):
    client, app, settings = hub
    human = human_headers(client, app, settings)
    sentinel = "private-spoof-value-do-not-echo"
    response = client.post(BASE + "/tasks", headers=human, json={
        "project": "3d-viewer", "title": "Pilot", "client_request_id": "spoof",
        field: sentinel,
    })
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_input"}}
    assert sentinel not in response.text and field not in response.text
    item = claim_candidate(client, new_task(client, human))
    response = client.post(f"{BASE}/tasks/{item['id']}/review", headers=agent_headers("claude"), json={
        "expected_revision": item["revision"], "commit": COMMIT, "verdict": "approve", field: sentinel,
    })
    assert response.status_code == 422 and sentinel not in response.text
    assert app.state.repository.get_task(item["id"])["review"] is None


def test_protected_decisions_never_grant_execution_capabilities(hub):
    client, app, settings = hub
    payload = {"project": "3d-viewer", "title": "Production release?",
               "options": ["Approve", "Reject"], "protected": True}
    # An agent can request a protected decision, but cannot answer it or grant
    # capabilities merely by marking the request protected.
    created = client.post(BASE + "/decisions", headers=agent_headers(), json=payload)
    assert created.status_code == 201
    decision = created.json()
    denied = client.post(f"{BASE}/decisions/{decision['id']}/answer", headers=agent_headers(),
                         json={"expected_revision": decision["revision"], "answer": "Approve"})
    assert denied.status_code == 403
    human = human_headers(client, app, settings)
    answer = client.post(f"{BASE}/decisions/{decision['id']}/answer", headers=human,
                         json={"expected_revision": decision["revision"], "answer": "Approve"})
    assert answer.status_code == 200 and answer.json()["answered_by"] == "human:42"
    overview = client.get(BASE + "/overview", headers=agent_headers()).json()
    assert overview["capabilities"]["production"] is False
    assert overview["capabilities"]["host_dispatch"] is False
    assert overview["runs"] == []


@pytest.mark.parametrize("target", ["3d-viewer:staging", "3d-viewer:production", "unlisted-host"])
def test_all_host_dispatch_is_fail_closed(hub, target):
    client, app, settings = hub
    item = claim_candidate(client, new_task(client, human_headers(client, app, settings)))
    reviewed = client.post(f"{BASE}/tasks/{item['id']}/review", headers=agent_headers("claude"),
                           json={"expected_revision": item["revision"], "commit": COMMIT, "verdict": "approve"}).json()
    for name in ("hermes", "codex", "claude"):
        created = client.post(BASE + "/runs", headers=agent_headers(name), json={
            "task_id": item["id"], "expected_revision": reviewed["revision"],
            "artifact_digest": DIGEST, "target": target,
        })
        assert created.status_code == 423
        assert created.json() == {"detail": {"code": "executor_not_enabled"}}
        dispatched = client.post(BASE + "/runs/unconfigured-run/dispatch", headers=agent_headers(name))
        assert dispatched.status_code == 423
    assert app.state.repository.list_runs() == []


def test_reopened_app_preserves_tasks_decisions_sessions_and_pause(hub):
    client, app, settings = hub
    human = human_headers(client, app, settings)
    item = new_task(client, human)
    decision = client.post(BASE + "/decisions", headers=agent_headers(), json={
        "project": "3d-viewer", "title": "Storage", "options": ["NAS", "SSD"],
    }).json()
    assert client.post(BASE + "/pause", headers=human, json={"paused": True}).status_code == 200
    reopened_app = create_app(settings)
    with TestClient(reopened_app, base_url=settings.auth_config.origin) as reopened:
        reopened.cookies.update(client.cookies)
        response = reopened.get(BASE + "/overview")
        assert response.status_code == 200
        # The overview adds the audited state_since; it must survive a reopen.
        assert response.json()["tasks"] == [dict(item, state_since=item["updated_at"])]
        assert response.json()["decisions"] == [decision]
        assert response.json()["paused"] is True
        assert reopened.get(BASE + "/auth/me").json()["actor"] == "human:42"
    assert len(reopened_app.state.repository.list_audit()) == 3


def test_documented_inputs_forbid_actor_spoofing_and_interactive_docs_disabled(hub):
    client, app, _ = hub
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    schema = app.openapi()
    for name in ("NewTask", "Revision", "Candidate", "Review", "NewDecision", "Answer", "Pause", "NewRun"):
        model = schema["components"]["schemas"][name]
        assert model["additionalProperties"] is False
        assert not {"actor", "reviewer", "owner", "approved_by"} & model["properties"].keys()
    assert "/api/hub/v1/runs/{run_id}/dispatch" in schema["paths"]


def mcp_rpc(client, method, params=None, *, name="codex", headers=None):
    request_headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
        **(agent_headers(name) if name else {}),
        **(headers or {}),
    }
    return client.post("/mcp/", headers=request_headers, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    })


def initialize_mcp(client, **kwargs):
    return mcp_rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "integrated-hub-api-test", "version": "1"},
    }, **kwargs)


def test_integrated_mcp_initialize_and_list_on_configured_https_origin(hub):
    client, _, _ = hub
    response = initialize_mcp(client, headers={"Origin": "https://hub.test"})
    assert response.status_code == 200, response.text
    assert response.json()["result"]["serverInfo"]["name"] == "Engineering Hub"
    for name in ("hermes", "codex", "claude"):
        response = mcp_rpc(client, "tools/list", name=name)
        assert response.status_code == 200, response.text
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
            "hub_overview", "hub_project_tasks", "hub_tracking_task_propose", "hub_tracking_task_origin", "hub_orchestration_request_tasks", "hub_orchestration_request_origin_task_summary", "hub_tracking_task_update", "hub_tracking_task_list", "hub_tracking_task_review", "hub_role_list", "hub_role_get", "hub_role_acquire", "hub_role_renew", "hub_role_release", "hub_role_claim", "hub_role_claims", "hub_role_claim_release", "hub_task_progress_get", "hub_task_progress_update", "hub_orchestration_request_create", "hub_orchestration_request_get", "hub_orchestration_request_list", "hub_dispatch_create", "hub_dispatch_get", "hub_dispatch_list", "hub_role_dispatch_inbox", "hub_dispatch_report", "hub_dispatch_checkpoint", "hub_dispatch_decisions", "hub_dispatch_reports", "hub_orchestrator_attention_list", "hub_orchestrator_attention_reconcile_closed", "hub_addressed_attention_list", "hub_addressed_attention_ack", "hub_worker_dispatch_pending", "hub_worker_dispatch_ack", "hub_worker_decision_inspect", "hub_orchestrator_report_inspect", "hub_tracking_task_history", "hub_tracking_task_compact", "hub_tracking_claim_withdraw", "hub_tracking_draft_propose", "hub_task_get", "hub_task_claim", "hub_task_candidate",
            "hub_task_review", "hub_decision_request", "hub_decision_get", "hub_run_list",
            "hub_topic_peers", "hub_topic_preflight", "hub_staging_task_propose",
            "hub_planning_list", "hub_planning_get", "hub_planning_put",
            "hub_label_set_create", "hub_label_set_put_image", "hub_label_set_results", "hub_label_set_list",
            "hub_entity_put_image", "hub_entity_images", "hub_entity_withdraw",
            "hub_project_list", "hub_project_get",
            "hub_component_list", "hub_component_get", "hub_component_create",
            "hub_component_update", "hub_component_retire", "hub_interactive_review_create",
            "hub_interactive_review_get", "hub_interactive_review_list",
            "hub_infrastructure_list", "hub_infrastructure_get",
            "hub_infrastructure_maintenance_open", "hub_infrastructure_maintenance_resolve",
            "hub_infrastructure_probe_record", "hub_infrastructure_metrics",
        }
    assert client.get(BASE + "/overview", headers=agent_headers()).status_code == 200
    assert client.get("/health").json()["service"] == "engineering-hub-control"
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert BASE + "/tasks" in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("method", ["initialize", "tools/list", "tools/call"])
def test_integrated_mcp_rejects_missing_and_invalid_bearer_even_with_human_cookie(hub, method):
    client, app, settings = hub
    human_headers(client, app, settings)
    for authorization in (None, "Bearer invalid", "Basic invalid"):
        headers = {"Authorization": authorization} if authorization else {}
        response = mcp_rpc(client, method, name=None, headers=headers)
        assert response.status_code == 401, response.text
        assert response.headers["www-authenticate"] == "Bearer"
        assert app.state.repository.list_tasks() == []


def test_integrated_mcp_rebinding_and_cross_origin_are_rejected(hub):
    client, _, _ = hub
    response = initialize_mcp(client, headers={"Host": "attacker.test"})
    assert response.status_code == 421, response.text
    response = initialize_mcp(client, headers={"Origin": "https://attacker.test"})
    assert response.status_code == 403, response.text
    assert initialize_mcp(client).status_code == 200


def test_integrated_mcp_and_rest_share_persisted_state_across_app_lifespans(hub):
    client, app, settings = hub
    human = human_headers(client, app, settings)
    item = new_task(client, human, key="mcp-reopen")
    assert initialize_mcp(client).status_code == 200
    response = mcp_rpc(client, "tools/call", {
        "name": "hub_task_claim", "arguments": {"task_id": item["id"], "expected_revision": 1},
    })
    assert response.status_code == 200, response.text
    assert response.json()["result"].get("isError") is not True
    assert client.get(f"{BASE}/tasks/{item['id']}", headers=agent_headers()).json()["owner"] == "codex"
    # A second app runs its own MCP lifespan over the same durable database.
    reopened_app = create_app(settings)
    with TestClient(reopened_app, base_url=settings.auth_config.origin) as reopened:
        assert initialize_mcp(reopened).status_code == 200
        response = mcp_rpc(reopened, "tools/call", {
            "name": "hub_task_get", "arguments": {"task_id": item["id"]},
        })
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result.get("isError") is not True
        returned = result.get("structuredContent")
        if returned is None:
            returned = json.loads(result["content"][0]["text"])
        assert returned["owner"] == "codex"
        assert reopened.get(BASE + "/overview", headers=agent_headers()).status_code == 200

def test_human_project_tracking_is_scoped_paginated_and_non_executable(hub):
    client, app, settings = hub
    path = BASE + '/projects/3d-viewer/tracking-tasks'
    assert client.get(path).status_code == 401
    assert client.get(path, headers=agent_headers()).status_code == 403
    human_headers(client, app, settings)
    first = app.state.repository.propose_tracking_task('codex','3d-viewer','First','human-tracking-1')
    second = app.state.repository.propose_tracking_task('codex','3d-viewer','Second','human-tracking-2')
    app.state.repository.propose_tracking_task('codex','project-alpha','Private other','human-tracking-3')
    app.state.repository.update_tracking_task('codex',first['id'],1,'human-tracking-update',
        progress='Claimed done',evidence_links=[{'label':'Proof','url':'https://example.com/proof'}],completion_claim=True)
    page = client.get(path,params={'limit':1}).json()
    assert page['truncated'] is True and page['next_after'] == page['tasks'][0]['id']
    next_page=client.get(path,params={'limit':1,'after_task_id':page['next_after']}).json()
    assert {page['tasks'][0]['id'],next_page['tasks'][0]['id']} == {first['id'],second['id']}
    assert all(item['project']=='3d-viewer' and item['execution_authorization'] is False for item in page['tasks']+next_page['tasks'])
    claimed=next(item for item in page['tasks']+next_page['tasks'] if item['id']==first['id'])
    assert claimed['status']=='completion_claimed' and claimed['updates'][0]['evidence_links'][0]['label']=='Proof'
    assert client.get(path,params={'limit':0}).status_code == 422
    assert client.get(path,params={'after_task_id':'bad'}).status_code == 422
    assert client.post(path).status_code == 405


def test_unfinished_tracking_filters_before_pagination_and_reopened_returns(hub, monkeypatch):
    import uuid
    client, app, settings = hub
    human_headers(client, app, settings)
    repo = app.state.repository
    ids = iter(uuid.UUID(int=i, version=4) for i in range(1, 100))
    monkeypatch.setattr('engineering_hub.repository.uuid.uuid4', lambda: next(ids))
    evidence = [{'label': 'Proof', 'url': 'https://example.com/proof'}]
    verified = []
    for i in range(52):
        item = repo.propose_tracking_task('codex', '3d-viewer', f'Finished {i}', f'list-done-{i}')
        repo.update_tracking_task('codex', item['id'], 1, f'list-claim-{i}',
                                  progress='Done', evidence_links=evidence, completion_claim=True)
        verified.append(repo.review_tracking_task('claude', item['id'], 2, f'list-review-{i}',
                         verdict='verify', note='Checked', evidence_links=evidence))
    open_item = repo.propose_tracking_task('codex', '3d-viewer', 'Open work', 'list-open')
    claim = repo.propose_tracking_task('codex', '3d-viewer', 'Needs review', 'list-needs-review')
    repo.update_tracking_task('codex', claim['id'], 1, 'list-new-claim',
                              progress='Ready', evidence_links=evidence, completion_claim=True)
    repo.propose_tracking_task('codex', 'project-alpha', 'Other project', 'list-other')
    path = BASE + '/projects/3d-viewer/tracking-tasks'
    legacy = client.get(path, params={'limit': 50}).json()
    assert legacy['truncated'] and len(legacy['tasks']) == 50
    assert all(item['status'] == 'completion_verified' for item in legacy['tasks'])
    first = client.get(path, params={'limit': 1, 'unfinished': 'true'}).json()
    second = client.get(path, params={'limit': 1, 'unfinished': 'true', 'after_task_id': first['next_after']}).json()
    assert first['truncated'] and not second['truncated']
    assert {first['tasks'][0]['id'], second['tasks'][0]['id']} == {open_item['id'], claim['id']}
    assert {first['tasks'][0]['status'], second['tasks'][0]['status']} == {'open', 'completion_claimed'}
    assert client.get(path, params={'unfinished': 'not-a-bool'}).status_code == 422
    repo.review_tracking_task('claude', verified[0]['id'], 3, 'list-reopen',
                              verdict='reopen', note='More work', evidence_links=[])
    page = client.get(path, params={'unfinished': 'true'}).json()
    assert {item['id'] for item in page['tasks']} == {verified[0]['id'], open_item['id'], claim['id']}


def test_human_tracking_review_requires_session_csrf_and_current_claim(hub):
    client, app, settings = hub
    item = app.state.repository.propose_tracking_task('codex', '3d-viewer', 'Review task', 'hr-propose')
    links = [{'label': 'Evidence', 'url': 'https://example.com/evidence'}]
    app.state.repository.update_tracking_task('codex', item['id'], 1, 'hr-claim', progress='Finished',
                                              evidence_links=links, completion_claim=True)
    path = BASE + '/tracking-tasks/' + item['id'] + '/review'
    payload = {'expected_revision': 2, 'client_request_id': 'hr-review', 'verdict': 'verify',
               'note': 'Evidence inspected', 'evidence_links': links}
    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, headers=agent_headers(), json=payload).status_code == 403
    headers = human_headers(client, app, settings)
    assert client.post(path, json=payload).status_code == 403
    verified = client.post(path, headers=headers, json=payload)
    assert verified.status_code == 200 and verified.json()['status'] == 'completion_verified'
    assert verified.json()['reviews'][0]['claim_revision'] == 2
    assert client.post(path, headers=headers, json={**payload, 'client_request_id': 'stale'}).status_code == 409

def test_human_tracking_review_csrf_and_competing_revision(hub):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client, app, settings = hub
    item = app.state.repository.propose_tracking_task('codex', '3d-viewer', 'Competing review', 'competition-propose')
    links = [{'label':'Evidence', 'url':'https://example.com/evidence'}]
    app.state.repository.update_tracking_task('codex', item['id'], 1, 'competition-claim',
        progress='Done', evidence_links=links, completion_claim=True)
    path = BASE + '/tracking-tasks/' + item['id'] + '/review'
    headers = human_headers(client, app, settings)
    payload = {'expected_revision':2, 'verdict':'verify', 'note':'Checked', 'evidence_links':links}
    assert client.post(path, headers={**headers, 'X-Hub-CSRF':'invalid'}, json={**payload, 'client_request_id':'bad-csrf'}).status_code == 403
    barrier = Barrier(2)
    def submit(key):
        barrier.wait(timeout=5)
        return client.post(path, headers=headers, json={**payload, 'client_request_id':key}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, key) for key in ('review-a','review-b')]
        statuses = sorted(f.result(timeout=10) for f in futures)
    assert statuses == [200,409]
    stored = app.state.repository._read(item['id'], 'tracking_task')
    assert stored['revision'] == 3 and len(stored['reviews']) == 1
    assert stored['reviews'][0]['claim_revision'] == 2
