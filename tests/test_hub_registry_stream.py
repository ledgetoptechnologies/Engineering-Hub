"""Read-only scoped SSE transport, including framing and lifetime boundaries."""
import asyncio
from contextlib import aclosing
import json

import httpx
import pytest

from engineering_hub.registry_client import (
    RegistryAccessDenied, RegistryPointer, RegistrySnapshotRequired,
    RegistrySourceUnavailable, RegistryUnavailable,
)
from engineering_hub.registry_stream import RegistryStream, RegistryStreamProtocolError

TOPIC = "hub-build"
EPOCH = "6db5150b-3ced-45aa-9ab6-72b2dd03c5ec"
REV = "8a4b2be5-dbd1-484c-8046-ce48f3774f79"
OTHER_EPOCH = "4d5320a2-36f3-47ac-8dce-dea638c5bd40"


def pointer(cursor=1, **updates):
    return dict(topic=TOPIC, registry_epoch=EPOCH, snapshot_revision=REV,
                cursor=cursor, **updates)


def frame(kind="registry.invalidated", data=None, *, event_id="auto", newline="\n"):
    data = pointer() if data is None else data
    if event_id == "auto":
        event_id = f"{data['registry_epoch']}:{data['topic']}:{data.get('cursor', 1)}" if kind == "registry.invalidated" else None
    lines = [f"event: {kind}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("data: " + (data if isinstance(data, str) else json.dumps(data)))
    return (newline.join(lines) + newline * 2).encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, block=False, error=None):
        self.chunks = chunks
        self.block = block
        self.error = error
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def subscription(body=b"", *, status=200, headers=None, block=False):
    wire = Chunks([body] if isinstance(body, bytes) else body, block=block)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers or {"content-type": "text/event-stream"}, stream=wire)

    return RegistryStream("https://registry.example", transport=httpx.MockTransport(handler)), wire, requests


async def collect(stream, **kwargs):
    return [item async for item in stream.events(TOPIC, "private-test-token", **kwargs)]


async def test_scoped_request_and_explicit_early_close():
    stream, wire, requests = subscription(frame(), block=True)
    # 4096-byte chunk buffering must not delay small live events. No EOF needed.
    async with aclosing(stream.events(TOPIC, "private-test-token", after=0, epoch=EPOCH)) as events:
        item = await asyncio.wait_for(anext(events), 1)
        assert isinstance(item, RegistryPointer) and item.cursor == 1
    assert wire.closed
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"https://registry.example/api/topic-registry/v1/topics/{TOPIC}/subscribe?after=0&epoch={EPOCH}"
    assert request.headers["Authorization"] == "Bearer private-test-token"
    assert request.headers["Last-Event-ID"] == f"{EPOCH}:{TOPIC}:0"
    assert "x-act-as" not in request.headers
    assert request.extensions["timeout"] == dict(connect=5, read=45, write=5, pool=5)
    assert "private-test-token" not in repr(stream)


async def test_access_guard_checks_comment_chunks_and_closes_without_public_event():
    stream, wire, requests = subscription([b": keepalive\n\n", b": keepalive again\n\n"], block=True)
    checks = []

    def check_access():
        checks.append(True)
        if len(checks) == 2:
            raise RegistryAccessDenied(403)

    with pytest.raises(RegistryAccessDenied):
        await asyncio.wait_for(collect(stream, check_access=check_access), 1)
    assert len(checks) == 2
    assert len(requests) == 1
    assert wire.closed
    assert not wire.waiting.is_set()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_fragmented_frames_comments_and_exact_duplicate(newline):
    unavailable = dict(topic=TOPIC, registry_epoch=EPOCH, reason="observer_unavailable")
    body = (b": keepalive\n\n" + frame(newline=newline) + frame(newline=newline)
            + frame(data=pointer(2), newline=newline)
            + frame("registry.unavailable", unavailable, newline=newline))
    stream, wire, _ = subscription([body[i:i+3] for i in range(0, len(body), 3)])
    result = await collect(stream)
    assert [type(x) for x in result] == [RegistryPointer, RegistryPointer, RegistryUnavailable]
    assert [x.cursor for x in result[:2]] == [1, 2]
    assert wire.closed


@pytest.mark.parametrize("reason", ["initial_snapshot", "epoch_changed", "retention_gap", "catchup_limit", "subscriber_overflow"])
async def test_explicit_snapshot_required_is_returned_not_silent_reset(reason):
    notice_epoch = OTHER_EPOCH if reason == "epoch_changed" else EPOCH
    body = dict(topic=TOPIC, registry_epoch=notice_epoch, head=8, reason=reason)
    stream, wire, requests = subscription(frame("registry.snapshot_required", body))
    items = await collect(stream, **({} if reason == "initial_snapshot" else dict(after=2, epoch=EPOCH)))
    assert len(items) == 1 and isinstance(items[0], RegistrySnapshotRequired)
    assert items[0].head == 8 and items[0].registry_epoch == notice_epoch
    assert wire.closed and len(requests) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_denial_is_reader_scoped_without_body_leak(status):
    stream, wire, requests = subscription(b"private-token-and-session", status=status)
    with pytest.raises(RegistryAccessDenied) as caught:
        await collect(stream)
    assert caught.value.status_code == status
    assert "private" not in str(caught.value)
    assert wire.closed and len(requests) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 400, 404, 409, 429, 500, 503])
async def test_status_errors_do_not_redirect_retry_or_expose_body(status):
    stream, wire, requests = subscription(b"secret", status=status, headers={"location": "https://private.example/secret"})
    with pytest.raises(RegistrySourceUnavailable):
        await collect(stream)
    assert wire.closed and len(requests) == 1


@pytest.mark.parametrize("body", [
    b"event: registry.invalidated\ndata: {}\n",  # no dispatch boundary
    b"unterminated", b"\xff\n", b"data: \x00\n\n",
    b"event: registry.invalidated\rdata: {}\n\n",  # bare CR
    b"data: {}\n\n", b"event: registry.invalidated\n\n",
    b"retry: 10\n\n", b"unknown: secret\n\n",
    b"event: registry.invalidated\nevent: registry.invalidated\ndata: {}\n\n",
    b"event: registry.invalidated\nid: x\nid: x\ndata: {}\n\n",
    b"x" * 16385 + b"\n", b":" + b"x" * 16385 + b"\n",
    b"event: registry.invalidated\n" + b"data: " + b"x" * 16360 + b"\n\n",
    frame("private.session.changed", {}, event_id=None),
    frame(data='{"topic":"hub-build","topic":"hub-build"}', event_id=None),
    frame(data={**pointer(), "session_id": "private-session"}),
    frame(data={**pointer(), "cursor": True}),
    frame(data={**pointer(), "cursor": -1}),
    frame(data={**pointer(), "cursor": 2**63}),
    frame(data={**pointer(), "topic": "some-other-topic"}),
    frame(event_id=None), frame(event_id=f"{EPOCH}:{TOPIC}:01"),
    frame(event_id=f"{EPOCH}:other:1"),
])
async def test_malformed_private_wrong_scope_frames_fail_closed(body):
    stream, wire, requests = subscription(body)
    with pytest.raises(RegistryStreamProtocolError) as caught:
        await collect(stream)
    assert str(caught.value) == "registry_stream_protocol_error"
    assert wire.closed and len(requests) == 1


@pytest.mark.parametrize("second", [
    frame(data={**pointer(), "snapshot_revision": "d20127f8-872a-44fc-9ebd-a7c49ef43621"}),
    frame(data=pointer(0)),
    frame(data={**pointer(2), "registry_epoch": OTHER_EPOCH}),
])
async def test_cursor_or_epoch_change_cannot_be_silently_adopted(second):
    stream, wire, _ = subscription(frame() + second)
    with pytest.raises(RegistryStreamProtocolError):
        await collect(stream)
    assert wire.closed


async def test_resume_cursor_equal_without_previous_payload_is_not_identical_proof():
    stream, _, _ = subscription(frame())
    with pytest.raises(RegistryStreamProtocolError):
        await collect(stream, after=1, epoch=EPOCH)


async def test_multiline_json_is_valid_sse():
    data = json.dumps(pointer(), indent=2)
    body = (f"event: registry.invalidated\nid: {EPOCH}:{TOPIC}:1\n" + "".join("data: " + line + "\n" for line in data.splitlines()) + "\n").encode()
    stream, wire, _ = subscription(body)
    async with aclosing(stream.events(TOPIC, "private-test-token")) as events:
        assert (await anext(events)).cursor == 1
    assert wire.closed


async def test_clean_eof_is_unavailable_not_reachable_or_reconnect():
    stream, wire, requests = subscription(b": keepalive\n\n")
    with pytest.raises(RegistrySourceUnavailable):
        await collect(stream)
    assert wire.closed and len(requests) == 1


async def test_cancellation_closes_response_without_swallowing_cancel():
    stream, wire, requests = subscription(b": keepalive\n\n", block=True)
    task = asyncio.create_task(collect(stream))
    await asyncio.wait_for(wire.waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.closed and len(requests) == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout("secret"), httpx.RemoteProtocolError("secret")])
async def test_transport_errors_safe_and_closed(error):
    stream, wire, requests = subscription([])
    wire.error = error
    with pytest.raises(RegistrySourceUnavailable) as caught:
        await collect(stream)
    assert "secret" not in str(caught.value)
    assert wire.closed and len(requests) == 1


@pytest.mark.parametrize("options", [dict(after=1), dict(epoch=EPOCH), dict(after=True, epoch=EPOCH), dict(after=-1, epoch=EPOCH), dict(after=2**63, epoch=EPOCH)])
async def test_invalid_resume_parameters_never_open_request(options):
    stream, _, requests = subscription()
    with pytest.raises(RegistryStreamProtocolError):
        await collect(stream, **options)
    assert requests == []


@pytest.mark.parametrize("headers", [{"content-type": "application/json"}, {"content-type": "text/event-stream", "content-encoding": "gzip"}])
async def test_wrong_type_or_compressed_stream_rejected(headers):
    stream, wire, _ = subscription(headers=headers)
    with pytest.raises(RegistryStreamProtocolError):
        await collect(stream)
    assert wire.closed


async def test_source_identity_is_read_only_canonical_origin():
    source = RegistryStream("https://registry.example/")
    assert source.source_identity == "https://registry.example"
    with pytest.raises(AttributeError):
        source.source_identity = "https://another.example"


@pytest.mark.parametrize("topic", ["../private", "a/b", "Topic", "t?token=secret", "", "a" * 129])
async def test_noncanonical_topic_never_opens_request(topic):
    stream, _, requests = subscription()
    with pytest.raises(ValueError):
        await anext(stream.events(topic, "token"))
    assert requests == []


@pytest.mark.parametrize("token", ["", "a\r\nX-Act-As: admin", "a b", "é", "x" * 8193, None])
async def test_invalid_bearer_never_opens_request(token):
    stream, _, requests = subscription()
    with pytest.raises(RegistryStreamProtocolError):
        await anext(stream.events(TOPIC, token))
    assert requests == []


async def test_setup_timeout_is_total_and_safe(monkeypatch):
    monkeypatch.setattr("engineering_hub.registry_stream.SETUP_TIMEOUT_SECONDS", .01)
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.Event().wait()

    stream = RegistryStream("https://registry.example", transport=httpx.MockTransport(handler))
    with pytest.raises(RegistrySourceUnavailable):
        await collect(stream)
    assert len(calls) == 1


async def test_idle_timeout_closes_without_polling_or_reconnecting(monkeypatch):
    monkeypatch.setattr("engineering_hub.registry_stream.READ_IDLE_SECONDS", .01)
    stream, wire, requests = subscription(b": keepalive\n\n", block=True)
    with pytest.raises(RegistrySourceUnavailable):
        await collect(stream)
    assert wire.closed and len(requests) == 1


@pytest.mark.parametrize("kind,data,event_id", [
    ("registry.unavailable", dict(topic=TOPIC, registry_epoch=EPOCH, reason="observer_unavailable"), f"{EPOCH}:{TOPIC}:0"),
    ("registry.unavailable", dict(topic=TOPIC, registry_epoch=OTHER_EPOCH, reason="observer_unavailable"), None),
    ("registry.snapshot_required", dict(topic=TOPIC, registry_epoch=EPOCH, head=0, reason="initial_snapshot"), "private"),
    ("registry.snapshot_required", dict(topic=TOPIC, registry_epoch=EPOCH, head=0, reason="private_reason"), None),
])
async def test_control_events_are_scoped_and_have_no_event_id(kind, data, event_id):
    stream, wire, _ = subscription(frame(kind, data, event_id=event_id))
    with pytest.raises(RegistryStreamProtocolError):
        await collect(stream, after=0, epoch=EPOCH)
    assert wire.closed


@pytest.mark.parametrize("reason,notice_epoch,head,resume", [
    ("epoch_changed", OTHER_EPOCH, 0, None),
    ("retention_gap", EPOCH, 8, None),
    ("catchup_limit", EPOCH, 8, None),
    ("subscriber_overflow", EPOCH, 8, None),
    ("initial_snapshot", EPOCH, 8, 2),
    ("epoch_changed", EPOCH, 8, 2),
    ("retention_gap", OTHER_EPOCH, 8, 2),
    ("catchup_limit", OTHER_EPOCH, 8, 2),
    ("subscriber_overflow", OTHER_EPOCH, 8, 2),
    ("retention_gap", EPOCH, 1, 2),
    ("catchup_limit", EPOCH, 1, 2),
    ("subscriber_overflow", EPOCH, 1, 2),
])
async def test_snapshot_required_reason_epoch_and_head_must_be_coherent(reason, notice_epoch, head, resume):
    payload = dict(topic=TOPIC, registry_epoch=notice_epoch, head=head, reason=reason)
    stream, wire, _ = subscription(frame("registry.snapshot_required", payload))
    options = {} if resume is None else dict(after=resume, epoch=EPOCH)
    with pytest.raises(RegistryStreamProtocolError):
        await collect(stream, **options)
    assert wire.closed


async def test_explicit_epoch_change_can_reset_head_below_prior_cursor():
    notice = dict(topic=TOPIC, registry_epoch=OTHER_EPOCH, head=0, reason="epoch_changed")
    stream, wire, _ = subscription(frame("registry.snapshot_required", notice))
    items = await collect(stream, after=20, epoch=EPOCH)
    assert len(items) == 1 and items[0].head == 0 and items[0].registry_epoch == OTHER_EPOCH
    assert wire.closed


@pytest.mark.parametrize("notice", [
    dict(topic=TOPIC, registry_epoch=EPOCH, head=0, reason="initial_snapshot"),
    dict(topic=TOPIC, registry_epoch=EPOCH, head=0, reason="retention_gap"),
    dict(topic=TOPIC, registry_epoch=OTHER_EPOCH, head=2, reason="subscriber_overflow"),
])
async def test_notices_also_obey_last_observed_pointer_without_resume(notice):
    stream, wire, _ = subscription(frame() + frame("registry.snapshot_required", notice))
    with pytest.raises(RegistryStreamProtocolError):
        await collect(stream)
    assert wire.closed
