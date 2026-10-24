"""In-process bounded asset streaming; no socket, worker or browser session."""
import asyncio

import httpx
import pytest

from engineering_hub.private_gateway import PublicBrowserGateway


PATH = '/api/hub/v1/interactive-reviews/4909f74a-73fb-4895-a86f-546aa84b76e5/assets/e10c4f3f-470d-44bf-8937-a87377b4ac39'


@pytest.mark.asyncio
@pytest.mark.parametrize('path,length,expected', [
    ('/api/hub/v1/interactive-reviews/4909f74a-73fb-4895-a86f-546aa84b76e5', 3 * 1024**2, 200),
    ('/api/hub/v1/interactive-reviews/4909f74a-73fb-4895-a86f-546aa84b76e5/state', 4 * 1024**2, 200),
    ('/api/hub/v1/interactive-reviews/4909f74a-73fb-4895-a86f-546aa84b76e5', 4 * 1024**2 + 1, 502),
    ('/api/hub/v1/tasks/4909f74a-73fb-4895-a86f-546aa84b76e5', 3 * 1024**2, 502),
])
async def test_only_typed_review_detail_has_four_mib_budget(path, length, expected):
    stream = Chunks(length)
    messages = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={'Content-Type': 'application/json'}, stream=stream))) as client:
        request_scope = scope()
        request_scope.update(path=path, raw_path=path.encode())
        async def send(message):
            messages.append(message)
        await PublicBrowserGateway(client)(request_scope, receiver(), send)
    assert messages[0]['status'] == expected
    assert stream.closed.is_set()


def scope(method='GET', headers=()):
    return {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': method, 'scheme': 'https', 'path': PATH, 'raw_path': PATH.encode(),
        'query_string': b'', 'headers': [(b'host', b'hub.example'), *headers],
        'server': ('hub.example', 443), 'client': ('127.0.0.1', 12345), 'root_path': ''}


def receiver():
    sent = False
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await asyncio.Future()
    return receive


class Chunks(httpx.AsyncByteStream):
    def __init__(self, length, release=None):
        self.length, self.release = length, release
        self.produced = 0
        self.closed = asyncio.Event()
    async def __aiter__(self):
        while self.produced < self.length:
            if self.produced == 65536 and self.release is not None:
                await self.release.wait()
            count = min(65536, self.length - self.produced)
            self.produced += count
            yield b'x' * count
    async def aclose(self):
        self.closed.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('length', [3 * 1024 * 1024, 8 * 1024 * 1024])
async def test_range_headers_and_more_than_default_two_mib_stream_without_buffering(length):
    release, first = asyncio.Event(), asyncio.Event()
    stream = Chunks(length, release)
    messages, calls = [], []
    offset, total = 1024, 20 * 1024 * 1024
    requested_range = f'bytes={offset}-{offset + length - 1}'
    content_range = f'bytes {offset}-{offset + length - 1}/{total}'
    async def upstream(request):
        calls.append(request)
        assert request.headers['range'] == requested_range
        assert request.method == 'GET' and request.url.path == PATH
        return httpx.Response(206, headers={'content-length': str(length),
            'content-range': content_range, 'accept-ranges': 'bytes',
            'etag': '"synthetic-sha256"', 'content-type': 'application/octet-stream'}, stream=stream)
    async def send(message):
        messages.append(message)
        if message['type'] == 'http.response.body' and message.get('body'):
            first.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        gateway = PublicBrowserGateway(client)  # Ordinary response bound remains2MiB.
        task = asyncio.create_task(gateway(scope(headers=[(b'range', requested_range.encode())]), receiver(), send))
        try:
            await asyncio.wait_for(first.wait(), 2)
            assert stream.produced == 65536 and not task.done()
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        headers = dict(messages[0]['headers'])
        assert messages[0]['status'] == 206
        assert headers[b'content-range'] == content_range.encode()
        assert headers[b'accept-ranges'] == b'bytes' and headers[b'content-length'] == str(length).encode()
        bodies = [message.get('body', b'') for message in messages if message['type'] == 'http.response.body']
        assert sum(map(len, bodies)) == length and max(map(len, bodies)) <= 65536
        assert stream.closed.is_set() and gateway._active == 0 and len(calls) == 1


@pytest.mark.asyncio
async def test_asset_cancellation_closes_original_upstream_and_capacity_slot():
    release, first = asyncio.Event(), asyncio.Event()
    stream = Chunks(3 * 1024 * 1024, release)
    calls = []
    async def upstream(request):
        calls.append(request)
        return httpx.Response(200, headers={'content-length': str(stream.length)}, stream=stream)
    async def send(message):
        if message['type'] == 'http.response.body' and message.get('body'):
            first.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        gateway = PublicBrowserGateway(client)
        task = asyncio.create_task(gateway(scope(), receiver(), send))
        await asyncio.wait_for(first.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(stream.closed.wait(), 2)
        assert gateway._active == 0 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('length', [8 * 1024 * 1024 + 1, 0])
async def test_invalid_declared_asset_size_denied_before_bytes_are_read(length):
    stream = Chunks(max(length, 1))
    messages = []
    async def upstream(request):
        return httpx.Response(206, headers={'content-length': str(length)}, stream=stream)
    async def send(message):
        messages.append(message)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        gateway = PublicBrowserGateway(client)
        await gateway(scope(), receiver(), send)
        assert messages[0]['status'] == 502 and stream.produced == 0
        assert stream.closed.is_set() and gateway._active == 0


@pytest.mark.asyncio
async def test_actual_stream_overrun_closes_without_second_response_or_retry():
    stream = Chunks(2 * 65536)
    messages, calls = [], []
    async def upstream(request):
        calls.append(request)
        return httpx.Response(206, headers={'content-length': '65536'}, stream=stream)
    async def send(message):
        messages.append(message)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        gateway = PublicBrowserGateway(client)
        with pytest.raises(Exception):
            await gateway(scope(), receiver(), send)
        assert len([message for message in messages if message['type'] == 'http.response.start']) == 1
        assert stream.closed.is_set() and gateway._active == 0 and len(calls) == 1


@pytest.mark.asyncio
async def test_head_preserves_full_asset_metadata_without_asset_body():
    messages, calls = [], []
    length = 20 * 1024 * 1024
    async def upstream(request):
        calls.append(request)
        assert request.method == 'HEAD'
        return httpx.Response(200, content=b'', headers={'content-length': str(length),
            'content-type': 'application/octet-stream', 'accept-ranges': 'bytes',
            'etag': '"synthetic-sha256"', 'cache-control': 'private, no-store'})
    async def send(message):
        messages.append(message)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        gateway = PublicBrowserGateway(client)
        await gateway(scope(method='HEAD'), receiver(), send)
        headers = dict(messages[0]['headers'])
        assert messages[0]['status'] == 200 and headers[b'content-length'] == str(length).encode()
        assert headers[b'accept-ranges'] == b'bytes' and headers[b'etag'] == b'"synthetic-sha256"'
        assert not any(message.get('body') for message in messages)
        assert len(calls) == 1 and gateway._active == 0
