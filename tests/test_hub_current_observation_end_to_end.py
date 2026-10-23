"""Synthetic signed supervisor -> UDS -> ASGI -> TLS -> core verifier slice.

The reused issuer fixture bypasses real Engine/kernel enrollment checks. This
tests the actual protocol pieces together, NOT a successful container launch,
NAS route, independently observed inventory, deployment or reboot acceptance.
"""
import asyncio
from contextlib import contextmanager
import json
import os
import sys
import threading
from uuid import uuid4

import pytest

if sys.platform != "linux":
    pytest.skip("Linux peer/PID fixtures", allow_module_level=True)

from engineering_hub.current_executor_observation import (
    CurrentExecutorObserver, CurrentExecutorObservationError,
)
from engineering_hub.current_executor_transport import CurrentExecutorHTTPSTransport
from hub_container_executor.asgi import create_app
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from tests.test_hub_container_executor_launch_supervisor import (
    short_root, root_journal, synthetic_issuer, current_pins, current_ready,
)
from tests.test_hub_current_executor_transport import material, server, TOKEN


class NoCommands:
    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError("Observation must not call a command adapter")


@contextmanager
def owned_supervisor_wait(authority):
    stop, outcomes = threading.Event(), []
    def run():
        try:
            outcomes.append(authority.wait_for_stop_with_observations(
                timeout_ms=10000, stop_requested=stop.is_set))
        except BaseException as error:
            outcomes.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(2)
        assert not thread.is_alive()
        assert outcomes == [True]


async def request(app, header, body):
    lines = header.split(b"\r\n")
    method, target, _ = lines[0].split(b" ")
    events = []
    headers = []
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        headers.append((name.lower(), value.strip()))
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(event):
        events.append(event)
    await app({"type": "http", "method": method.decode(), "path": target.decode(),
               "query_string": b"", "headers": headers}, receive, send)
    return events


@pytest.mark.parametrize("changed", ["launch_id", "original_expiry"])
def test_actual_signed_observation_roundtrip_and_sticky_pin_denial(
        synthetic_issuer, material, changed):
    authority, path, checks = synthetic_issuer
    current_ready(authority)
    # Retain independent authority BEFORE any response. Never learn pins from
    # the HTTPS body, launch relay output or a refreshed observation.
    release, launch = current_pins(authority)
    original = (authority._launch_id, authority._approval_expires_at_ms,
                authority._expires, authority._receipt_served)
    relay = UnixCurrentObservationTransport(socket_path=str(path),
        expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    adapter = NoCommands()
    app = create_app(adapter, bearer_token=TOKEN.decode(), current_observation_transport=relay)
    responses = []
    def serve_asgi(conn, _):
        header, body = seen[-1]
        # The local TLS fixture owns this Runner and all accepted worker futures.
        # There is no ambient asyncio policy loop or abandoned background task.
        with asyncio.Runner() as runner:
            events = runner.run(request(app, header, body))
        start, end = events
        assert start["type"] == "http.response.start" and end["type"] == "http.response.body"
        assert start["status"] == 200
        response_headers = start["headers"]
        assert dict(response_headers)[b"content-type"] == b"application/json"
        assert dict(response_headers)[b"connection"] == b"close"
        responses.append(json.loads(end["body"]))
        raw_header = b"HTTP/1.1 200 OK\r\n" + b"\r\n".join(
            key + b": " + value for key, value in response_headers) + b"\r\n\r\n"
        conn.sendall(raw_header + end["body"])
    try:
        with owned_supervisor_wait(authority):
            with server(material, serve_asgi) as (origin, seen):
                transport = CurrentExecutorHTTPSTransport(origin,
                    **{key: material[key] for key in (
                        "ca_file", "ca_sha256", "credential_file", "credential_sha256")})
                observer = CurrentExecutorObserver(release=release, launch=launch, transport=transport)
                first, second = observer(), observer()
                assert first.generation_id == second.generation_id == launch.launch_id
                assert first.artifact_digest == second.artifact_digest == release.image_config_id
                assert len(seen) == len(responses) == 2
                payloads = [response["payload"] for response in responses]
                assert payloads[0]["nonce"] != payloads[1]["nonce"]
                assert payloads[0]["observation_id"] != payloads[1]["observation_id"]
                assert all(set(json.loads(body)) == {"nonce"} for _, body in seen)
                assert all(payload["approval_expires_at_ms"] == launch.approval_expires_at_ms
                           for payload in payloads)
                assert (authority._launch_id, authority._approval_expires_at_ms,
                        authority._expires, authority._receipt_served) == original
                if changed == "launch_id":
                    authority._launch_id = str(uuid4())
                else:
                    authority._approval_expires_at_ms += 10000
                with pytest.raises(CurrentExecutorObservationError):
                    observer()
                assert len(seen) == len(responses) == 3
                # Restoring fixture state cannot reopen a denied observer or
                # generate another network request. Manual reconciliation is
                # a separate explicit composition requirement.
                authority._launch_id, authority._approval_expires_at_ms = original[:2]
                with pytest.raises(CurrentExecutorObservationError):
                    observer()
                assert len(seen) == 3
                assert not authority._held and checks
                assert adapter.touched == []
                observer.close()
    finally:
        relay.close()
