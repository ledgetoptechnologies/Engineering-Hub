"""Opt-in autonomous fixture dispatch through the complete ASGI lifecycle."""
import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.dispatcher import StagingDispatcher
from engineering_hub.events import EventBroker
from engineering_hub.executor_client import ExecutorClient
from engineering_hub.fixture_workflow import FixtureTarget, FixtureWorkflow
from engineering_hub.repository import HubRepository


BASE = "/api/hub/v1"
COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64
SECRET = "fixture-only-autonomy-service-secret"
AUTH = {"Authorization": "Bearer fixture-codex"}


class Feed:
    def __init__(self, supported=True):
        self.broker = EventBroker()
        self.supported = supported
        self.started = self.stopped = False
        self.reason = "not_started"

    @property
    def available(self):
        return self.broker.available

    @property
    def status(self):
        return {"available": self.available, "reason": self.reason,
                "observer_epoch": self.broker.observer_epoch, "cursor": self.broker.cursor}

    async def start(self):
        self.started = True
        if self.supported:
            self.broker.activate(0)
            self.reason = "native_source_active"
        else:
            self.reason = "native_source_unsupported"
        return self.status

    async def stop(self):
        self.stopped = True
        self.broker.fail()


def prepare(tmp_path, *, supported=True, timeout=False):
    settings = Settings(tmp_path / "hub.sqlite3",
                        AuthSettings("fixture-hermes", "fixture-codex", claude_token="fixture-claude"),
                        AuthConfig("https://hub.test", beau_github_id=42))
    repo = HubRepository(settings.database, execution_principal="executor:fixture")
    commands = []

    def transport(request):
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        assert request.method == "POST" and request.url.path == "/v1/commands"
        payload = json.loads(request.content)
        commands.append(payload)
        if timeout:
            raise httpx.ReadTimeout("fixture-private-accepted-response-lost", request=request)
        return httpx.Response(200, json=payload | {"state": "succeeded", "reason": None, "observed": {}})

    executor = ExecutorClient("https://fixture-executor.test", SECRET,
                              transport=httpx.MockTransport(transport))
    target = FixtureTarget("3d-viewer:staging", DIGEST, COMMIT, "sha256:" + "c" * 64, "d" * 40)
    workflow = FixtureWorkflow(repo, executor, [target])
    dispatcher = StagingDispatcher(repo, workflow)
    return SimpleNamespace(settings=settings, repo=repo, executor=executor, workflow=workflow,
                           dispatcher=dispatcher, feed=Feed(supported), commands=commands)


def app_for(harness, *, enabled=True):
    return create_app(harness.settings, repository=harness.repo, fixture_workflow=harness.workflow,
                      event_feed=harness.feed, staging_dispatcher=harness.dispatcher if enabled else None)


def reviewed_task(repo, key="startup"):
    item = repo.create_task("human:42", "3d-viewer", "Assigned fixture", key)
    item = repo.claim_task("codex", item["id"], item["revision"])
    item = repo.set_candidate("codex", item["id"], item["revision"], COMMIT)
    return repo.review_task("claude", item["id"], item["revision"], COMMIT, "approve")


async def until(predicate):
    async def check():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(check(), 3)


def human(client, app):
    session = app.state.sessions.create_session(42)
    client.cookies.set("hub_session", session["cookie_token"])
    return {"Origin": "https://hub.test", "X-Hub-CSRF": session["csrf_token"]}


def autonomy_read_surfaces(client):
    direct = client.get(BASE + "/autonomy", headers=AUTH)
    overview = client.get(BASE + "/overview", headers=AUTH)
    tool = client.post("/mcp/", headers={
        **AUTH, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "hub_overview", "arguments": {}}})
    assert direct.status_code == overview.status_code == tool.status_code == 200
    result = tool.json()["result"]
    assert result.get("isError") is not True
    content = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert direct.json() == overview.json()["autonomy"] == content["autonomy"]
    return direct.json(), tool.text


@pytest.mark.parametrize("enabled", [False, True])
def test_autonomy_status_is_consistent_and_redacted_across_rest_and_mcp(tmp_path, enabled):
    harness = prepare(tmp_path)
    if enabled:
        reviewed_task(harness.repo)
    app = app_for(harness, enabled=enabled)
    with TestClient(app, base_url="https://hub.test") as client:
        try:
            if enabled:
                runtime = app.state.staging_runtime
                client.portal.call(until, lambda: runtime.last_result is not None
                                   and not runtime.last_result["more"]
                                   and any(item["state"] == "succeeded" for item in harness.dispatcher.receipts()))
            public, tool_text = autonomy_read_surfaces(client)
            assert public["configured"] is enabled
            assert public["running"] is enabled
            assert public["state"] == ("active" if enabled else "disabled")
            assert SECRET not in tool_text
            assert all("owner" not in item and "request" not in item for item in public["receipts"])
            if enabled:
                private_owner = harness.dispatcher.receipts()[0]["owner"]
                assert private_owner not in tool_text
                assert public["receipts"][0]["state"] == "succeeded"
            else:
                assert public["receipts"] == []
        finally:
            if app.state.staging_runtime is not None:
                client.portal.call(app.state.staging_runtime.stop)
            client.portal.call(harness.executor.aclose)


def test_default_does_not_start_autonomous_dispatch_even_with_fixture_workflow(tmp_path):
    harness = prepare(tmp_path)
    reviewed_task(harness.repo)
    app = app_for(harness, enabled=False)
    with TestClient(app, base_url="https://hub.test") as client:
        try:
            assert client.get(BASE + "/autonomy", headers=AUTH).json() == {
                "configured": False, "state": "disabled", "running": False, "receipts": [],
            }
            assert app.state.staging_runtime is None
            assert harness.repo.list_runs() == [] and harness.commands == []
            assert harness.feed.broker.subscriber_count == 0
        finally:
            client.portal.call(harness.executor.aclose)
    assert harness.feed.stopped


def test_status_requires_auth_and_cannot_be_used_to_enable_runtime(tmp_path):
    harness = prepare(tmp_path)
    app = app_for(harness, enabled=False)
    with TestClient(app, base_url="https://hub.test") as client:
        try:
            for headers in ({}, {"Authorization": "Bearer invalid"}):
                assert client.get(BASE + "/autonomy", headers=headers).status_code == 401
            human(client, app)
            assert client.get(BASE + "/autonomy").status_code == 200
            assert client.get(BASE + "/autonomy", headers={"Authorization": "Bearer invalid"}).status_code == 401
            assert client.post(BASE + "/autonomy", headers=AUTH, json={"enabled": True}).status_code == 405
            assert harness.commands == []
        finally:
            client.portal.call(harness.executor.aclose)


def test_injected_dispatcher_requires_same_repository_and_workflow(tmp_path):
    harness = prepare(tmp_path)
    different_repo = HubRepository(tmp_path / "different.sqlite3", execution_principal="executor:fixture")
    for mismatch in (SimpleNamespace(repo=different_repo, workflow=harness.workflow),
                     SimpleNamespace(repo=harness.repo, workflow=object())):
        with pytest.raises(ValueError, match="dispatcher_workflow_mismatch"):
            create_app(harness.settings, repository=harness.repo, fixture_workflow=harness.workflow,
                       staging_dispatcher=mismatch, event_feed=harness.feed)
    # No app lifespan/client was entered, and MockTransport owns no loop I/O.
    with TestClient(app_for(harness, enabled=False), base_url="https://hub.test") as client:
        client.portal.call(harness.executor.aclose)


def test_unsupported_source_does_not_start_configured_dispatcher(tmp_path):
    harness = prepare(tmp_path, supported=False)
    reviewed_task(harness.repo)
    app = app_for(harness)
    with TestClient(app, base_url="https://hub.test") as client:
        try:
            response = client.get(BASE + "/autonomy", headers=AUTH)
            assert response.status_code == 200
            assert response.json()["configured"] is True
            assert response.json()["state"] == "native_source_unavailable"
            assert response.json()["running"] is False
            assert harness.commands == [] and harness.repo.list_runs() == []
        finally:
            client.portal.call(harness.executor.aclose)


def test_startup_catchup_autonomously_dispatches_real_fixture_and_redacts_receipts(tmp_path):
    harness = prepare(tmp_path)
    task = reviewed_task(harness.repo)
    app = app_for(harness)
    with TestClient(app, base_url="https://hub.test") as client:
        try:
            client.portal.call(until, lambda: any(item["state"] == "succeeded" for item in harness.dispatcher.receipts()))
            response = client.get(BASE + "/autonomy", headers=AUTH)
            data = response.json()
            assert data["configured"] is True and data["running"] is True
            assert data["state"] == "active"
            assert len(data["receipts"]) == 1
            assert data["receipts"][0]["entity_id"] == task["id"]
            assert data["receipts"][0]["state"] == "succeeded"
            assert "owner" not in data["receipts"][0] and "request" not in data["receipts"][0]
            assert SECRET not in response.text
            assert [item["action"] for item in harness.commands] == ["deploy_fixture", "smoke_fixture"]
            caps = client.get(BASE + "/overview", headers=AUTH).json()["capabilities"]
            assert caps["fixture_dispatch"] is True and caps["host_dispatch"] is False
            assert caps["production"] is False and caps["processing"] is False
        finally:
            client.portal.call(app.state.staging_runtime.stop)
            client.portal.call(harness.executor.aclose)
    assert harness.feed.stopped and harness.feed.broker.subscriber_count == 0


def test_new_review_dispatches_on_native_pointer_not_manual_run_endpoint(tmp_path):
    harness = prepare(tmp_path)
    app = app_for(harness)
    with TestClient(app, base_url="https://hub.test") as client:
        try:
            runtime = app.state.staging_runtime
            client.portal.call(until, lambda: runtime.last_result is not None)
            headers = human(client, app)
            response = client.post(BASE + "/tasks", headers=headers, json={
                "project": "3d-viewer", "title": "Autonomous fixture", "client_request_id": "new-review",
            })
            assert response.status_code == 201
            item = response.json()
            item = client.post(f"{BASE}/tasks/{item['id']}/claim", headers=AUTH,
                               json={"expected_revision": item["revision"]}).json()
            item = client.post(f"{BASE}/tasks/{item['id']}/candidate", headers=AUTH,
                               json={"expected_revision": item["revision"], "commit": COMMIT}).json()
            item = client.post(f"{BASE}/tasks/{item['id']}/review", headers={"Authorization": "Bearer fixture-claude"},
                               json={"expected_revision": item["revision"], "commit": COMMIT, "verdict": "approve"}).json()
            assert item["status"] == "verified" and harness.commands == []
            # Fixture publishes exactly the pointer a native source would emit.
            # No POST /runs or /dispatch call is made.
            cursor = harness.repo.list_outbox(limit=500)[-1]["sequence"]
            client.portal.call(harness.feed.broker.publish, cursor)
            client.portal.call(until, lambda: any(receipt["state"] == "succeeded" for receipt in harness.dispatcher.receipts()))
            assert len(harness.repo.list_runs()) == 1 and len(harness.commands) == 2
        finally:
            client.portal.call(app.state.staging_runtime.stop)
            client.portal.call(harness.executor.aclose)


def test_unknown_delivery_exposed_as_reconciliation_without_automatic_retry(tmp_path):
    harness = prepare(tmp_path, timeout=True)
    reviewed_task(harness.repo)
    app = app_for(harness)
    with TestClient(app, base_url="https://hub.test") as client:
        try:
            client.portal.call(until, lambda: any(item["state"] == "needs_reconciliation" for item in harness.dispatcher.receipts()))
            response = client.get(BASE + "/autonomy", headers=AUTH)
            receipt = response.json()["receipts"][0]
            assert receipt["needs_reconciliation"] is True and receipt["reason"] == "held"
            assert len(harness.commands) == 1
            client.portal.call(harness.feed.broker.publish, 100)
            client.portal.call(until, lambda: not app.state.staging_runtime.last_result["more"])
            assert len(harness.commands) == 1
            assert "fixture-private" not in response.text and SECRET not in response.text
        finally:
            client.portal.call(app.state.staging_runtime.stop)
            client.portal.call(harness.executor.aclose)


def test_paused_startup_keeps_review_and_unpause_pointer_resumes_without_manual_dispatch(tmp_path):
    harness = prepare(tmp_path)
    reviewed_task(harness.repo)
    harness.repo.set_pause("human:42", True)
    app = app_for(harness)
    with TestClient(app, base_url="https://hub.test") as client:
        try:
            runtime = app.state.staging_runtime
            client.portal.call(until, lambda: runtime.last_result is not None and runtime.last_result["paused"])
            assert harness.commands == [] and harness.repo.list_runs() == []
            headers = human(client, app)
            assert client.post(BASE + "/pause", headers=AUTH, json={"paused": False}).status_code == 403
            assert client.post(BASE + "/pause", headers=headers, json={"paused": False}).status_code == 200
            cursor = harness.repo.list_outbox(limit=500)[-1]["sequence"]
            client.portal.call(harness.feed.broker.publish, cursor)
            client.portal.call(until, lambda: any(item["state"] == "succeeded" for item in harness.dispatcher.receipts()))
            assert len(harness.commands) == 2 and len(harness.repo.list_runs()) == 1
        finally:
            client.portal.call(app.state.staging_runtime.stop)
            client.portal.call(harness.executor.aclose)
