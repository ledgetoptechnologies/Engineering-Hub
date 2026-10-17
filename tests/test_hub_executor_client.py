import json

import httpx
import pytest
from pydantic import ValidationError

from engineering_hub.executor_client import (
    CommandRequest, ExecutorClient, ExecutorUnavailable, HeldProtocolError, UnknownOutcome,
)


SECRET = "fixture-executor-service-secret"
COMMAND_ID = "44048f2e-fd35-4490-bb39-a10a37f765ed"


def command(**changes):
    return CommandRequest(**({
        "command_id": COMMAND_ID, "target_id": "3d-viewer:staging",
        "action": "deploy_fixture", "artifact_digest": "sha256:" + "a" * 64,
        "candidate_commit": "b" * 40, "policy_revision": 1, "fencing_generation": 1,
    } | changes))


def result(request, **changes):
    return command().model_dump() | {
        "state": "succeeded", "reason": None, "observed": {"fixture_healthy": True},
    } | changes


@pytest.mark.parametrize("field,value", [
    ("command_id", "../../commands"), ("command_id", "not-a-uuid"),
    ("command_id", COMMAND_ID.upper()), ("target_id", "3d-viewer:production"),
    ("target_id", "../3d-viewer:staging"), ("action", "shell"),
    ("artifact_digest", "latest"), ("candidate_commit", "main"),
    ("policy_revision", True), ("fencing_generation", False),
    ("policy_revision", "1"), ("fencing_generation", 1.0),
    ("policy_revision", 0), ("fencing_generation", -1), ("actor", "hermes"),
])
def test_strict_command_fields_reject_unsafe_or_coerced_values(field, value):
    with pytest.raises(ValidationError):
        command(**{field: value})


@pytest.mark.parametrize("origin", [
    "http://192.168.50.80:9876", "http://executor.example", "file:///tmp/executor",
    "https://user:secret@executor.test", "https://executor.test/path",
    "https://executor.test?token=secret", "https://executor.test#fragment",
    "https://executor.test:bad", "https://", "",
])
def test_executor_origin_policy_rejects_nonlocal_cleartext_and_embedded_secrets(origin):
    with pytest.raises(ValueError) as error:
        ExecutorClient(origin, SECRET)
    assert SECRET not in str(error.value)
    if origin:
        assert origin not in str(error.value)


@pytest.mark.parametrize("origin", ["https://executor.test", "http://localhost:9876", "http://127.0.0.1:9876", "http://[::1]:9876"])
async def test_https_and_loopback_http_allowed_with_explicit_mock_transport(origin):
    async with ExecutorClient(origin, SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True})
    )) as client:
        assert (await client.health()).ok is True
        assert SECRET not in repr(client)


@pytest.mark.parametrize("timeout", [0, -1, True, 61, "15", float("inf"), float("nan")])
def test_timeouts_are_strict_positive_and_bounded(timeout):
    with pytest.raises(ValueError):
        ExecutorClient("https://executor.test", SECRET, timeout_seconds=timeout)


async def test_typed_command_contract_auth_echo_inspect_and_pause():
    received = []

    def handler(request):
        received.append(request)
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        assert request.url.host == "executor.test"
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v1/pause":
            assert json.loads(request.content) == {"paused": True}
            return httpx.Response(200, json={"paused": True})
        if request.url.path == "/v1/commands":
            assert request.method == "POST"
            assert json.loads(request.content) == command().model_dump()
        else:
            assert not request.content  # inspect/reconcile never resubmit work.
        return httpx.Response(200, json=result(request))

    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(handler)) as client:
        for response in (await client.submit_command(command()), await client.get_command(command()),
                         await client.reconcile_command(command())):
            assert response.command_id == COMMAND_ID
            assert response.state == "succeeded" and response.observed == {"fixture_healthy": True}
        assert await client.pause(True) is True
        assert (await client.health()).ok is True
    assert [(request.method, request.url.path) for request in received] == [
        ("POST", "/v1/commands"), ("GET", f"/v1/commands/{COMMAND_ID}"),
        ("POST", f"/v1/commands/{COMMAND_ID}/reconcile"), ("POST", "/v1/pause"), ("GET", "/health"),
    ]


async def test_timeout_after_acceptance_never_retries_or_invents_new_command():
    accepted = []

    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/commands":
            accepted.append(json.loads(request.content))
            raise httpx.ReadTimeout(f"accepted-but-lost-response {SECRET}", request=request)
        return httpx.Response(200, json=result(request, state="held", reason="inspect_only"))

    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnknownOutcome) as error:
            await client.submit_command(command())
        assert error.value.command_id == COMMAND_ID
        assert SECRET not in str(error.value) and SECRET not in repr(error.value)
        assert error.value.__suppress_context__ is True
        assert len(accepted) == 1
        assert (await client.get_command(command())).state == "held"
        assert (await client.reconcile_command(command())).state == "held"
        assert len(accepted) == 1


@pytest.mark.parametrize("field,value", [
    ("command_id", "c809d4e4-97a6-4cad-8daa-2bbc7305d966"),
    ("target_id", "treedar:staging"), ("action", "rollback_fixture"),
    ("artifact_digest", "sha256:" + "c" * 64), ("candidate_commit", "d" * 40),
    ("policy_revision", 2), ("fencing_generation", 2),
    ("policy_revision", True), ("state", "finished"), ("unknown", SECRET),
])
async def test_mismatched_echo_or_invalid_response_is_held(field, value):
    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=result(request, **{field: value}))
    )) as client:
        with pytest.raises(HeldProtocolError) as error:
            await client.submit_command(command())
        assert error.value.command_id == COMMAND_ID and error.value.state == "held"
        assert SECRET not in str(error.value)
        if isinstance(value, str):
            assert value not in str(error.value)


@pytest.mark.parametrize("status", [301, 307, 401, 403, 409, 500, 503])
async def test_status_errors_and_redirects_never_reflect_credentials_or_follow_location(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": SECRET}, headers={"Location": f"https://evil.test/{SECRET}"})

    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnknownOutcome) as error:
            await client.submit_command(command())
        assert SECRET not in str(error.value) and "evil.test" not in str(error.value)
        assert len(calls) == 1


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b"null", b"{}"])
async def test_malformed_response_is_safely_held(payload):
    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload)
    )) as client:
        with pytest.raises(HeldProtocolError):
            await client.submit_command(command())


async def test_reflected_service_secret_never_returns_in_success_body():
    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=result(request, observed={"headers": SECRET}))
    )) as client:
        with pytest.raises(HeldProtocolError) as error:
            await client.submit_command(command())
        assert SECRET not in str(error.value)


async def test_escaped_reflected_service_secret_does_not_bypass_validation():
    def handler(request):
        body = json.dumps(result(request, observed={"headers": SECRET}))
        body = body.replace(SECRET, "".join(f"\\u{ord(character):04x}" for character in SECRET))
        assert SECRET not in body
        return httpx.Response(200, content=body)

    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HeldProtocolError):
            await client.submit_command(command())


async def test_existing_command_inspect_allows_strict_replayed_flag():
    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=result(request, replayed=True))
    )) as client:
        returned = await client.get_command(command())
        assert returned.replayed is True
        assert returned.model_dump()["command_id"] == COMMAND_ID


@pytest.mark.parametrize("replayed", [1, "true", None])
async def test_replayed_flag_never_coerces_values(replayed):
    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=result(request, replayed=replayed))
    )) as client:
        with pytest.raises(HeldProtocolError):
            await client.get_command(command())


@pytest.mark.parametrize("token", ["", "line\nbreak", "carriage\rreturn", "space token", "\x00", "private-\u2603"])
def test_invalid_credential_rejected_without_echo(token):
    with pytest.raises(ValueError) as error:
        ExecutorClient("https://executor.test", token)
    assert str(error.value) == "invalid executor credential"


@pytest.mark.parametrize("payload", [{"paused": 1}, {"paused": "true"}, {"paused": False}, {"paused": True, "extra": "unexpected"}])
async def test_pause_response_strict_and_exact(payload):
    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )) as client:
        with pytest.raises(ExecutorUnavailable):
            await client.pause(True)


async def test_http_client_safety_settings_are_explicit(monkeypatch):
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    ExecutorClient("https://executor.test", SECRET, timeout_seconds=12)
    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False
    assert captured["timeout"].connect == 5
    assert captured["timeout"].read == captured["timeout"].write == captured["timeout"].pool == 12


@pytest.mark.parametrize("paused", [1, 0, "true", None])
async def test_pause_rejects_nonboolean_before_network(paused):
    def handler(request):
        raise AssertionError("must not contact executor")

    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await client.pause(paused)


async def test_health_and_pause_errors_sanitized():
    def handler(request):
        raise httpx.ConnectError(f"private endpoint error {SECRET}", request=request)

    async with ExecutorClient("https://executor.test", SECRET, transport=httpx.MockTransport(handler)) as client:
        for action in (client.health, lambda: client.pause(True)):
            with pytest.raises(ExecutorUnavailable) as error:
                await action()
            assert SECRET not in str(error.value)
