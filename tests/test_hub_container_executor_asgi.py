"""ASGI protocol tests use HTTPX's in-process transport only."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from engineering_hub.container_pilot_client import ContainerPilotRequest
from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.asgi import create_app
from tests.test_hub_container_pilot_manifest import make_manifest_data


def run_owned_loop(coroutine):
    # Own and close only this call's loop. asyncio.run() would clear a
    # caller-owned policy loop left by pytest-asyncio, orphaning its sockets.
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        return runner.run(coroutine)


def test_owned_runner_preserves_ambient_policy_loop():
    previous_policy = asyncio.get_event_loop_policy()
    policy = asyncio.DefaultEventLoopPolicy()
    retained = policy.new_event_loop()
    policy.set_event_loop(retained)
    asyncio.set_event_loop_policy(policy)
    try:
        assert run_owned_loop(asyncio.sleep(0, result="done")) == "done"
        assert policy.get_event_loop() is retained and not retained.is_closed()
    finally:
        retained.close()
        policy.set_event_loop(None)
        asyncio.set_event_loop_policy(previous_policy)


def setup_app(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    manifest = PilotManifest.from_bytes(json.dumps(make_manifest_data()).encode())
    adapter = HostAdapter(root / "ledger.sqlite3", manifest)
    app = create_app(adapter, bearer_token="test-token-only")
    return adapter, app


def command(manifest):
    return ContainerPilotRequest(
        schema_version="container_pilot_v1", command_id=str(uuid4()), run_id=str(uuid4()),
        target_id="engineering-hub:staging", action="deploy_container",
        manifest_digest=manifest.digest(), candidate_commit=manifest.candidate.source_commit,
        policy_revision=manifest.policy_revision, fencing_generation=1,
    )


async def request(app, method, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test.invalid") as client:
        return await client.request(method, path, headers={"Authorization": "Bearer test-token-only"}, **kwargs)


def test_get_consumes_and_rejects_headerless_nonempty_body(tmp_path):
    adapter, app = setup_app(tmp_path)
    messages = []
    receive_count = 0

    async def receive():
        nonlocal receive_count
        receive_count += 1
        return {"type": "http.request", "body": b"unexpected", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "method": "GET",
        "path": "/v1/container-pilot/commands/" + str(uuid4()),
        "query_string": b"", "headers": [(b"authorization", b"Bearer test-token-only")],
    }
    run_owned_loop(app(scope, receive, send))
    assert messages[0]["status"] == 400
    assert receive_count == 1
    adapter.close()


@pytest.mark.parametrize("path,headers", [
    ("/v1/container-pilot/commands", [
        (b"authorization", b"Bearer test-token-only"),
        (b"content-type", b"application/json"),
        (b"content-length", b"10"),
    ]),
    ("/v1/container-pilot/commands/00000000-0000-4000-8000-000000000000/reconcile", [
        (b"authorization", b"Bearer test-token-only"),
    ]),
], ids=["submit", "reconcile"])
def test_stalled_post_body_is_bounded_by_independent_request_deadline(tmp_path, path, headers):
    adapter, _unused_app = setup_app(tmp_path)
    app = create_app(adapter, bearer_token="test-token-only", request_body_timeout_seconds=0.02)
    scope = {
        "type": "http", "method": "POST", "path": path,
        "query_string": b"", "headers": headers,
    }
    messages = []

    async def receive():
        await asyncio.Future()

    async def send(message):
        messages.append(message)

    run_owned_loop(asyncio.wait_for(app(scope, receive, send), timeout=0.5))
    assert messages[0]["status"] == 408
    adapter.close()


def test_optional_asgi_exact_echo_get_and_bodyless_reconcile(tmp_path):
    adapter, app = setup_app(tmp_path)
    body = command(adapter.manifest).model_dump(mode="json")

    async def flow():
        post = await request(app, "POST", "/v1/container-pilot/commands", json=body)
        assert post.status_code == 200
        response = post.json()
        assert {key: response[key] for key in body} == body
        assert response["state"] == "held"
        assert response["reason"] == "daemon_unavailable"
        assert set(response["evidence"]) == {"container_id", "image_config_id", "health", "evidence_verified"}
        assert response["replayed"] is False
        get = await request(app, "GET", "/v1/container-pilot/commands/" + body["command_id"])
        assert get.status_code == 200 and get.json()["command_id"] == body["command_id"]
        reconcile = await request(app, "POST", "/v1/container-pilot/commands/" + body["command_id"] + "/reconcile")
        assert reconcile.status_code == 200 and reconcile.json()["state"] == "held"
        replay = await request(app, "POST", "/v1/container-pilot/commands", json=body)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        return post, get, reconcile, replay

    results = run_owned_loop(flow())
    assert all("test-token-only" not in result.text for result in results)
    adapter.close()


def test_auth_unknown_duplicate_oversized_and_nonempty_reconcile_are_rejected(tmp_path):
    adapter, app = setup_app(tmp_path)
    body = command(adapter.manifest).model_dump(mode="json")

    async def flow():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test.invalid") as client:
            unauth = await client.post("/v1/container-pilot/commands", json=body)
            assert unauth.status_code == 401 and "test-token-only" not in unauth.text
            bad = dict(body, unreviewed=True)
            unknown = await client.post("/v1/container-pilot/commands", json=bad,
                                        headers={"Authorization": "Bearer test-token-only"})
            assert unknown.status_code == 400
            duplicate_raw = json.dumps(body, separators=(",", ":")).replace(
                '"schema_version":"container_pilot_v1"',
                '"schema_version":"container_pilot_v1","schema_version":"container_pilot_v1"',
            )
            duplicate = await client.post("/v1/container-pilot/commands", content=duplicate_raw,
                                          headers={"Authorization": "Bearer test-token-only",
                                                   "Content-Type": "application/json"})
            assert duplicate.status_code == 400
            oversized = await client.post("/v1/container-pilot/commands", content=b" " * 9000,
                                          headers={"Authorization": "Bearer test-token-only",
                                                   "Content-Type": "application/json"})
            assert oversized.status_code == 413
            reconcile = await client.post(
                "/v1/container-pilot/commands/" + body["command_id"] + "/reconcile",
                content=b"{}", headers={"Authorization": "Bearer test-token-only"},
            )
            assert reconcile.status_code == 400
            missing = await client.get("/v1/container-pilot/commands/" + str(uuid4()),
                                       headers={"Authorization": "Bearer test-token-only"})
            assert missing.status_code == 404

    run_owned_loop(flow())
    adapter.close()
