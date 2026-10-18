"""Explicit, read-only subscription to one canonical registry topic.

There is no background task, polling, reconnect, credential persistence or
readiness inference here. The caller owns and must close the async iterator.
Comments are transport keepalives, never evidence of a ready chat. Snapshot
requests and unavailable notices remain explicit events for the caller.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx

from .registry_client import (
    RegistryAccessDenied,
    RegistryPointer,
    RegistrySnapshotRequired,
    RegistrySourceUnavailable,
    RegistryUnavailable,
    parse_event,
    validate_epoch,
    validate_origin,
    validate_topic,
)


RegistryEvent = RegistryPointer | RegistrySnapshotRequired | RegistryUnavailable
MAX_FRAME_BYTES = 16 * 1024
MAX_LINE_BYTES = 16 * 1024
MAX_CURSOR = 2**63 - 1
SETUP_TIMEOUT_SECONDS = 5
READ_IDLE_SECONDS = 45


class RegistryStreamProtocolError(Exception):
    """Fixed public error; never includes remote payloads or credentials."""

    code = "registry_stream_protocol_error"

    def __init__(self) -> None:
        super().__init__(self.code)


def _cursor(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CURSOR:
        raise RegistryStreamProtocolError()
    return value


async def _frames(response: httpx.Response) -> AsyncIterator[tuple[str, str | None, str]]:
    """Strict bounded SSE subset. Unknown fields and incomplete frames fail shut."""
    pending = bytearray()
    fields: dict[str, str] = {}
    data: list[str] = []
    frame_bytes = 0

    async for chunk in _chunks(response):
        # Retain at most one bounded partial line, regardless of remote chunking.
        for octet in chunk:
            if octet != 10:
                pending.append(octet)
                if len(pending) > MAX_LINE_BYTES:
                    raise RegistryStreamProtocolError()
                continue
            raw = bytes(pending)
            wire_line_bytes = len(raw) + 1
            pending.clear()
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                line = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise RegistryStreamProtocolError() from None
            if "\r" in line or "\x00" in line:
                raise RegistryStreamProtocolError()
            if not line:
                if fields or data:
                    if set(fields) - {"event", "id"} or "event" not in fields or not data:
                        raise RegistryStreamProtocolError()
                    yield fields["event"], fields.get("id"), "\n".join(data)
                fields = {}
                data = []
                frame_bytes = 0
                continue
            # Standalone comments do not accumulate; in-frame comments count
            # toward the frame bound and cannot prolong partial-frame storage.
            if line.startswith(":") and not fields and not data:
                continue
            frame_bytes += wire_line_bytes
            if frame_bytes > MAX_FRAME_BYTES:
                raise RegistryStreamProtocolError()
            if line.startswith(":"):
                continue
            name, separator, value = line.partition(":")
            if not separator or name not in {"event", "id", "data"}:
                raise RegistryStreamProtocolError()
            if value.startswith(" "):
                value = value[1:]
            if name == "data":
                data.append(value)
            elif name in fields:
                raise RegistryStreamProtocolError()
            else:
                fields[name] = value
    if pending or fields or data:
        raise RegistryStreamProtocolError()


async def _chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    iterator = response.aiter_bytes().__aiter__()
    while True:
        try:
            async with asyncio.timeout(READ_IDLE_SECONDS):
                chunk = await anext(iterator)
        except StopAsyncIteration:
            return
        yield chunk


@asynccontextmanager
async def _open(client: httpx.AsyncClient, url: str, params: dict[str, str], headers: dict[str, str]):
    # Bound complete response-header setup, not the caller-owned stream lifetime.
    async with AsyncExitStack() as stack:
        async with asyncio.timeout(SETUP_TIMEOUT_SECONDS):
            response = await stack.enter_async_context(client.stream("GET", url, params=params, headers=headers))
        yield response


class RegistryStream:
    """One-shot subscriptions to a configured origin; no caller-selected URLs.

    A bearer exists only in the HTTP client's request memory for this iterator.
    Use ``contextlib.aclosing(stream.events(...))`` when stopping early. The
    subscription closes on cancellation, invalid frames, unavailable notice or
    iterator closure. A clean remote EOF is unavailable, not a successful read.
    """

    def __init__(
        self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._origin = validate_origin(base_url)
        self._transport = transport

    @property
    def source_identity(self) -> str:
        """Configured canonical origin, for trusted snapshot/stream pairing."""
        return self._origin

    async def events(
        self, topic: str, bearer: str, *, after: int | None = None,
        epoch: str | None = None,
    ) -> AsyncIterator[RegistryEvent]:
        topic = validate_topic(topic)
        if not isinstance(bearer, str) or not bearer or len(bearer) > 8192 or any(
            ord(character) < 33 or ord(character) > 126 for character in bearer
        ):
            raise RegistryStreamProtocolError()
        if (after is None) != (epoch is None):
            raise RegistryStreamProtocolError()
        params: dict[str, str] = {}
        headers = {
            "Authorization": f"Bearer {bearer}", "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
        }
        current_epoch = None
        current_cursor = None
        if epoch is not None:
            current_epoch = validate_epoch(epoch)
            current_cursor = _cursor(after)
            params = {"after": str(current_cursor), "epoch": current_epoch}
            headers["Last-Event-ID"] = f"{current_epoch}:{topic}:{current_cursor}"
        previous: tuple[str, str, int] | None = None
        try:
            async with httpx.AsyncClient(
                transport=self._transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(connect=5, read=45, write=5, pool=5),
            ) as client:
                async with _open(
                    client, f"{self._origin}/api/topic-registry/v1/topics/{topic}/subscribe",
                    params, headers,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise RegistryAccessDenied(response.status_code)
                    if response.status_code != 200:
                        raise RegistrySourceUnavailable()
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type.split(";", 1)[0].strip() != "text/event-stream":
                        raise RegistryStreamProtocolError()
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise RegistryStreamProtocolError()
                    async for event_type, event_id, data in _frames(response):
                        try:
                            event = parse_event(event_type, data)
                        except RegistryStreamProtocolError:
                            raise
                        except (RegistrySourceUnavailable, ValueError, TypeError, KeyError):
                            raise RegistryStreamProtocolError() from None
                        if event.topic != topic:
                            raise RegistryStreamProtocolError()
                        if isinstance(event, RegistryPointer):
                            cursor = _cursor(event.cursor)
                            if event_id != f"{event.registry_epoch}:{topic}:{cursor}":
                                raise RegistryStreamProtocolError()
                            if current_epoch is not None and event.registry_epoch != current_epoch:
                                raise RegistryStreamProtocolError()
                            if current_cursor is not None and cursor < current_cursor:
                                raise RegistryStreamProtocolError()
                            identity = (event.registry_epoch, event.snapshot_revision, cursor)
                            if current_cursor == cursor:
                                if previous is None or identity != previous:
                                    raise RegistryStreamProtocolError()
                                continue
                            current_epoch, current_cursor = event.registry_epoch, cursor
                            previous = identity
                        elif isinstance(event, RegistrySnapshotRequired):
                            if event_id is not None:
                                raise RegistryStreamProtocolError()
                            head = _cursor(event.head)
                            if current_epoch is None:
                                if event.reason != "initial_snapshot":
                                    raise RegistryStreamProtocolError()
                            elif event.reason == "initial_snapshot":
                                raise RegistryStreamProtocolError()
                            elif event.reason == "epoch_changed":
                                if event.registry_epoch == current_epoch:
                                    raise RegistryStreamProtocolError()
                            elif event.registry_epoch != current_epoch:
                                raise RegistryStreamProtocolError()
                            elif current_cursor is not None and head < current_cursor:
                                raise RegistryStreamProtocolError()
                            # A reset is explicit, never silently inferred from
                            # a pointer. The consumer must fetch a new snapshot.
                            yield event
                            return
                        elif isinstance(event, RegistryUnavailable):
                            if event_id is not None or (
                                event.registry_epoch is not None and current_epoch is not None
                                and event.registry_epoch != current_epoch
                            ):
                                raise RegistryStreamProtocolError()
                            yield event
                            return
                        else:
                            raise RegistryStreamProtocolError()
                        yield event
                    raise RegistrySourceUnavailable()
        except (httpx.HTTPError, TimeoutError):
            raise RegistrySourceUnavailable() from None
