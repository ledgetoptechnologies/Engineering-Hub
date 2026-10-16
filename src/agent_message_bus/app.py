from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from importlib.metadata import version

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as StarletteJSONResponse

from .api import Service, policy_from_env, router
from .audit import AuditLog, correlation_id_context, request_id_context
from .auth import AuthSettings
from .database import MessageRepository
from .files import FileRepository
from .mcp_server import build_mcp
from .retention import cleanup
from .semantic import SemanticIndex


class OperationalLoggingMiddleware(BaseHTTPMiddleware):
    """Log request metadata only; never inspect headers, query strings, or bodies wholesale."""

    def __init__(self, app, auth: AuthSettings):
        super().__init__(app)
        self.auth = auth
        self.logger = logging.getLogger("agent_message_bus.http")

    def _identifier(self, value: str | None) -> str | None:
        if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            return None
        # Do not echo a bearer token if a client mistakenly reuses one as a trace ID.
        return None if self.auth.identity_for(value) else value

    async def dispatch(self, request: Request, call_next):
        request_id = self._identifier(request.headers.get("x-request-id")) or uuid.uuid4().hex
        correlation_id = self._identifier(request.headers.get("x-correlation-id")) or request_id
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id
        request_token = request_id_context.set(request_id)
        correlation_token = correlation_id_context.set(correlation_id)
        started = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        finally:
            record = {
                "event": "http.request",
                "request_id": request_id,
                "correlation_id": correlation_id,
                "method": request.method,
                "path": ("/api/files/{path}" if request.url.path.startswith("/api/files/")
                         else "/api/feedback/{id}" if request.url.path.startswith("/api/feedback/")
                         else request.url.path),
                "status_code": response.status_code if response is not None else 500,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            identity = getattr(request.state, "agent_identity", None)
            if identity:
                record["agent"] = identity
            self.logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
            correlation_id_context.reset(correlation_token)
            request_id_context.reset(request_token)


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
               auth: AuthSettings | None = None, file_root: str | Path | None = None):
    db_path = db_path or os.environ.get("AGENT_BUS_DB", "data/messages.sqlite3")
    audit_path = audit_path or os.environ.get("AGENT_BUS_AUDIT", "data/audit.jsonl")
    file_root = file_root or os.environ.get("AGENT_BUS_FILE_ROOT", "data/files")
    auth = auth or AuthSettings.from_env()
    # Uvicorn configures its own loggers, not the application's logger tree.
    operational_logger = logging.getLogger("agent_message_bus")
    operational_logger.setLevel(logging.INFO)
    if not operational_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        operational_logger.addHandler(handler)
    repository = MessageRepository(db_path, lease_seconds=int(os.environ.get("CLAIM_LEASE_SECONDS", "600")))
    files = FileRepository(db_path, file_root)
    audit = AuditLog(audit_path)
    semantic = SemanticIndex(db_path, file_root)
    service = Service(repository, audit, auth, policy_from_env(), files, semantic)
    cleanup(repository, service.policy)
    mcp = build_mcp(service)
    app = mcp.streamable_http_app()  # Base app preserves FastMCP's session-manager lifespan.
    app.add_middleware(MCPAuthMiddleware, auth=auth)
    app.add_middleware(OperationalLoggingMiddleware, auth=auth)

    # FastAPI owns the REST dependency/validation layer, while the MCP app remains the root ASGI app.
    rest = FastAPI(title="Agent Message Bus REST", docs_url=None, redoc_url=None)
    @rest.exception_handler(RequestValidationError)
    async def safe_feedback_validation(request, exc):
        if request.url.path.startswith("/api/feedback"):
            return JSONResponse({"detail": {"code": "invalid_feedback", "message": "feedback input is invalid"}}, status_code=422)
        return await request_validation_exception_handler(request, exc)

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
                                      "schema_version": 3,
                                      "version": version("agent-message-bus"),
                                      "audit": "error" if audit.last_error else ("ok" if audit.verify() else "invalid"),
                                      "semantic": {"state": "enabled" if semantic.enabled else "disabled"}})

    # Keep this before the catch-all REST mount.
    app.routes.insert(0, __import__("starlette.routing", fromlist=["Route"]).Route("/health", health, methods=["GET"]))
    app.state.repository = repository
    app.state.audit = audit
    app.state.service = service
    app.state.files = files
    return app


app = create_app()
