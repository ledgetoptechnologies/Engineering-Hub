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
        "production": False, "host_dispatch": False, "github_merge": False,
        "processing": False, "merge_gate_strength": "not_enforced",
    }
    assert all(not project["executor_enabled"] and project["health"] == "unknown"
               for project in overview["projects"])
    assert all(agent["transport"] == "unknown" and agent["chat_readiness"] == "unverified"
               for agent in overview["agents"])
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


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
        assert response.json()["tasks"] == [item]
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
            "hub_overview", "hub_task_get", "hub_task_claim", "hub_task_candidate",
            "hub_task_review", "hub_decision_request", "hub_run_list",
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
