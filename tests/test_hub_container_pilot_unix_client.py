"""Private Linux HTTP/UDS fixtures; no real Docker or installed host service."""
import asyncio
from contextlib import asynccontextmanager
import inspect
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile

import pytest

if sys.platform != "linux":
    pytest.skip("Private Unix pilot transport requires Linux", allow_module_level=True)

import httpx
from starlette.responses import JSONResponse, Response

from engineering_hub.container_pilot_client import (
    ContainerPilotClient, ContainerPilotRequest, HeldProtocolError, UnknownOutcome,
)
from engineering_hub.private_browser_socket import private_browser_socket
from engineering_hub.server import _listener


TOKEN = "synthetic-private-pilot-token"
COMMAND_ID = "44048f2e-fd35-4490-bb39-a10a37f765ed"
RUN_ID = "7bdf82d0-6388-41cf-bcc7-aef3838a7e59"


def command():
    return ContainerPilotRequest(schema_version="container_pilot_v1", command_id=COMMAND_ID,
        run_id=RUN_ID, target_id="engineering-hub:staging", action="deploy_container",
        manifest_digest="sha256:" + "a" * 64, candidate_commit="b" * 40,
        policy_revision=1, fencing_generation=1)


def result(**changes):
    return command().model_dump() | dict(state="succeeded", reason=None, replayed=False,
        evidence=dict(container_id="c" * 64, image_config_id="sha256:" + "d" * 64,
                      health="healthy", evidence_verified=True)) | changes


@pytest.fixture
def endpoint():
    with tempfile.TemporaryDirectory(prefix="eh-pilot-client-", dir="/tmp") as directory:
        yield Path(directory) / "executor.sock"


@asynccontextmanager
async def served(endpoint, app):
    with private_browser_socket(endpoint) as listener:
        server = _listener(app)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                        pytest.fail("Fixture server stopped before startup")
                    await asyncio.sleep(.001)
            yield
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
    assert listener.fileno() == -1 and not endpoint.exists()


async def received_body(receive):
    value = bytearray()
    while True:
        event = await receive()
        assert event["type"] == "http.request"
        value.extend(event.get("body", b""))
        if not event.get("more_body", False):
            return bytes(value)


async def test_real_unix_operations_preserve_token_request_identity_and_inspection(endpoint, monkeypatch):
    calls = []
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    async def app(scope, receive, send):
        headers = dict(scope["headers"])
        assert headers[b"authorization"] == ("Bearer " + TOKEN).encode()
        assert headers[b"host"] == b"127.0.0.1"
        body = await received_body(receive)
        calls.append((scope["method"], scope["path"], body))
        await JSONResponse(result())(scope, receive, send)

    async with served(endpoint, app):
        async with ContainerPilotClient.for_private_unix(endpoint, TOKEN) as client:
            assert client.timeout_seconds == 125.0 and TOKEN not in repr(client)
            assert type(client) is ContainerPilotClient
            for response in (await client.submit_command(command()), await client.get_command(command()),
                             await client.reconcile_command(command())):
                assert response.command_id == COMMAND_ID and response.run_id == RUN_ID
            inner = client._ContainerPilotClient__client
        assert inner.is_closed
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/v1/container-pilot/commands"),
        ("GET", "/v1/container-pilot/commands/" + COMMAND_ID),
        ("POST", "/v1/container-pilot/commands/" + COMMAND_ID + "/reconcile"),
    ]
    assert json.loads(calls[0][2]) == command().model_dump()
    assert calls[1][2] == calls[2][2] == b""


async def test_fresh_inert_transport_has_no_retry_proxy_or_injected_hook(endpoint, monkeypatch):
    original, transports, options = httpx.AsyncHTTPTransport, [], []
    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        options.append((args, kwargs))
        transports.append(value)
        return value
    with private_browser_socket(endpoint):
        monkeypatch.setattr(httpx, "AsyncHTTPTransport", capture)
        first = ContainerPilotClient.for_private_unix(endpoint, TOKEN)
        second = ContainerPilotClient.for_private_unix(endpoint, TOKEN)
        try:
            assert len(transports) == 2 and transports[0] is not transports[1]
            assert options == [((), dict(uds=str(endpoint), retries=0, trust_env=False))] * 2
            for client in (first, second):
                inner = client._ContainerPilotClient__client
                assert not inner.follow_redirects and not inner._trust_env
                assert inner.event_hooks == {"request": [], "response": []}
            signature = inspect.signature(ContainerPilotClient.for_private_unix)
            assert set(signature.parameters) == {"path", "service_token", "timeout_seconds"}
        finally:
            await first.aclose()
            await second.aclose()


@pytest.mark.parametrize("status", [301, 307, 401, 403, 409, 500])
async def test_real_unix_http_errors_do_not_redirect_retry_or_reflect_secrets(endpoint, status):
    calls = []
    async def app(scope, receive, send):
        calls.append(scope["path"])
        await received_body(receive)
        await Response(TOKEN, status_code=status,
                       headers={"Location": "http://127.0.0.1:9/" + TOKEN})(scope, receive, send)
    async with served(endpoint, app):
        async with ContainerPilotClient.for_private_unix(endpoint, TOKEN, timeout_seconds=1) as client:
            with pytest.raises(UnknownOutcome) as error:
                await client.submit_command(command())
            assert error.value.command_id == COMMAND_ID
            assert TOKEN not in str(error.value) and str(endpoint) not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["identity", "oversized", "duplicate-json", "secret-reflection"])
async def test_real_unix_response_identity_and_byte_bound_remain_fail_closed(endpoint, kind):
    if kind == "identity":
        raw = json.dumps(result(run_id=COMMAND_ID)).encode()
    elif kind == "oversized":
        raw = b"x" * (ContainerPilotClient.RESPONSE_BYTE_LIMIT + 1)
    elif kind == "duplicate-json":
        raw = json.dumps(result()).encode()[:-1] + b',"state":"succeeded"}'
    else:
        raw = json.dumps(result(reason=TOKEN)).encode()
    async def app(scope, receive, send):
        await received_body(receive)
        await Response(raw, media_type="application/json")(scope, receive, send)
    async with served(endpoint, app):
        async with ContainerPilotClient.for_private_unix(endpoint, TOKEN, timeout_seconds=1) as client:
            with pytest.raises(HeldProtocolError) as error:
                await client.submit_command(command())
            assert error.value.command_id == COMMAND_ID and error.value.state == "held"
            assert TOKEN not in str(error.value)


async def test_real_unix_whole_deadline_not_renewed_by_response_chunks(endpoint):
    calls = []
    async def app(scope, receive, send):
        calls.append(scope["path"])
        await received_body(receive)
        await send(dict(type="http.response.start", status=200,
                        headers=[(b"content-type", b"application/json")]))
        for part in (b'{"schema_version":', b'"container_pilot_v1",', b'"state":"succeeded"}'):
            await asyncio.sleep(.04)
            await send(dict(type="http.response.body", body=part, more_body=True))
        await send(dict(type="http.response.body", body=b"", more_body=False))
    async with served(endpoint, app):
        async with ContainerPilotClient.for_private_unix(endpoint, TOKEN, timeout_seconds=.09) as client:
            with pytest.raises(UnknownOutcome):
                await client.submit_command(command())
    assert len(calls) == 1


async def test_stale_endpoint_is_unknown_without_tcp_fallback_or_unlink(endpoint):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(endpoint))
        endpoint.chmod(0o600)
    before = endpoint.lstat()
    async with ContainerPilotClient.for_private_unix(endpoint, TOKEN, timeout_seconds=.2) as client:
        with pytest.raises(UnknownOutcome):
            await client.submit_command(command())
    assert endpoint.lstat().st_ino == before.st_ino


@pytest.mark.parametrize("kind", ["missing", "regular", "symlink", "parent-symlink", "weak-parent", "weak-socket", "writable-ancestor"])
def test_invalid_paths_are_rejected_without_repair_or_unlink(endpoint, kind):
    target, owned = endpoint, None
    if kind in {"weak-socket", "weak-parent"}:
        owned = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        owned.bind(str(endpoint))
        endpoint.chmod(0o666 if kind == "weak-socket" else 0o600)
        if kind == "weak-parent":
            endpoint.parent.chmod(0o755)
    elif kind == "regular":
        endpoint.write_bytes(b"fixture")
    elif kind == "symlink":
        endpoint.symlink_to(endpoint.parent / "missing")
    elif kind == "parent-symlink":
        parent = endpoint.parent / "actual"
        parent.mkdir(mode=0o700)
        alias = endpoint.parent / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        target = alias / endpoint.name
    elif kind == "writable-ancestor":
        outer = endpoint.parent / "shared"
        outer.mkdir(mode=0o777)
        outer.chmod(0o777)
        inner = outer / "private"
        inner.mkdir(mode=0o700)
        target = inner / endpoint.name
    before = endpoint.lstat() if endpoint.is_symlink() or endpoint.exists() else None
    try:
        with pytest.raises(ValueError, match="^invalid private container pilot socket$"):
            ContainerPilotClient.for_private_unix(target, TOKEN)
        if before:
            after = endpoint.lstat()
            assert (before.st_ino, before.st_mode) == (after.st_ino, after.st_mode)
    finally:
        if owned is not None:
            owned.close()
        if kind == "weak-parent":
            endpoint.parent.chmod(0o700)  # Only our synthetic fixture cleanup.


@pytest.mark.parametrize("path", ["relative.sock", "@abstract", "/tmp/../executor.sock", "/tmp/a\x00b", "/tmp/" + "a" * 108])
def test_noncanonical_abstract_or_oversized_paths_fail_sanitized(path):
    with pytest.raises(ValueError, match="^invalid private container pilot socket$"):
        ContainerPilotClient.for_private_unix(path, TOKEN)


@pytest.mark.parametrize("secret,timeout", [("", 1), ("bad secret", 1), (TOKEN, 0),
                                          (TOKEN, True), (TOKEN, float("nan")), (TOKEN, 126)])
def test_invalid_bearer_or_budget_denied_before_transport_allocation(endpoint, monkeypatch, secret, timeout):
    def forbidden(*args, **kwargs):
        pytest.fail("Transport allocated for invalid client inputs")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", forbidden)
    with pytest.raises(ValueError):
        ContainerPilotClient.for_private_unix(endpoint, secret, timeout_seconds=timeout)
