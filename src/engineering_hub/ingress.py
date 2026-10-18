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
    def __init__(self, app, *, mode, agent_auth, cloudflare_config=None):
        self.app, self.mode, self.agent_auth = app, mode, agent_auth
        if mode not in {"agents", "browser"}:
            raise ValueError("Separated Hub ingress mode required")
        self.checker = CloudflareAccessVerifier(cloudflare_config) if mode == "browser" else None
        self.cloudflare_config = cloudflare_config

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
            if (_within(path, "/api/hub/v1/auth")
                    or not (_within(path, "/mcp") or _within(path, "/api/hub/v1"))):
                await deny()
                return
            if len(authorizations) != 1:
                await deny("unauthorized", 401)
                return
            try:
                # Existing identity and source-IP checks, never proxy claims.
                self.agent_auth.agent(Request(scope))
            except HTTPException as error:
                await deny("forbidden" if error.status_code == 403 else "unauthorized",
                           403 if error.status_code == 403 else 401)
                return
        else:
            if _within(path, "/mcp"):
                await deny()
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
        await self.app(scope, receive, send)
