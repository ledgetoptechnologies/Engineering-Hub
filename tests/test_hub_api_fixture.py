"""Whole HTTP workflow through the real typed executor client, no live hosts."""
import asyncio
from dataclasses import dataclass, replace
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.executor_client import ExecutorClient
from engineering_hub.fixture_workflow import FixtureTarget, FixtureWorkflow
from engineering_hub.repository import HubRepository


BASE = "/api/hub/v1"
COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64
OLD_COMMIT = "c" * 40
OLD_DIGEST = "sha256:" + "d" * 64
SERVICE_SECRET = "fixture-only-private-service-token"


@dataclass
class Harness:
    client: TestClient
    app: object
    settings: Settings
    repo: HubRepository
    workflow: FixtureWorkflow
    target: FixtureTarget
    commands: list
    outcomes: dict
    human: dict


def agent(name="codex"):
    return {"Authorization": f"Bearer fixture-{name}"}


@pytest.fixture
def fixture_hub(tmp_path):
    settings = Settings(tmp_path / "hub.sqlite3",
                        AuthSettings("fixture-hermes", "fixture-codex", claude_token="fixture-claude"),
                        AuthConfig("https://hub.test", beau_github_id=42))
    repo = HubRepository(settings.database, execution_principal="executor:fixture")
    target = FixtureTarget("3d-viewer:staging", DIGEST, COMMIT, OLD_DIGEST, OLD_COMMIT)
    commands, outcomes = [], {}

    def transport(request):
        assert request.headers["Authorization"] == f"Bearer {SERVICE_SECRET}"
        assert request.method == "POST" and request.url.path == "/v1/commands"
        payload = json.loads(request.content)
        commands.append(payload)
        outcome = outcomes.get(payload["action"], "succeeded")
        if outcome == "timeout":
            raise httpx.ReadTimeout("fixture-only-accepted-response-lost", request=request)
        if outcome == "pause":
            repo.set_pause("human:42", True)
            outcome = "succeeded"
        response = payload | {"state": outcome, "reason": None, "observed": {"fixture_only": True}}
        if outcome == "bad_echo":
            response.update(state="succeeded", artifact_digest="sha256:" + "e" * 64)
        return httpx.Response(200, json=response)

    executor = ExecutorClient("https://fixture-executor.test", SERVICE_SECRET,
                              transport=httpx.MockTransport(transport))
    workflow = FixtureWorkflow(repo, executor, [target])
    app = create_app(settings, repository=repo, fixture_workflow=workflow)
    with TestClient(app, base_url=settings.auth_config.origin) as client:
        session = app.state.sessions.create_session(42)  # Internal fixture; never an HTTP mint route.
        client.cookies.set(settings.auth_config.cookie_name, session["cookie_token"])
        human = {"Origin": settings.auth_config.origin, "X-Hub-CSRF": session["csrf_token"]}
        yield Harness(client, app, settings, repo, workflow, target, commands, outcomes, human)
    asyncio.run(executor.aclose())


def verified_task(hub, *, commit=COMMIT):
    response = hub.client.post(BASE + "/tasks", headers=hub.human, json={
        "project": "3d-viewer", "title": "Fixture staging pilot", "client_request_id": "fixture-pilot",
    })
    assert response.status_code == 201, response.text
    task = response.json()
    response = hub.client.post(f"{BASE}/tasks/{task['id']}/claim", headers=agent(),
                               json={"expected_revision": task["revision"]})
    assert response.status_code == 200
    task = response.json()
    response = hub.client.post(f"{BASE}/tasks/{task['id']}/candidate", headers=agent(),
                               json={"expected_revision": task["revision"], "commit": commit})
    assert response.status_code == 200
    task = response.json()
    payload = {"expected_revision": task["revision"], "commit": commit, "verdict": "approve"}
    assert hub.client.post(f"{BASE}/tasks/{task['id']}/review", headers=agent(), json=payload).status_code == 403
    response = hub.client.post(f"{BASE}/tasks/{task['id']}/review", headers=agent("claude"), json=payload)
    assert response.status_code == 200
    assert response.json()["review"]["actor"] == "claude"
    return response.json()


def create_run(hub, task=None, **changes):
    task = task or verified_task(hub)
    return hub.client.post(BASE + "/runs", headers=agent(), json={
        "task_id": task["id"], "expected_revision": task["revision"],
        "artifact_digest": DIGEST, "target": "3d-viewer:staging", **changes,
    })


def dispatch(hub, run, name="codex"):
    return hub.client.post(f"{BASE}/runs/{run['id']}/dispatch", headers=agent(name))


def test_fixture_http_success_independent_review_exact_manifest_and_capability_distinction(fixture_hub):
    hub = fixture_hub
    capabilities = hub.client.get(BASE + "/overview", headers=agent()).json()["capabilities"]
    assert capabilities["fixture_dispatch"] is True
    assert capabilities["host_dispatch"] is False and capabilities["production"] is False
    assert capabilities["github_merge"] is False and capabilities["processing"] is False
    assert all(not project["executor_enabled"] for project in hub.client.get(BASE + "/projects", headers=agent()).json()["items"])
    response = create_run(hub)
    assert response.status_code == 201, response.text
    run = response.json()
    response = dispatch(hub, run)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert [item["action"] for item in hub.commands] == ["deploy_fixture", "smoke_fixture"]
    assert all(item["artifact_digest"] == DIGEST and item["candidate_commit"] == COMMIT
               and item["target_id"] == "3d-viewer:staging" for item in hub.commands)
    assert len({item["command_id"] for item in hub.commands}) == 2
    assert dispatch(hub, run).json()["status"] == "succeeded"
    assert len(hub.commands) == 2
    assert SERVICE_SECRET not in response.text


@pytest.mark.parametrize("change", [
    {"artifact_digest": "sha256:" + "e" * 64}, {"target": "3d-viewer:production"},
    {"target": "treedar:staging"}, {"target": "../../unlisted"},
])
def test_fixture_manifest_rejection_happens_before_persistence_or_io(fixture_hub, change):
    hub = fixture_hub
    task = verified_task(hub)
    response = create_run(hub, task, **change)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": {"code": "fixture_manifest_mismatch"}}
    assert hub.repo.list_runs() == [] and hub.commands == []
    assert hub.repo.get_task(task["id"]) == task


def test_candidate_not_in_server_manifest_cannot_create_run(fixture_hub):
    hub = fixture_hub
    task = verified_task(hub, commit="f" * 40)
    assert create_run(hub, task).status_code == 403
    assert hub.repo.list_runs() == [] and hub.commands == []


def test_unsupported_manifest_policy_rejected_before_persist(fixture_hub):
    hub = fixture_hub
    hub.workflow.targets[hub.target.target_id] = replace(hub.target, policy_revision=2)
    task = verified_task(hub)
    response = create_run(hub, task)
    assert response.status_code == 403, response.text
    assert hub.repo.list_runs() == [] and hub.commands == []
    assert hub.repo.get_task(task["id"]) == task


def test_complete_manifest_pinned_atomically_and_change_denied_before_first_effect(fixture_hub):
    hub = fixture_hub
    run = create_run(hub).json()
    assert run["execution_manifest_digest"].startswith("sha256:")
    assert hub.repo.get_run(run["id"])["execution_manifest_digest"] == run["execution_manifest_digest"]
    hub.workflow.targets[hub.target.target_id] = replace(hub.target, rollback_commit="e" * 40)
    response = dispatch(hub, run)
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": {"code": "fixture_manifest_changed"}}
    assert hub.commands == [] and hub.repo.get_run(run["id"])["status"] == "pending"


def test_only_run_owner_agent_can_dispatch_and_human_cookies_do_not_substitute(fixture_hub):
    hub = fixture_hub
    response = create_run(hub)
    assert response.status_code == 201
    run = response.json()
    for identity in ("hermes", "claude"):
        assert dispatch(hub, run, identity).status_code == 403
    path = f"{BASE}/runs/{run['id']}/dispatch"
    assert hub.client.post(path, headers=hub.human).status_code == 401
    assert hub.client.post(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert hub.commands == [] and hub.repo.get_run(run["id"])["status"] == "pending"
    assert dispatch(hub, run).json()["status"] == "succeeded"


def test_smoke_failure_rolls_back_pinned_artifact_and_repeat_dispatch_has_no_effects(fixture_hub):
    hub = fixture_hub
    hub.outcomes["smoke_fixture"] = "failed"
    run = create_run(hub).json()
    response = dispatch(hub, run)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rolled_back"
    assert [item["action"] for item in hub.commands] == ["deploy_fixture", "smoke_fixture", "rollback_fixture"]
    assert hub.commands[-1]["artifact_digest"] == OLD_DIGEST
    assert hub.commands[-1]["candidate_commit"] == OLD_COMMIT
    assert hub.repo.get_task(run["task_id"])["repair_count"] == 1
    assert dispatch(hub, run).json()["status"] == "rolled_back"
    assert len(hub.commands) == 3


@pytest.mark.parametrize("outcome", ["timeout", "bad_echo", "pending", "running", "held"])
def test_unknown_or_nonterminal_delivery_holds_and_repeat_never_resubmits(fixture_hub, outcome):
    hub = fixture_hub
    hub.outcomes["deploy_fixture"] = outcome
    run = create_run(hub).json()
    response = dispatch(hub, run)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "held"
    assert len(hub.commands) == 1
    assert dispatch(hub, run).json()["status"] == "held"
    assert len(hub.commands) == 1
    assert "fixture-only-accepted-response-lost" not in response.text and SERVICE_SECRET not in response.text


def test_human_pause_prevents_dispatch_then_unpause_allows_same_pending_run(fixture_hub):
    hub = fixture_hub
    run = create_run(hub).json()
    assert hub.client.post(BASE + "/pause", headers=agent(), json={"paused": True}).status_code == 403
    assert hub.client.post(BASE + "/pause", headers=hub.human, json={"paused": True}).status_code == 200
    assert dispatch(hub, run).status_code == 423
    assert hub.commands == [] and hub.repo.get_run(run["id"])["status"] == "pending"
    assert hub.client.post(BASE + "/pause", headers=hub.human, json={"paused": False}).status_code == 200
    assert dispatch(hub, run).json()["status"] == "succeeded"


def test_pause_between_effects_prevents_smoke_and_holds(fixture_hub):
    hub = fixture_hub
    hub.outcomes["deploy_fixture"] = "pause"
    run = create_run(hub).json()
    response = dispatch(hub, run)
    assert response.status_code == 200 and response.json()["status"] == "held"
    assert [item["action"] for item in hub.commands] == ["deploy_fixture"]
    assert hub.client.post(BASE + "/pause", headers=hub.human, json={"paused": False}).status_code == 200
    assert dispatch(hub, run).json()["status"] == "held"
    assert len(hub.commands) == 1


def test_default_application_still_cannot_dispatch_with_same_config_and_database(fixture_hub):
    hub = fixture_hub
    run = create_run(hub).json()
    default_app = create_app(hub.settings)
    with TestClient(default_app, base_url=hub.settings.auth_config.origin) as client:
        capabilities = client.get(BASE + "/overview", headers=agent()).json()["capabilities"]
        assert capabilities["fixture_dispatch"] is False and capabilities["host_dispatch"] is False
        assert client.post(f"{BASE}/runs/{run['id']}/dispatch", headers=agent()).status_code == 423
    assert hub.commands == []


def test_reject_workflow_repository_mismatch(fixture_hub, tmp_path):
    hub = fixture_hub
    different = HubRepository(tmp_path / "different.sqlite3", execution_principal="executor:fixture")
    with pytest.raises(ValueError, match="fixture_repository_mismatch"):
        create_app(hub.settings, repository=different, fixture_workflow=hub.workflow)
