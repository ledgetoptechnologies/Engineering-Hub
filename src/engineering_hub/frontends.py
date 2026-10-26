"""Two explicitly separated front doors over one Hub owner and lifecycle.

Run both ASGI servers with lifespan disabled inside ``pair.lifespan()``.
This composes application boundaries; it does not bind sockets, establish a
tunnel, or permit another process to open an owner-enrolled database.
"""
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.responses import JSONResponse

from .app import Settings
from .ingress import HubIngressMiddleware


class _FrontDoor:
    def __init__(self, pair, mode):
        self.pair = pair
        self.mode = mode
        self.ingress = HubIngressMiddleware(
            pair.core, mode=mode, agent_auth=pair.settings.agent_auth,
            cloudflare_config=pair.settings.auth_config.cloudflare_config,
            legacy_bus_enabled=getattr(pair.core.state, 'embedded_bus', None) is not None,
            access_verifier=pair.access_verifier if mode == 'browser' else None,
        )

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            # Never let two servers independently start the feed, MCP manager,
            # pilot owner or dispatcher. The caller must own the shared context.
            raise RuntimeError("Use the shared Hub frontend lifespan and disable server lifespans")
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http":
            raise RuntimeError("Unsupported Hub frontend scope")
        if self.pair.state != "active":
            await JSONResponse({"detail": {"code": "hub_not_ready"}}, status_code=503,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        # Static arbitrary website code has a separate origin and a short-lived
        # scoped capability. It never enters the Hub cookie/Access middleware.
        # The agent listener has no such exception. Exact Host and ticket checks
        # remain inside the preview app; forwarded host headers are ignored.
        preview = getattr(self.pair.core.state, 'preview_app', None)
        origin = getattr(self.pair.core.state, 'preview_origin', None)
        if self.mode == 'browser' and preview is not None and origin is not None:
            hosts = [v for k, v in scope.get('headers', []) if k.lower() == b'host']
            if hosts == [urlsplit(origin).netloc.encode('ascii')]:
                await preview(scope, receive, send)
                return
        await self.ingress(scope, receive, send)


class HubFrontends:
    """One-use lifecycle coordinator; both front doors share the exact core.

    The core must be newly created and must not also be served directly or have
    its lifespan managed elsewhere. There is no owner transfer or retry here.
    A failed/closed pair requires a new process/core under normal owner rules.
    """

    def __init__(self, core, *, access_verifier=None):
        if not isinstance(core, FastAPI):
            raise ValueError("Hub frontends require one existing FastAPI Hub core")
        settings = getattr(core.state, "settings", None)
        if (not isinstance(settings, Settings) or settings.ingress_mode != "combined"
                or settings.auth_config.human_auth_mode != "cloudflare_access"
                or settings.auth_config.cloudflare_config is None):
            raise ValueError("Shared Hub core requires combined ingress and configured Cloudflare Access")
        if getattr(core.state, "hub_frontends", None) is not None:
            raise ValueError("Hub core already has a frontend lifecycle coordinator")
        if access_verifier is not None:
            from .cloudflare_access import CloudflareAccessVerifier
            if (type(access_verifier) is not CloudflareAccessVerifier
                    or access_verifier.config != settings.auth_config.cloudflare_config):
                raise ValueError('explicit_access_verifier_binding_required')
        self.core, self.settings = core, settings
        self.access_verifier = access_verifier
        self.state = "new"
        self.agents = _FrontDoor(self, "agents")
        self.browser = _FrontDoor(self, "browser")
        core.state.hub_frontends = self

    def close_admission(self):
        """Deny new HTTP requests without closing the shared core lifespan."""
        if self.state not in {'active', 'stopping'}:
            raise RuntimeError('Hub frontend admission is not active')
        self.state = 'stopping'

    @asynccontextmanager
    async def lifespan(self):
        if self.state != "new":
            raise RuntimeError("Hub frontend lifespan is one-use and must have one owner")
        # No await before reserving lifecycle ownership; concurrent callers in
        # this event loop cannot both enter the core's lifespan.
        self.state = "starting"
        try:
            async with self.core.router.lifespan_context(self.core):
                self.state = "active"
                try:
                    yield self
                finally:
                    self.state = "stopping"
        except BaseException:
            # Preserve cancellation and startup/shutdown failures verbatim for
            # the supervisor, never reopen the ledger or invent readiness.
            self.state = "failed"
            raise
        else:
            self.state = "closed"
