import json
import threading
import urllib.error
import urllib.request

from hub_executor.http import make_server


TOKEN = "test-token-not-a-production-secret"
TARGET = "3d-viewer:staging"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST_V1 = "sha256:3ab6e82eb9e517ba1b2a107b4d8b838e7e872a4f7a62c068a07b07d4227103ec"
DIGEST_V2 = "sha256:4c031a5fbabf2e39b2aa313c390470863285ffea20396317c6c996a8e1fb8725"


def request(server, path, *, token=None, method="GET", body=None):
    headers = {}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(call, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_command_routes_reject_missing_and_forged_bearer_tokens(tmp_path):
    from hub_executor.core import HubExecutor

    executor = HubExecutor(
        tmp_path / "ledger.sqlite3",
        {"3d-viewer:staging": tmp_path / "target"},
        {},
        candidate_commit="0123456789abcdef0123456789abcdef01234567",
        policy_revision=1,
    )
    server = make_server(executor, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        missing_status, missing = request(server, "/v1/commands")
        forged_status, forged = request(server, "/v1/commands", token="forged-token")
        non_ascii_status, non_ascii = request(server, "/v1/commands", token="forgé")
        health_status, health = request(server, "/health")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert missing_status == forged_status == non_ascii_status == 401
    assert missing == forged == non_ascii == {"error": {"code": "unauthorized"}}
    assert health_status == 200
    assert health == {"ok": True}


def test_command_schema_rejects_unknown_request_fields(tmp_path):
    from hub_executor.core import HubExecutor

    executor = HubExecutor(
        tmp_path / "ledger.sqlite3",
        {"3d-viewer:staging": tmp_path / "target"},
        {},
        candidate_commit="0123456789abcdef0123456789abcdef01234567",
        policy_revision=1,
    )
    server = make_server(executor, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, response = request(
            server,
            "/v1/commands",
            token=TOKEN,
            method="POST",
            body={"command_id": "550e8400-e29b-41d4-a716-446655440000", "extra": "x"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert status == 400
    assert response == {"error": {"code": "invalid_payload"}}


def test_typed_command_replay_returns_durable_result_without_second_effect(tmp_path):
    from hub_executor.core import HubExecutor

    target_root = tmp_path / "targets" / "3d-viewer-staging"
    executor = HubExecutor(
        tmp_path / "ledger.sqlite3",
        {TARGET: target_root},
        {DIGEST_V2: b"engineering hub fixture v2\n"},
        candidate_commit=COMMIT,
        policy_revision=1,
    )
    server = make_server(executor, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {
        "command_id": "550e8400-e29b-41d4-a716-446655440000",
        "target_id": TARGET,
        "action": "deploy_fixture",
        "artifact_digest": DIGEST_V2,
        "candidate_commit": COMMIT,
        "policy_revision": 1,
        "fencing_generation": 1,
    }
    try:
        first_status, first = request(
            server, "/v1/commands", token=TOKEN, method="POST", body=payload
        )
        replay_status, replay = request(
            server, "/v1/commands", token=TOKEN, method="POST", body=payload
        )
        state = json.loads((target_root / "state.json").read_text(encoding="utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert first_status == 201
    assert replay_status == 200
    assert first["state"] == "succeeded"
    assert first["reason"] is None
    assert isinstance(first["observed"], dict)
    assert set(first) == set(payload) | {"state", "reason", "observed"}
    assert type(replay["replayed"]) is bool and replay["replayed"] is True
    assert set(replay) == set(first) | {"replayed"}
    assert {key: value for key, value in replay.items() if key != "replayed"} == first
    assert state["effect_count"] == 1


def test_pause_route_uses_exact_boolean_schema_and_persists_admission_state(tmp_path):
    from hub_executor.core import HubExecutor

    executor = HubExecutor(
        tmp_path / "ledger.sqlite3",
        {TARGET: tmp_path / "target"},
        {},
        candidate_commit=COMMIT,
        policy_revision=1,
    )
    server = make_server(executor, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, paused = request(
            server, "/v1/pause", token=TOKEN, method="POST", body={"paused": True}
        )
        invalid_status, invalid = request(
            server, "/v1/pause", token=TOKEN, method="POST", body={"paused": 1}
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert status == 200
    assert paused == {"paused": True}
    assert invalid_status == 400
    assert invalid == {"error": {"code": "invalid_payload"}}
    assert executor.is_paused() is True


def test_shell_path_url_and_nonstring_actions_are_rejected_without_effect(tmp_path):
    from hub_executor.core import HubExecutor

    target_root = tmp_path / "target"
    executor = HubExecutor(
        tmp_path / "ledger.sqlite3",
        {TARGET: target_root},
        {DIGEST_V1: b"engineering hub fixture v1\n"},
        candidate_commit=COMMIT,
        policy_revision=1,
    )
    server = make_server(executor, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = {
        "command_id": "550e8400-e29b-41d4-a716-446655440010",
        "target_id": TARGET,
        "action": "deploy_fixture",
        "artifact_digest": DIGEST_V1,
        "candidate_commit": COMMIT,
        "policy_revision": 1,
        "fencing_generation": 1,
    }
    rejected = [
        {**base, "target_id": "../tmp"},
        {**base, "target_id": "https://example.invalid"},
        {**base, "target_id": "sh -c touch /tmp/owned"},
        {**base, "action": "rm -rf /"},
        {**base, "action": ["deploy_fixture"]},
        {**base, "artifact_digest": "file:///etc/passwd"},
    ]
    try:
        responses = [
            request(server, "/v1/commands", token=TOKEN, method="POST", body=payload)
            for payload in rejected
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert [status for status, _ in responses] == [400] * len(rejected)
    assert all(response == {"error": {"code": "invalid_payload"}} for _, response in responses)
    assert not (target_root / "state.json").exists()


def test_status_get_and_reconcile_are_inspect_only_after_interruption(tmp_path):
    from hub_executor.core import HubExecutor

    class SimulatedInterruption(RuntimeError):
        pass

    class InterruptAfterEffect(HubExecutor):
        def _apply_fixture(self, command):
            super()._apply_fixture(command)
            raise SimulatedInterruption("fixture interruption after atomic effect")

    target_root = tmp_path / "target"
    options = {
        "database_path": tmp_path / "ledger.sqlite3",
        "target_roots": {TARGET: target_root},
        "artifacts": {DIGEST_V1: b"engineering hub fixture v1\n"},
        "candidate_commit": COMMIT,
        "policy_revision": 1,
    }
    interrupted = InterruptAfterEffect(**options)
    payload = {
        "command_id": "550e8400-e29b-41d4-a716-446655440020",
        "target_id": TARGET,
        "action": "deploy_fixture",
        "artifact_digest": DIGEST_V1,
        "candidate_commit": COMMIT,
        "policy_revision": 1,
        "fencing_generation": 1,
    }
    try:
        interrupted.execute_command(payload)
    except SimulatedInterruption:
        pass
    else:
        raise AssertionError("failure fixture did not interrupt after its filesystem effect")

    state_path = target_root / "state.json"
    actual_before = state_path.read_bytes()
    restarted = HubExecutor(**options)
    server = make_server(restarted, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    command_path = f"/v1/commands/{payload['command_id']}"
    try:
        get_status, held = request(server, command_path, token=TOKEN)
        reconcile_status, reconciled = request(
            server, f"{command_path}/reconcile", token=TOKEN, method="POST"
        )
        final_status, final = request(server, command_path, token=TOKEN)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert get_status == reconcile_status == final_status == 200
    assert held["state"] == "held"
    assert reconciled["state"] == final["state"] == "succeeded"
    assert reconciled["observed"]["marker"]["command_id"] == payload["command_id"]
    assert state_path.read_bytes() == actual_before
    assert json.loads(actual_before)["effect_count"] == 1


def test_fixture_manifest_and_fence_errors_match_wire_contract(tmp_path):
    from hub_executor.fixtures import (
        FIXTURE_CANDIDATE_COMMIT,
        FIXTURE_CANDIDATE_DIGEST,
        FIXTURE_ROLLBACK_COMMIT,
        FIXTURE_ROLLBACK_DIGEST,
        FIXTURE_TARGET,
        make_fixture_executor,
    )

    executor = make_fixture_executor(tmp_path)
    server = make_server(executor, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = {
        "command_id": "42a55e0f-443c-5b68-a342-94cebbe799cf",
        "target_id": FIXTURE_TARGET,
        "action": "deploy_fixture",
        "artifact_digest": FIXTURE_CANDIDATE_DIGEST,
        "candidate_commit": FIXTURE_CANDIDATE_COMMIT,
        "policy_revision": 1,
        "fencing_generation": 2,
    }
    try:
        created_status, _ = request(
            server, "/v1/commands", token=TOKEN, method="POST", body=base
        )
        conflict_status, conflict = request(
            server,
            "/v1/commands",
            token=TOKEN,
            method="POST",
            body={
                **base,
                "action": "rollback_fixture",
                "artifact_digest": FIXTURE_ROLLBACK_DIGEST,
                "candidate_commit": FIXTURE_ROLLBACK_COMMIT,
            },
        )
        stale_status, stale = request(
            server,
            "/v1/commands",
            token=TOKEN,
            method="POST",
            body={**base, "command_id": "550e8400-e29b-41d4-a716-446655440030", "fencing_generation": 1},
        )
        target_status, target_error = request(
            server,
            "/v1/commands",
            token=TOKEN,
            method="POST",
            body={**base, "command_id": "550e8400-e29b-41d4-a716-446655440031", "target_id": "other:staging", "fencing_generation": 2},
        )
        policy_status, policy_error = request(
            server,
            "/v1/commands",
            token=TOKEN,
            method="POST",
            body={**base, "command_id": "550e8400-e29b-41d4-a716-446655440032", "candidate_commit": FIXTURE_ROLLBACK_COMMIT, "fencing_generation": 2},
        )
        absent_status, absent = request(
            server, "/v1/commands/550e8400-e29b-41d4-a716-446655440099", token=TOKEN
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert created_status == 201
    assert conflict_status == stale_status == policy_status == 409
    assert conflict == {"error": {"code": "command_conflict"}}
    assert stale == {"error": {"code": "stale_fence"}}
    assert policy_error == {"error": {"code": "policy_conflict"}}
    assert target_status == 403
    assert target_error == {"error": {"code": "target_or_artifact_not_allowed"}}
    assert absent_status == 404
    assert absent == {"error": {"code": "command_not_found"}}


def test_unsupported_methods_remain_authenticated_and_return_json(tmp_path):
    from hub_executor.core import HubExecutor

    executor = HubExecutor(
        tmp_path / "ledger.sqlite3",
        {TARGET: tmp_path / "target"},
        {},
        candidate_commit=COMMIT,
        policy_revision=1,
    )
    server = make_server(executor, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        unauth_status, unauth = request(
            server, "/v1/commands", method="PUT"
        )
        auth_status, auth = request(
            server, "/v1/commands", token=TOKEN, method="PUT"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert unauth_status == 401 and unauth == {"error": {"code": "unauthorized"}}
    assert auth_status == 404 and auth == {"error": {"code": "command_not_found"}}
