from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as StarletteJSONResponse

from .api import Service, policy_from_env, router
from .audit import AuditLog
from .auth import AuthSettings
from .database import MessageRepository
from .mcp_server import build_mcp
from .retention import cleanup


class MCPAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, auth: AuthSettings):
        super().__init__(app); self.auth = auth

    async def dispatch(self, request: Request, call_next):
        # The MCP Starlette app is the base app so its session-manager lifespan is retained.
        # REST and health are intentionally mounted alongside it and have their own auth.
        if request.url.path.rstrip("/").endswith("/mcp"):
            try:
                self.auth.agent(request)
            except Exception as exc:
                status_code = getattr(exc, "status_code", 401)
                detail = getattr(exc, "detail", "invalid bearer token")
                return JSONResponse({"detail": detail}, status_code=status_code,
                                    headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else {})
        return await call_next(request)


def create_app(*, db_path: str | Path | None = None, audit_path: str | Path | None = None,
               auth: AuthSettings | None = None):
    db_path = db_path or os.environ.get("AGENT_BUS_DB", "data/messages.sqlite3")
    audit_path = audit_path or os.environ.get("AGENT_BUS_AUDIT", "data/audit.jsonl")
    auth = auth or AuthSettings.from_env()
    repository = MessageRepository(db_path, lease_seconds=int(os.environ.get("CLAIM_LEASE_SECONDS", "600")))
    audit = AuditLog(audit_path)
    service = Service(repository, audit, auth, policy_from_env())
    cleanup(repository, service.policy)
    mcp = build_mcp(service)
    app = mcp.streamable_http_app()  # Base app preserves FastMCP's session-manager lifespan.
    app.add_middleware(MCPAuthMiddleware, auth=auth)

    # FastAPI owns the REST dependency/validation layer, while the MCP app remains the root ASGI app.
    rest = FastAPI(title="Agent Message Bus REST", docs_url=None, redoc_url=None)
    rest.include_router(router(service))
    app.mount("/", rest)

    async def health(request: Request):
        try:
            with repository.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            db_status = "ok"
        except Exception:
            db_status = "error"
        return StarletteJSONResponse({"ok": db_status == "ok", "database": db_status,
                                      "schema_version": 1,
                                      "audit": "error" if audit.last_error else ("ok" if audit.verify() else "invalid")})

    # Keep this before the catch-all REST mount.
    app.routes.insert(0, __import__("starlette.routing", fromlist=["Route"]).Route("/health", health, methods=["GET"]))
    app.state.repository = repository
    app.state.audit = audit
    app.state.service = service
    return app


app = create_app()
