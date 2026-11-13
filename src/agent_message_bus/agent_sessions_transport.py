"""Opt-in session transport isolation; never grants legacy family authority."""
import asyncio
import json
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from .agent_sessions import AgentSessionError
from .agent_sessions_api import CLIENT_DOWNLOAD_PATHS

PREFIX = '/api/agent-sessions/v1'
MAX_NEW_TRANSPORTS = 1024  # Concurrent SDK transports in the isolated EHS manager.
MAX_NEW_STREAMS = 128
MAX_STREAMS_PER_TRANSPORT = 2
MAX_COMPLETED_POSTS_PER_TRANSPORT = 1


def within(path, prefix=PREFIX):
    return path == prefix or path.startswith(prefix + '/')


def bearer(request):
    values = request.headers.getlist('authorization')
    if (len(values) != 1 or len(values[0]) > 512
            or re.fullmatch(r'(?i:Bearer) [^\s,]+', values[0]) is None):
        raise HTTPException(401, detail={'code': 'agent_session_required'})
    return values[0].partition(' ')[2]


def transport_bearer(request):
    """Preserve AuthSettings' raw legacy token partition; reject duplicates."""
    values = request.headers.getlist('authorization')
    if len(values) != 1:
        raise HTTPException(401, detail={'code': 'agent_session_required'})
    scheme, separator, token = values[0].partition(' ')
    if scheme.lower() != 'bearer' or not separator or not token:
        raise HTTPException(401, detail={'code': 'agent_session_required'})
    return token


def source_allowed(request, auth):
    return not auth.allowed_ips or (request.client is not None and request.client.host in auth.allowed_ips)


def session_origin_allowed(request, config):
    return request.headers.getlist('host') == [urlsplit(config.transport_origin).netloc]


async def denied(scope, receive, send, code='unauthorized', status=401):
    await JSONResponse({'detail': {'code': code}}, status_code=status,
        headers={'Cache-Control': 'no-store', **({'WWW-Authenticate': 'Bearer'} if status == 401 else {})})(scope, receive, send)


async def session_post(scope, receive, send, bridge_tools=()):
    """Bound and gate new-principal MCP methods without changing SDK handlers."""
    body = bytearray()
    while True:
        message = await receive()
        if message['type'] == 'http.disconnect':
            return None
        if message['type'] != 'http.request':
            return None
        body.extend(message.get('body', b''))
        if len(body) > 65536:
            await denied(scope, receive, send, 'request_too_large', 413)
            return None
        if not message.get('more_body', False):
            break
    try:
        value = json.loads(body)
        if type(value) is not dict or type(value.get('method')) is not str:
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError):
        await denied(scope, receive, send, 'invalid_mcp_request', 400)
        return None
    method = value['method']
    allowed = {'initialize', 'notifications/initialized', 'notifications/cancelled',
               'notifications/progress', 'ping', 'tools/list', 'tools/call'}
    tools = {'session_identity', 'session_peers', 'session_message_send', 'session_messages',
             'session_message_get', 'session_message_claim', 'session_message_renew',
             'session_message_acknowledge'} | set(bridge_tools)
    if method not in allowed:
        await JSONResponse({'jsonrpc': '2.0', 'id': value.get('id'),
            'error': {'code': -32601, 'message': 'session_method_not_available'}},
            headers={'Cache-Control': 'no-store'})(scope, receive, send)
        return None
    if method == 'tools/call' and (type(value.get('params')) is not dict or value['params'].get('name') not in tools):
        await JSONResponse({'jsonrpc': '2.0', 'id': value.get('id'), 'result': {
            'isError': True, 'content': [{'type': 'text', 'text': 'legacy_authority_required'}]}},
            headers={'Cache-Control': 'no-store'})(scope, receive, send)
        return None
    delivered = False
    async def replay():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
        return await receive()
    return replay


class IsolatedSessionTransportMiddleware:
    """Route only already-authenticated EHS requests to their bounded manager.

    Authentication and the session-owner check must wrap this middleware; the
    principal is ASGI state set by MCPAuthMiddleware, never a client header.
    The existing legacy Starlette app and its session manager remain unchanged.
    """
    def __init__(self, app, *, manager):
        self.app, self.manager = app, manager

    async def __call__(self, scope, receive, send):
        from .agent_sessions import SessionPrincipal
        principal = scope.get('state', {}).get('agent_session_principal')
        if (scope['type'] == 'http' and scope.get('path', '').rstrip('/') == '/mcp'
                and isinstance(principal, SessionPrincipal)):
            if scope.get('path') != '/mcp':
                return await denied(scope, receive, send, 'not_found', 404)
            return await self.manager.handle_request(scope, receive, send)
        return await self.app(scope, receive, send)


class AgentSessionsBoundaryMiddleware:
    """Guard direct bus REST as well as an embedded Hub's explicit front door."""
    def __init__(self, app, *, auth, repository=None, config=None):
        self.app, self.auth, self.repository, self.config = app, auth, repository, config

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = scope.get('path', '')
        values = request.headers.getlist('authorization')
        try:
            candidate = transport_bearer(request)
        except HTTPException:
            candidate = ''
        is_new = self.repository is not None and candidate.lower().startswith(('ehs_', 'ehi_', 'ehc_'))
        if within(path):
            if self.repository is None or self.config is None:
                return await denied(scope, receive, send, 'not_found', 404)
            if (not session_origin_allowed(request, self.config) or not source_allowed(request, self.auth)
                    or b'%' in scope.get('raw_path', b'') or '//' in path or '\\' in path
                    or any(part in {'.', '..'} for part in path.split('/'))):
                return await denied(scope, receive, send, 'forbidden', 403)
            try:
                if path in CLIENT_DOWNLOAD_PATHS and scope['method'] in {'GET', 'HEAD'}:
                    # Only fixed public bootstrap bytes; Host/IP/raw-path guards
                    # above still apply. No principal or enrollment is created.
                    pass
                elif path == PREFIX + '/enrollments' and scope['method'] == 'POST':
                    if values:
                        raise HTTPException(401)
                elif path == PREFIX + '/exchange' and scope['method'] == 'POST':
                    if not bearer(request).startswith('ehi_'):
                        raise HTTPException(401)
                else:
                    token = bearer(request)
                    if not token.startswith('ehs_'):
                        raise HTTPException(401)
                    principal = self.repository.authenticate_session(token)
                    scope.setdefault('state', {})['agent_session_principal'] = principal
            except (HTTPException, AgentSessionError):
                return await denied(scope, receive, send)
        elif is_new:
            if path.rstrip('/') != '/mcp':
                return await denied(scope, receive, send)
            # MCP session credentials use the selected private transport Host
            # too; otherwise the same bearer can fall back to the public owner
            # hostname even when the helper was enrolled for VPN-only access.
            if (self.config is None or not session_origin_allowed(request, self.config)
                    or not source_allowed(request, self.auth)):
                return await denied(scope, receive, send, 'forbidden', 403)
        await self.app(scope, receive, send)


class MCPAuthMiddleware:
    """One SDK server, with legacy family owners or exact installation/session owners.

    Session GET cancellation targets only the owned downstream request. Timers
    and commit-time callbacks are removed by their own registration generation.
    No polling, listener replacement or legacy resource subscription is added.
    """
    def __init__(self, app, auth, push_publisher, agent_sessions=None, on_owner=None):
        self.app, self.auth, self.push_publisher = app, auth, push_publisher
        self.agent_sessions = agent_sessions
        self.session_owners = {}
        self._new_records = {}
        self._new_pending = 0
        if on_owner is not None:
            on_owner(self)

    def _drop(self, sid, record):
        if self._new_records.get(sid) is not record:
            return
        self._new_records.pop(sid, None)
        if self.session_owners.get(sid) == record['owner']:
            self.session_owners.pop(sid, None)
        record['timer'].cancel()
        record['unsubscribe']()

    def _register(self, sid, owner, token):
        loop = asyncio.get_running_loop()
        record = {'owner': owner}
        def revoke():
            loop.call_soon_threadsafe(self._drop, sid, record)
        principal, unsubscribe = self.agent_sessions.subscribe_session(token, revoke)
        record['unsubscribe'] = unsubscribe
        record['timer'] = loop.call_later(max(0, principal.expires_at - self.agent_sessions.clock()), self._drop, sid, record)
        self.session_owners[sid] = owner
        self._new_records[sid] = record


    def close(self):
        for sid, record in list(self._new_records.items()):
            self._drop(sid, record)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            try:
                return await self.app(scope, receive, send)
            finally:
                self.close()
        if scope['type'] != 'http' or scope.get('path', '').rstrip('/') != '/mcp':
            return await self.app(scope, receive, send)
        request = Request(scope)
        try:
            token = transport_bearer(request)
            if self.agent_sessions is not None and token.lower().startswith(('ehs_', 'ehi_', 'ehc_')):
                token = bearer(request)
            new = self.agent_sessions is not None and token.lower().startswith('ehs_')
            if new:
                if self.agent_sessions is None:
                    raise HTTPException(401)
                if not source_allowed(request, self.auth):
                    raise HTTPException(403)
                principal = self.agent_sessions.authenticate_session(token)
                owner = ('installation-session', principal.installation_id, principal.session_id)
                scope.setdefault('state', {})['agent_session_principal'] = principal
            else:
                owner = self.auth.agent(request)
        except (HTTPException, AgentSessionError) as exc:
            status = 403 if getattr(exc, 'status_code', 401) == 403 else 401
            return await denied(scope, receive, send, 'forbidden' if status == 403 else 'unauthorized', status)
        ids = request.headers.getlist('mcp-session-id')
        if len(ids) > 1 or (ids and (not ids[0] or len(ids[0]) > 256)):
            return await denied(scope, receive, send, 'session_not_found', 404)
        sid = ids[0] if ids else None
        if (sid and self.session_owners.get(sid) != owner) or (not sid and scope['method'] in {'GET', 'DELETE'}):
            return await denied(scope, receive, send, 'session_not_found', 404)
        if new and scope['method'] == 'POST':
            replay = await session_post(scope, receive, send,
                tuple(getattr(self.agent_sessions, '_hub_bridge_tools', ()))
                + tuple(getattr(self.agent_sessions, '_infrastructure_metric_tools', ())))
            if replay is None:
                return
            receive = replay
        opening = new and not sid
        if opening:
            # An SDK transport can terminate without a matching middleware
            # DELETE. Retain its owner record until credential expiry/revoke,
            # but bound those records and outstanding opens independently.
            if len(self._new_records) + self._new_pending >= MAX_NEW_TRANSPORTS:
                return await denied(scope, receive, send, 'session_transport_limit', 429)
            self._new_pending += 1
        if not new and scope['method'] == 'GET':
            state = scope.setdefault('state', {})
            state['mcp_presence_agent'], state['mcp_presence_session_id'] = owner, sid
        status = None
        suppressed = False
        async def observed(message):
            nonlocal status, suppressed
            if suppressed:
                return
            if message['type'] == 'http.response.start':
                status = message['status']
                if scope['method'] == 'POST' and status < 400:
                    server_ids = [v.decode('ascii') for k,v in message.get('headers', []) if k.lower() == b'mcp-session-id']
                    if len(server_ids) == 1:
                        issued = server_ids[0]
                        if issued in self.session_owners and self.session_owners[issued] != owner:
                            raise RuntimeError('mcp_session_owner_conflict')
                        if issued not in self.session_owners:
                            if new:
                                try:
                                    self._register(issued, owner, token)
                                except AgentSessionError:
                                    suppressed = True
                                    status = 401
                                    await denied(scope, receive, send)
                                    return
                            else:
                                self.session_owners[issued] = owner
            await send(message)
        try:
            await self.app(scope, receive, observed)
        finally:
            if opening:
                self._new_pending -= 1
        if scope['method'] == 'DELETE' and sid and status == 200:
            if new:
                record = self._new_records.get(sid)
                if record is not None and record['owner'] == owner:
                    self._drop(sid, record)
            elif self.session_owners.get(sid) == owner:
                await self.push_publisher.drop_session(sid)
                self.session_owners.pop(sid, None)


class AgentSessionResponseGuardMiddleware:
    """Guard the actual GET/POST response outside response-buffering middleware.

    Each callback owns only its one downstream request task. The exact-repository
    marker avoids duplicate registration through the Hub and embedded bus layers.
    No SDK owner, resource subscription or family identity is created here.
    """
    def __init__(self, app, *, auth, repository=None):
        self.app, self.auth, self.repository = app, auth, repository
        self._active = {}
        self._completed_posts = {}
        self._total = 0

    async def __call__(self, scope, receive, send):
        if (self.repository is None or scope['type'] != 'http'
                or scope.get('path', '').rstrip('/') != '/mcp'
                or scope.get('method') not in {'GET', 'POST'}):
            return await self.app(scope, receive, send)
        state = scope.setdefault('state', {})
        if state.get('_agent_session_response_guard') is self.repository:
            return await self.app(scope, receive, send)
        request = Request(scope)
        try:
            token = transport_bearer(request)
        except HTTPException:
            # The ordinary authentication owner emits its existing denial.
            return await self.app(scope, receive, send)
        if not token.lower().startswith('ehs_'):
            return await self.app(scope, receive, send)
        try:
            token = bearer(request)
        except HTTPException:
            return await denied(scope, receive, send)
        if not source_allowed(request, self.auth):
            return await denied(scope, receive, send, 'forbidden', 403)
        loop = asyncio.get_running_loop()
        task = None
        invalidated = False
        started = complete = False
        promoted = False
        def stop():
            nonlocal invalidated
            invalidated = True
            if task is not None and not task.done():
                task.cancel()
        def revoke():
            loop.call_soon_threadsafe(stop)
        try:
            principal, unsubscribe = self.repository.subscribe_session(token, revoke)
        except AgentSessionError:
            return await denied(scope, receive, send)
        key = (principal.installation_id, principal.session_id)
        if self._total >= MAX_NEW_STREAMS or self._active.get(key, 0) >= MAX_STREAMS_PER_TRANSPORT:
            unsubscribe()
            return await denied(scope, receive, send, 'session_transport_limit', 429)
        timer = loop.call_later(max(0, principal.expires_at - self.repository.clock()), stop)
        self._total += 1
        self._active[key] = self._active.get(key, 0) + 1
        state['_agent_session_response_guard'] = self.repository
        async def observed(message):
            nonlocal started, complete, promoted
            if invalidated:
                raise asyncio.CancelledError
            await send(message)
            if message['type'] == 'http.response.start':
                started = True
            elif message['type'] == 'http.response.body' and not message.get('more_body', False):
                complete = True
                # Retain the task and global slot while freeing one response slot.
                if (scope['method'] == 'POST' and not promoted
                        and self._completed_posts.get(key, 0) < MAX_COMPLETED_POSTS_PER_TRANSPORT):
                    promoted = True
                    remaining = self._active[key] - 1
                    if remaining:
                        self._active[key] = remaining
                    else:
                        self._active.pop(key, None)
                    self._completed_posts[key] = self._completed_posts.get(key, 0) + 1
        try:
            task = asyncio.create_task(self.app(scope, receive, observed))
            await task
        except asyncio.CancelledError:
            if not invalidated:
                raise
        finally:
            def release(_task=None):
                timer.cancel()
                unsubscribe()
                self._total -= 1
                counts = self._completed_posts if promoted else self._active
                remaining = counts[key] - 1
                if remaining:
                    counts[key] = remaining
                else:
                    counts.pop(key, None)
                if state.get('_agent_session_response_guard') is self.repository:
                    state.pop('_agent_session_response_guard', None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await asyncio.gather(task, return_exceptions=True)
                finally:
                    if task.done():
                        release()
                    else:
                        # Repeated cancellation cannot release a live task.
                        task.add_done_callback(release)
            else:
                release()
        if invalidated and not complete:
            if started:
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            else:
                await denied(scope, receive, send)
