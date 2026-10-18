"""Mock transport unit coverage, not actual container-pilot acceptance."""
import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from engineering_hub.container_pilot_client import (
    ContainerPilotClient, ContainerPilotEvidence, ContainerPilotRequest,
    ContainerPilotResponse, HeldProtocolError, UnknownOutcome,
)

SECRET = "synthetic-container-pilot-secret"
COMMAND_ID = "44048f2e-fd35-4490-bb39-a10a37f765ed"
RUN_ID = "7bdf82d0-6388-41cf-bcc7-aef3838a7e59"


def command(**changes):
    return ContainerPilotRequest(**({
        "schema_version": "container_pilot_v1", "command_id": COMMAND_ID,
        "run_id": RUN_ID, "target_id": "engineering-hub:staging",
        "action": "deploy_container", "manifest_digest": "sha256:" + "a" * 64,
        "candidate_commit": "b" * 40, "policy_revision": 1, "fencing_generation": 1,
    } | changes))


def result(**changes):
    return command().model_dump() | {
        "state": "succeeded", "reason": None, "replayed": False,
        "evidence": {"container_id": "c" * 64, "image_config_id": "sha256:" + "d" * 64,
                     "health": "healthy", "evidence_verified": True},
    } | changes


@pytest.mark.parametrize("field,value", [
    ("schema_version", "fixture_v1"), ("schema_version", True),
    ("command_id", COMMAND_ID.upper()), ("command_id", "../../commands"),
    ("run_id", RUN_ID.upper()), ("run_id", "not-a-uuid"),
    ("target_id", "engineering-hub:production"), ("target_id", "3d-viewer:staging"),
    ("action", "deploy_fixture"), ("action", "shell"),
    ("manifest_digest", "latest"), ("manifest_digest", "sha256:" + "A" * 64),
    ("candidate_commit", "main"), ("candidate_commit", "B" * 40),
    ("policy_revision", True), ("policy_revision", 0), ("policy_revision", 1.0),
    ("fencing_generation", False), ("fencing_generation", "1"),
    ("fencing_generation", 2**63), ("fencing_generation", -1), ("image", "latest"),
])
def test_request_rejects_unsafe_or_coerced_values(field, value):
    with pytest.raises(ValidationError):
        command(**{field: value})


@pytest.mark.parametrize("action", ["deploy_container", "smoke_container", "rollback_container", "smoke_container_rollback"])
def test_four_separate_actions_and_maximum_fence(action):
    assert command(action=action, fencing_generation=2**63 - 1).action == action


def test_models_frozen_including_nested_evidence():
    response = ContainerPilotResponse.model_validate(result())
    with pytest.raises(ValidationError):
        response.run_id = COMMAND_ID
    with pytest.raises(ValidationError):
        response.evidence.health = "unknown"


@pytest.mark.parametrize("changes", [
    {"container_id": "short"}, {"container_id": "C" * 64},
    {"image_config_id": "latest"}, {"health": "maybe"},
    {"evidence_verified": 1}, {"raw_output": SECRET},
])
def test_evidence_is_typed_not_arbitrary_docker_output(changes):
    with pytest.raises(ValidationError):
        ContainerPilotEvidence.model_validate(result()["evidence"] | changes)


@pytest.mark.parametrize("origin", [
    "http://192.168.50.80:9876", "http://localhost:9876", "http://127.1:9876",
    "https://user:secret@executor.test", "https://executor.test/path",
    "https://executor.test?token=secret", "https://executor.test#fragment",
    "https://executor.test:bad", "https://", "file:///tmp/executor", "",
    " https://executor.test", "https://executor.test\n", "https://executor.test\\path",
])
def test_origin_policy_and_safe_errors(origin):
    with pytest.raises(ValueError) as error:
        ContainerPilotClient(origin, SECRET)
    assert SECRET not in str(error.value)
    if origin:
        assert origin not in str(error.value)


@pytest.mark.parametrize("timeout", [0, -1, True, 126, "15", float("inf"), float("nan")])
def test_bounded_timeout(timeout):
    with pytest.raises(ValueError):
        ContainerPilotClient("https://executor.test", SECRET, timeout_seconds=timeout)


@pytest.mark.parametrize('timeout', [None, 125, 120.001, 61])
async def test_manifest_maximum_transport_deadline_is_bounded_and_explicit(timeout):
    kwargs = {} if timeout is None else {'timeout_seconds': timeout}
    async with ContainerPilotClient('https://executor.test', SECRET, **kwargs) as client:
        assert client.timeout_seconds == (125.0 if timeout is None else timeout)
        with pytest.raises(AttributeError):
            client.timeout_seconds = 1


@pytest.mark.parametrize("secret", ["", "has space", "line\nbreak", "\x7f", None])
def test_invalid_credentials(secret):
    with pytest.raises(ValueError):
        ContainerPilotClient("https://executor.test", secret)


@pytest.mark.parametrize("origin", ["https://executor.test", "http://127.0.0.1:9876", "http://[::1]:9876"])
async def test_explicit_operations_auth_echo_and_no_body_on_inspection(origin):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        if request.url.path.endswith("/commands"):
            assert json.loads(request.content) == command().model_dump()
        else:
            assert request.content == b""
        return httpx.Response(200, json=result())

    async with ContainerPilotClient(origin, SECRET, transport=httpx.MockTransport(handler)) as client:
        assert SECRET not in repr(client)
        for response in (await client.submit_command(command()), await client.get_command(command()),
                         await client.reconcile_command(command())):
            assert response.run_id == RUN_ID
    assert seen == [
        ("POST", "/v1/container-pilot/commands"),
        ("GET", f"/v1/container-pilot/commands/{COMMAND_ID}"),
        ("POST", f"/v1/container-pilot/commands/{COMMAND_ID}/reconcile"),
    ]


@pytest.mark.parametrize("field,value", [
    ("schema_version", "different"), ("command_id", RUN_ID), ("run_id", COMMAND_ID),
    ("target_id", "3d-viewer:staging"), ("action", "smoke_container"),
    ("manifest_digest", "sha256:" + "e" * 64), ("candidate_commit", "f" * 40),
    ("policy_revision", 2), ("fencing_generation", 2), ("replayed", 1),
    ("reason", "raw exception text"), ("state", "complete"), ("raw_output", SECRET),
])
async def test_entire_echo_and_response_contract_verified(field, value):
    async with ContainerPilotClient("https://executor.test", SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=result(**{field: value}))
    )) as client:
        with pytest.raises(HeldProtocolError) as error:
            await client.submit_command(command())
        assert error.value.command_id == COMMAND_ID and error.value.state == "held"
        assert SECRET not in str(error.value)


@pytest.mark.parametrize("status", [301, 307, 401, 403, 409, 500, 503])
async def test_error_status_never_follows_redirect_or_retries(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=SECRET, headers={"Location": "https://other.test/" + SECRET})

    async with ContainerPilotClient("https://executor.test", SECRET, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnknownOutcome) as error:
            await client.submit_command(command())
        assert error.value.command_id == COMMAND_ID
        assert SECRET not in str(error.value) and "other.test" not in str(error.value)
    assert len(calls) == 1


async def test_ambiguous_post_retains_identity_and_never_resubmits():
    posts = []

    def handler(request):
        if request.url.path.endswith("/commands"):
            posts.append(json.loads(request.content))
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(200, json=result(state="held", reason="inspect_required"))

    async with ContainerPilotClient("https://executor.test", SECRET, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnknownOutcome) as error:
            await client.submit_command(command())
        assert error.value.command_id == COMMAND_ID
        assert error.value.__suppress_context__ and SECRET not in str(error.value)
        assert (await client.get_command(command())).state == "held"
        assert (await client.reconcile_command(command())).state == "held"
    assert posts == [command().model_dump()]


@pytest.mark.parametrize("payload", [
    b"not-json", b"[]", b"null", b"{}", b"\xff", b"{" + b" " * (16 * 1024),
    json.dumps(result()).replace('"replayed": false', '"replayed": false, "replayed": false').encode(),
    json.dumps(result()).replace('"health": "healthy"', '"health": "healthy", "health": "healthy"').encode(),
    json.dumps(result()).replace('"reason": null', '"reason": "' + SECRET + '"').encode(),
    json.dumps(result()).replace('"reason": null', '"reason": "' + ''.join('\\u%04x' % ord(c) for c in SECRET) + '"').encode(),
])
async def test_bounded_strict_json_and_no_credential_reflection(payload):
    async with ContainerPilotClient("https://executor.test", SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=payload)
    )) as client:
        with pytest.raises(HeldProtocolError) as error:
            await client.submit_command(command())
        assert SECRET not in str(error.value)


async def test_stream_budget_stops_consumption_and_closes_response():
    class OversizedStream(httpx.AsyncByteStream):
        reads = 0
        closed = False

        async def __aiter__(self):
            for _ in range(100):
                self.reads += 1
                yield b" " * 1024

        async def aclose(self):
            self.closed = True

    stream = OversizedStream()
    async with ContainerPilotClient("https://executor.test", SECRET, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream)
    )) as client:
        with pytest.raises(HeldProtocolError):
            await client.submit_command(command())
    assert stream.reads == 17 and stream.closed


async def test_environment_proxy_is_not_loaded(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("environment proxies must not be consulted")

    monkeypatch.setattr(httpx._client, "get_environment_proxies", forbidden)
    async with ContainerPilotClient("https://executor.test", SECRET) as client:
        assert SECRET not in repr(client)


async def test_total_operation_deadline_even_for_slow_drip_response():
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.002)
                yield b" "

        async def aclose(self):
            self.closed = True

    stream = SlowStream()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)

    async with ContainerPilotClient("https://executor.test", SECRET, timeout_seconds=0.03,
                                    transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnknownOutcome) as error:
            await client.submit_command(command())
        assert error.value.command_id == COMMAND_ID
    assert len(calls) == 1 and stream.closed
