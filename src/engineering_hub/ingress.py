"""Explicit per-listener application boundary, independent of proxy headers.

This is not a network firewall: operators must separately bind the browser
listener to the tunnel's private network and publish only the agent listener.
"""
import ipaddress
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from .cloudflare_access import AccessDenied, CloudflareAccessVerifier, _record_verified_access
from .unified_bus import is_bus_api, BUS_ADMIN_PATH, AGENT_SESSION_PREFIX
from agent_message_bus.agent_sessions_transport import (AgentSessionsBoundaryMiddleware, AgentSessionResponseGuardMiddleware, bearer, transport_bearer, session_origin_allowed, source_allowed, within)
from agent_message_bus.agent_sessions import AgentSessionError


def _within(path, prefix):
    return path == prefix or path.startswith(prefix + "/")


def validate_agent_hosts(hosts):
    """Exact MCP Host allowlist only; never a browser/proxy trust decision."""
    if type(hosts) is not tuple:
        raise ValueError("Agent allowed hosts must be an exact tuple")
    for value in hosts:
        if (not isinstance(value, str) or not value or not value.isascii()
                or value != value.lower() or any(c.isspace() or ord(c) < 33 for c in value)
                or any(c in value for c in "/\\@?#%*")):
            raise ValueError("Agent allowed hosts require exact host[:port] values")
        try:
            parsed = urlsplit("//" + value)
            hostname = parsed.hostname
            port = parsed.port
            if not hostname or parsed.username or parsed.password or (port is not None and not 1 <= port <= 65535):
                raise ValueError
            if ":" in hostname:
                ipaddress.IPv6Address(hostname)
                if value != "[" + hostname + "]" + (":" + str(port) if port is not None else ""):
                    raise ValueError
            else:
                if (not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname)
                        or len(hostname) > 253
                        or value != hostname + (":" + str(port) if port is not None else "")):
                    raise ValueError
        except ValueError:
            raise ValueError("Agent allowed hosts require exact host[:port] values") from None
    if len(set(hosts)) != len(hosts):
        raise ValueError("Duplicate agent allowed host")


class HubIngressMiddleware:
    def __init__(self, app, *, mode, agent_auth, cloudflare_config=None, legacy_bus_enabled=False,
                 access_verifier=None, agent_sessions=None, agent_sessions_config=None,
                 browser_auth_mode='cloudflare_access', browser_auth_config=None, browser_sessions=None):
        if browser_auth_mode not in {'cloudflare_access', 'github'} or (browser_auth_mode == 'github'
                and (cloudflare_config is not None or access_verifier is not None)):
            raise ValueError('explicit_browser_auth_mode_required')
        if access_verifier is not None:
            if (mode != 'browser' or type(access_verifier) is not CloudflareAccessVerifier
                    or cloudflare_config is None or access_verifier.config != cloudflare_config):
                raise ValueError('explicit_access_verifier_binding_required')
        self.app, self.mode, self.agent_auth = app, mode, agent_auth
        if type(legacy_bus_enabled) is not bool:
            raise ValueError('Explicit bus ingress enable flag required')
        self.legacy_bus_enabled = legacy_bus_enabled
        if (agent_sessions is None) != (agent_sessions_config is None):
            raise ValueError('explicit_agent_session_composition_required')
        self.agent_sessions, self.agent_sessions_config = agent_sessions, agent_sessions_config
        self.session_boundary = AgentSessionsBoundaryMiddleware(app, auth=agent_auth,
            repository=agent_sessions, config=agent_sessions_config)
        self.session_response_guard = AgentSessionResponseGuardMiddleware(app,
            auth=agent_auth, repository=agent_sessions)
        if mode not in {"agents", "browser"}:
            raise ValueError("Separated Hub ingress mode required")
        self.checker = (access_verifier if access_verifier is not None else
                        CloudflareAccessVerifier(cloudflare_config)) if mode == "browser" and browser_auth_mode == 'cloudflare_access' else None
        self.cloudflare_config = cloudflare_config
        self.browser_auth_mode = browser_auth_mode
        if browser_auth_mode == 'github':
            from .auth import AuthConfig, SessionStore
            if (mode != 'browser' or type(browser_auth_config) is not AuthConfig
                    or browser_auth_config.human_auth_mode != 'github'
                    or type(browser_sessions) is not SessionStore):
                raise ValueError('explicit_browser_session_binding_required')
        self.browser_auth_config, self.browser_sessions = browser_auth_config, browser_sessions

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def deny(code="ingress_forbidden", status=403):
            await JSONResponse({"detail": {"code": code}}, status_code=status,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)

        path = scope.get("path", "")
        raw = scope.get("raw_path", path.encode("utf-8"))
        # Reject alternate spellings rather than relying on differing proxy,
        # ASGI server and router normalization. Queries are not part of raw_path.
        if (b"%" in raw or "\\" in path or "//" in path
                or any(segment in {".", ".."} for segment in path.split("/"))):
            await deny()
            return
        authorizations = [value for name, value in scope.get("headers", [])
                          if name.lower() == b"authorization"]
        if len(authorizations) > 1:
            await deny("unauthorized", 401)
            return
        if self.mode == "browser" and authorizations:
            await deny("browser_credentials_required")
            return
        if path == "/health" and scope.get("method") in {"GET", "HEAD"}:
            await self.app(scope, receive, send)
            return
        if self.mode == "agents":
            if within(path, AGENT_SESSION_PREFIX):
                await self.session_boundary(scope, receive, send)
                return
            bus_api = self.legacy_bus_enabled and is_bus_api(path)
            bus_admin = self.legacy_bus_enabled and path == BUS_ADMIN_PATH
            if (_within(path, "/api/hub/v1/auth")
                    or not (_within(path, "/mcp") or _within(path, "/api/hub/v1")
                            or bus_api or bus_admin)):
                await deny()
                return
            if len(authorizations) != 1:
                await deny("unauthorized", 401)
                return
            try:
                # Existing identity and source-IP checks, never proxy claims.
                request = Request(scope)
                token = transport_bearer(request)
                if self.agent_sessions is not None and token.lower().startswith('ehs_'):
                    token = bearer(request)
                    if path.rstrip('/') != '/mcp' or self.agent_sessions is None:
                        raise HTTPException(401)
                    if (not session_origin_allowed(request, self.agent_sessions_config)
                            or not source_allowed(request, self.agent_auth)):
                        raise HTTPException(403)
                    principal = self.agent_sessions.authenticate_session(token)
                    scope.setdefault('state', {})['agent_session_principal'] = principal
                elif bus_admin:
                    self.agent_auth.admin(request)
                else:
                    self.agent_auth.agent(request)
            except (HTTPException, AgentSessionError) as error:
                await deny("forbidden" if getattr(error, "status_code", 401) == 403 else "unauthorized",
                           403 if getattr(error, "status_code", 401) == 403 else 401)
                return
        else:
            if _within(path, "/mcp") or is_bus_api(path) or _within(path, '/api/admin'):
                await deny()
                return
            if self.browser_auth_mode == 'github':
                # Protect new routes by default. Only the fixed public login
                # shell/assets and OAuth handshake bypass the human session.
                public_auth = {('/api/hub/v1/auth/login', 'GET'),
                               ('/api/hub/v1/auth/callback', 'GET'),
                               ('/api/hub/v1/auth/me', 'GET')}
                method = scope.get('method')
                # This namespace is reserved for public packaged UI assets;
                # never register private dynamic handlers under these names.
                asset = re.fullmatch(r'/assets/[A-Za-z0-9_.-]{1,200}\.(?:js|css|svg|woff2|png|webp)', path)
                public_shell = (method in {'GET', 'HEAD'} and
                                (path in {'/', '/index.html', '/favicon.ico', '/hub.svg'}
                                 or asset is not None))
                if not public_shell and (path, method) not in public_auth:
                    from .auth import require_human
                    try:
                        require_human(Request(scope), self.browser_auth_config, self.browser_sessions,
                                      mutation=scope.get('method') not in {'GET', 'HEAD'})
                    except HTTPException as error:
                        await deny('browser_credentials_required', error.status_code)
                        return
                await self.app(scope, receive, send)
                return
            assertions = [value for name, value in scope.get("headers", [])
                          if name.lower() == b"cf-access-jwt-assertion"]
            if len(assertions) != 1:
                await deny("access_required", 401)
                return
            try:
                identity = await self.checker.verify(assertions[0].decode("ascii"))
            except (AccessDenied, UnicodeDecodeError):
                await deny("access_required", 401)
                return
            except Exception:
                # Cancellation propagates; verifier failures never serve assets.
                await deny("access_unavailable", 503)
                return
            scope.setdefault("state", {})["cloudflare_identity"] = identity
            _record_verified_access(scope, self.cloudflare_config, identity)
        if self.mode == 'agents' and path.rstrip('/') == '/mcp':
            await self.session_response_guard(scope, receive, send)
        else:
            await self.app(scope, receive, send)
