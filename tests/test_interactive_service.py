"""Private asset transport ownership and immutable range boundaries; no hosts."""
import asyncio

from fastapi import FastAPI
import httpx
import pytest
from starlette.responses import JSONResponse

from engineering_hub.interactive_service import InteractiveService, attach_interactive_assets, byte_range
from engineering_hub.repository import HubError


ASSET = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
COMPONENT = 'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee'
DIGEST = 'a' * 64
PATH = '/api/hub/v1/interactive-reviews/synthetic/assets/' + ASSET


class Bytes(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        self.reads += 1
        yield b'abcd'

    async def aclose(self):
        self.closed = True


def app_for(service, *, fail_second=None, project='treedar'):
    class Reviews:
        calls = 0

        def get_review(self, actor, identity):
            self.calls += 1
            if self.calls == 2 and fail_second is not None:
                raise fail_second
            return {'project': project, 'component_id': COMPONENT, 'items': [{'asset_id': ASSET}]}

    app = FastAPI()

    @app.exception_handler(HubError)
    async def failure(request, error):
        return JSONResponse({'detail': {'code': error.code}}, status_code=error.status)

    def human(request):
        if request.headers.get('x-synthetic-human') != 'yes':
            raise HubError('unauthorized', 401)
        return 'human:1'

    attach_interactive_assets(app, service, Reviews(), human)
    return app


def service_for(client):
    return InteractiveService(grants={'treedar': {'human:1': ['review_read', 'review_answer']}},
        assets={ASSET: {'project': 'treedar', 'component_id': COMPONENT, 'size': 4,
                       'sha256': DIGEST, 'media_type': 'application/octet-stream'}}, client=client)


def test_asset_binding_for_authorized_reader_does_not_require_create():
    service = service_for(None)
    assert service.asset_authorize('human:1', 'treedar', COMPONENT, ASSET)
    assert not service.asset_authorize('human:1', 'treedar', COMPONENT, 'unknown')


@pytest.mark.asyncio
async def test_real_stream_range_headers_and_owned_closure():
    stream = Bytes()

    async def upstream(request):
        assert request.url == 'https://192.168.50.90/v1/assets/' + ASSET
        assert request.headers['range'] == 'bytes=0-3'
        assert 'cookie' not in request.headers and 'x-synthetic-human' not in request.headers
        return httpx.Response(206, headers={'Content-Range': 'bytes 0-3/4', 'Content-Length': '4',
            'ETag': '"sha256:' + DIGEST + '"', 'Content-Type': 'application/octet-stream'}, stream=stream)

    async with httpx.AsyncClient(base_url='https://192.168.50.90', transport=httpx.MockTransport(upstream)) as worker:
        async with httpx.AsyncClient(base_url='https://hub.invalid', transport=httpx.ASGITransport(app=app_for(service_for(worker)))) as browser:
            assert (await browser.get(PATH)).status_code == 401
            response = await browser.get(PATH, headers={'X-Synthetic-Human': 'yes', 'Range': 'bytes=0-3'})
            assert response.status_code == 206 and response.content == b'abcd'
            assert response.headers['content-range'] == 'bytes 0-3/4'
            assert stream.closed and stream.reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_headers', [{'Content-Range': 'bytes 1-4/5'}, {'ETag': '"changed"'},
    {'Content-Length': '5'}, {'Content-Type': 'text/html'}, {'Content-Encoding': 'gzip'}])
async def test_bad_immutable_upstream_metadata_closes_without_reading(bad_headers):
    stream = Bytes()
    headers = {'Content-Range': 'bytes 0-3/4', 'Content-Length': '4',
               'ETag': '"sha256:' + DIGEST + '"', 'Content-Type': 'application/octet-stream', **bad_headers}
    async with httpx.AsyncClient(base_url='https://192.168.50.90', transport=httpx.MockTransport(
            lambda request: httpx.Response(206, headers=headers, stream=stream))) as worker:
        async with httpx.AsyncClient(base_url='https://hub.invalid', transport=httpx.ASGITransport(app=app_for(service_for(worker)))) as browser:
            response = await browser.get(PATH, headers={'X-Synthetic-Human': 'yes'})
            assert response.status_code == 503
    assert stream.closed and stream.reads == 0


@pytest.mark.asyncio
async def test_cancellation_during_readmission_closes_acquired_upstream():
    stream = Bytes()
    headers = {'Content-Range': 'bytes 0-3/4', 'Content-Length': '4',
               'ETag': '"sha256:' + DIGEST + '"', 'Content-Type': 'application/octet-stream'}
    async with httpx.AsyncClient(base_url='https://192.168.50.90', transport=httpx.MockTransport(
            lambda request: httpx.Response(206, headers=headers, stream=stream))) as worker:
        app = app_for(service_for(worker), fail_second=asyncio.CancelledError())
        async with httpx.AsyncClient(base_url='https://hub.invalid', transport=httpx.ASGITransport(app=app)) as browser:
            with pytest.raises(asyncio.CancelledError):
                await browser.get(PATH, headers={'X-Synthetic-Human': 'yes'})
    assert stream.closed and stream.reads == 0


@pytest.mark.parametrize('value', ['bytes=0-1,3-4', 'bytes=-0', 'bytes=4-', 'bytes=3-1', 'bytes=0-9999999999999'])
def test_invalid_range_never_becomes_unbounded(value):
    with pytest.raises(HubError) as error:
        byte_range(value, 4)
    assert error.value.status == 416
