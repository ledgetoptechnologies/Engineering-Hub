"""Inert browser-only seam to one private Hub writer.

The caller must provide a dedicated AsyncClient with a permission-restricted
UDS transport (no retries, proxy, event hooks, or default auth), and serve the
receiver only on that UDS. This module neither creates nor proves that socket
boundary. It does not open a database, own an executor, install a server, manage
lifespans, or proxy agent/MCP requests. Agents retain their original listener
and real source-IP checks. The private receiver verifies Access independently.
"""
from __future__ import annotations

import asyncio
import math
import os
import re

import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse

from .ingress import HubIngressMiddleware
from .lifecycle import finish_owned
from .browser_pages import client_page_allowed


PRIVATE_ORIGIN = 'http://hub-private.invalid'
_UUID = r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
_TOPIC = r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}'
_LABEL_ID = r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}'
_GET = frozenset({
    '/', '/index.html', '/hub.svg', '/favicon.ico',
    '/api/hub/v1/auth/login', '/api/hub/v1/auth/me',
    *('/api/hub/v1/' + name for name in (
        'overview', 'projects', 'agents', 'tasks', 'decisions', 'runs', 'image-versions',
        'autonomy', 'events', 'events/status', 'work/status', 'work/jobs', 'notes', 'notes/document',
        'planning', 'planning/document', 'activity', 'label-sets')),
})
_POST = frozenset('/api/hub/v1/' + name for name in (
    'auth/logout', 'tasks', 'decisions', 'pause', 'work/pause', 'notes', 'planning'))
_REQUEST_HEADERS = frozenset({
    b'host', b'origin', b'cookie', b'cf-access-jwt-assertion', b'x-hub-csrf',
    b'content-type', b'accept', b'last-event-id', b'if-none-match',
    b'if-modified-since', b'cache-control', b'user-agent',
})
_RESPONSE_HEADERS = frozenset({
    b'content-type', b'content-length', b'cache-control', b'set-cookie',
    b'location', b'content-security-policy', b'x-content-type-options',
    b'referrer-policy', b'x-accel-buffering', b'etag', b'last-modified',
    b'vary', b'www-authenticate',
})


def browser_route_allowed(method, path):
    """Closed browser surface; new backend routes are not exposed implicitly."""
    if method in {'GET', 'HEAD'}:
        return (path in _GET or client_page_allowed(method, path)
                or re.fullmatch(r'/assets/[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.(?:js|css|svg|png|ico|woff2)', path) is not None
                or re.fullmatch(r'/api/hub/v1/(?:tasks|work/jobs)/' + _UUID, path) is not None
                or re.fullmatch(r'/api/hub/v1/(?:tasks|decisions)/' + _UUID + '/attachments', path) is not None
                or re.fullmatch(r'/api/hub/v1/attachments/' + _UUID + '/image', path) is not None
                or re.fullmatch(r'/api/hub/v1/label-sets/' + _LABEL_ID + r'(?:/results|/items/' + _LABEL_ID + '/image)?', path) is not None
                or re.fullmatch(r'/api/hub/v1/topics/' + _TOPIC + r'/(?:peers|preflight|events)', path) is not None)
    if method == 'PUT':
        return re.fullmatch(r'/api/hub/v1/label-sets/' + _LABEL_ID + '/items/' + _LABEL_ID + '/label', path) is not None
    return method == 'POST' and (path in _POST or re.fullmatch(
        r'/api/hub/v1/decisions/' + _UUID + '/answer', path) is not None
        or re.fullmatch(r'/api/hub/v1/(?:tasks|decisions)/' + _UUID + '/context', path) is not None)


def _is_events(path):
    return path == '/api/hub/v1/events' or re.fullmatch(
        r'/api/hub/v1/topics/' + _TOPIC + '/events', path) is not None


class _Denied(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status


async def _deny(scope, receive, send, code, status):
    await JSONResponse({'detail': {'code': code}}, status_code=status,
                       headers={'Cache-Control': 'no-store'})(scope, receive, send)


def _scope_headers(scope):
    path = scope.get('path', '')
    try:
        canonical = path.encode('ascii')
    except (AttributeError, UnicodeEncodeError):
        raise _Denied('gateway_invalid_path', 400) from None
    if (scope.get('raw_path', canonical) != canonical or b'%' in canonical
            or '\\' in path or '//' in path or any(
                part in {'.', '..'} for part in path.split('/'))):
        raise _Denied('gateway_invalid_path', 400)
    query = scope.get('query_string', b'')
    if type(query) is not bytes or len(query) > 4096 or any(c < 32 or c == 127 for c in query):
        raise _Denied('gateway_invalid_query', 400)
    if not browser_route_allowed(scope.get('method'), path):
        raise _Denied('gateway_route_forbidden', 403)
    headers = scope.get('headers', [])
    if len(headers) > 64 or sum(len(k) + len(v) for k, v in headers) > 32768:
        raise _Denied('gateway_headers_too_large', 431)
    selected, counts = [], {}
    for name, value in headers:
        if (type(name) is not bytes or type(value) is not bytes
                or re.fullmatch(rb'[a-z0-9!#$&\x27*+.^_`|~-]+', name) is None
                or any(c < 32 or c == 127 for c in value)):
            raise _Denied('gateway_invalid_headers', 400)
        counts[name] = counts.get(name, 0) + 1
        if name == b'authorization' or (name.startswith(b'x-hub-') and name != b'x-hub-csrf'):
            raise _Denied('browser_credentials_required', 403)
        if name in {b'transfer-encoding', b'content-encoding'}:
            raise _Denied('gateway_invalid_framing', 400)
        if name in _REQUEST_HEADERS:
            selected.append((name, value))
    if counts.get(b'host') != 1 or any(counts.get(name, 0) > 1 for name in (
            b'content-length', b'origin', b'cookie', b'x-hub-csrf')):
        raise _Denied('gateway_invalid_headers', 400)
    lengths = [value for name, value in headers if name == b'content-length']
    if lengths and re.fullmatch(rb'(?:0|[1-9][0-9]{0,9})', lengths[0]) is None:
        raise _Denied('gateway_invalid_framing', 400)
    return selected, int(lengths[0]) if lengths else None


async def _bounded_body(scope, receive, length, *, maximum, timeout):
    if length is not None and length > maximum:
        raise _Denied('gateway_body_too_large', 413)
    body = bytearray()
    async with asyncio.timeout(timeout):
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                raise _Denied('gateway_disconnected', 400)
            if message['type'] != 'http.request':
                raise _Denied('gateway_invalid_body', 400)
            body.extend(message.get('body', b''))
            if len(body) > maximum:
                raise _Denied('gateway_body_too_large', 413)
            if not message.get('more_body', False):
                break
    if length is not None and length != len(body):
        raise _Denied('gateway_invalid_framing', 400)
    if scope['method'] in {'GET', 'HEAD'} and body:
        raise _Denied('gateway_bodyless_method', 400)
    return bytes(body)


class PrivateBrowserGateway:
    """Restricted receiver around the existing owner core, not a new writer.

    Existing core lifecycle belongs to the caller. Never publish this receiver
    as an unprotected TCP listener; no socket or deployment is created here.
    """
    def __init__(self, core, *, agent_auth, cloudflare_config):
        if cloudflare_config is None:
            raise ValueError('Private browser gateway requires Cloudflare Access')
        self.ingress = HubIngressMiddleware(core, mode='browser', agent_auth=agent_auth,
                                            cloudflare_config=cloudflare_config)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if scope['type'] != 'http':
            raise RuntimeError('Private browser gateway has no independent lifespan')
        try:
            _, length = _scope_headers(scope)
            body = await _bounded_body(scope, receive, length, maximum=65536, timeout=15.0)
        except _Denied as error:
            await _deny(scope, receive, send, error.code, error.status)
            return
        except TimeoutError:
            await _deny(scope, receive, send, 'gateway_request_timeout', 408)
            return
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()
        await self.ingress(scope, replay, send)


class PublicBrowserGateway:
    """Bounded browser HTTP proxy; caller owns the dedicated transport/client.

    One upstream attempt only. A lost mutation response is an unknown outcome,
    not permission to replay it. Original request IDs/revisions remain the
    domain's recovery mechanism. SSE is streamed without buffering a full body.
    """
    def __init__(self, client, *, max_body_bytes=65536, max_response_bytes=2097152,
                 max_concurrency=16, request_timeout=15.0):
        if type(client) is not httpx.AsyncClient:
            raise ValueError('Explicit caller-owned AsyncClient required')
        for value, maximum in ((max_body_bytes, 1048576), (max_response_bytes, 16777216),
                               (max_concurrency, 64)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Invalid gateway bound')
        if (type(request_timeout) not in (int, float) or not math.isfinite(request_timeout)
                or not 0 < request_timeout <= 60):
            raise ValueError('Invalid gateway request timeout')
        if client.event_hooks.get('request') or client.event_hooks.get('response'):
            raise ValueError('Gateway client must not install request/response hooks')
        self.client = client
        self.max_body_bytes, self.max_response_bytes = max_body_bytes, max_response_bytes
        self.max_concurrency, self.request_timeout = max_concurrency, request_timeout
        self._active, self._loop = 0, None
        self._pid = os.getpid()

    async def _body(self, scope, receive, length):
        return await _bounded_body(scope, receive, length, maximum=self.max_body_bytes,
                                   timeout=self.request_timeout)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if scope['type'] != 'http':
            raise RuntimeError('Public browser gateway has no independent lifespan')
        response = None
        admitted = False
        started = False
        async def tracked_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)
        try:
            headers, length = _scope_headers(scope)
            if length is not None and length > self.max_body_bytes:
                raise _Denied('gateway_body_too_large', 413)
            loop = asyncio.get_running_loop()
            if self._loop is None:
                self._loop = loop
            if self._pid != os.getpid() or self._loop is not loop or self._active >= self.max_concurrency:
                raise _Denied('gateway_capacity', 503)
            self._active += 1
            admitted = True
            body = await self._body(scope, receive, length)
            raw_path = scope['path'].encode('ascii')
            if scope.get('query_string'):
                raw_path += b'?' + scope['query_string']
            # Direct Request bypasses client cookie/default-header merging.
            # Explicit auth/redirect options bypass configured client defaults.
            streaming = scope['method'] == 'GET' and _is_events(scope['path'])
            request = httpx.Request(scope['method'], httpx.URL(PRIVATE_ORIGIN).copy_with(
                raw_path=raw_path), headers=headers + [(b'accept-encoding', b'identity')], content=body,
                extensions={'timeout': {'connect': self.request_timeout,
                                        'read': max(45.0, self.request_timeout) if streaming else self.request_timeout,
                                        'write': self.request_timeout, 'pool': self.request_timeout}})
            async with asyncio.timeout(self.request_timeout):
                response = await self.client.send(request, auth=None, follow_redirects=False, stream=True)
            raw_headers = response.headers.raw
            if (len(raw_headers) > 64 or sum(len(k) + len(v) for k, v in raw_headers) > 32768
                    or response.headers.get('content-encoding', 'identity') != 'identity'):
                raise _Denied('gateway_invalid_upstream', 502)
            outgoing = [(k.lower(), v) for k, v in raw_headers if k.lower() in _RESPONSE_HEADERS]
            if streaming and response.status_code == 200:
                if response.headers.get('content-type', '').split(';')[0] != 'text/event-stream':
                    raise _Denied('gateway_invalid_upstream', 502)
                async def chunks():
                    # Never use a buffering chunk_size for a tiny event: it
                    # would delay delivery until enough future events arrive.
                    async for chunk in response.aiter_raw():
                        for offset in range(0, len(chunk), 16384):
                            yield chunk[offset:offset + 16384]
                result = StreamingResponse(chunks(), status_code=200)
            else:
                content = bytearray()
                async with asyncio.timeout(self.request_timeout):
                    if response.is_stream_consumed:
                        content.extend(response.content)
                        if len(content) > self.max_response_bytes:
                            raise _Denied('gateway_response_too_large', 502)
                    else:
                        async for chunk in response.aiter_raw(chunk_size=16384):
                            content.extend(chunk)
                            if len(content) > self.max_response_bytes:
                                raise _Denied('gateway_response_too_large', 502)
                result = Response(bytes(content), status_code=response.status_code)
            result.raw_headers = outgoing
            await result(scope, receive, tracked_send)
        except _Denied as error:
            if started:
                raise
            await _deny(scope, receive, send, error.code, error.status)
        except (httpx.HTTPError, TimeoutError):
            # Never expose upstream exception text/URLs/credentials or retry.
            if started:
                raise
            await _deny(scope, receive, send, 'gateway_upstream_unavailable', 502)
        finally:
            try:
                if response is not None:
                    await finish_owned(response.aclose())
            finally:
                if admitted:
                    self._active -= 1
