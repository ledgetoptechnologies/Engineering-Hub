from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import urlsplit

from importlib.metadata import version

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as StarletteJSONResponse

from .api import Service, policy_from_env, router
from .audit import AuditLog, correlation_id_context, request_id_context
from .auth import AuthSettings
from .database import MessageRepository
from .files import FileRepository
from .mcp_server import build_mcp
from .push import PushPublisher
from .presence import MCPStreamPresenceMiddleware
from .sqlite_events import MessageCursorReader, SQLiteEventSource, native_event_support
from .retention import cleanup
from .agent_sessions_transport import (MCPAuthMiddleware, AgentSessionsBoundaryMiddleware,
    AgentSessionResponseGuardMiddleware)
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
        return None if value.lower().startswith(("ehs_", "ehi_", "ehc_")) or self.auth.identity_for(value) else value

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
                "path": ("/{redacted}" if re.search(r"(?i)(?:ehs_|ehi_|ehc_)", request.url.path)
                         else "/api/agent-sessions/v1/{path}" if request.url.path.startswith("/api/agent-sessions/v1/")
                         else "/api/files/{path}" if request.url.path.startswith("/api/files/")
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


def create_app(*, db_path: str | Path | None = None, audit_path: str | Path | None = None,
               auth: AuthSettings | None = None, file_root: str | Path | None = None,
               push_events_operational: bool | None = None,
               transport_security_settings: TransportSecuritySettings | None = None,
               startup_cleanup: bool = True, agent_sessions_config=None):
    # Validate before any database, file or retention work so a bad embedding
    # configuration fails without side effects.
    if transport_security_settings is not None and type(transport_security_settings) is not TransportSecuritySettings:
        raise ValueError("transport_security_settings must be a TransportSecuritySettings or None")
    if type(startup_cleanup) is not bool:
        raise ValueError("startup_cleanup must be True or False")
    db_path = db_path or os.environ.get("AGENT_BUS_DB", "data/messages.sqlite3")
    audit_path = audit_path or os.environ.get("AGENT_BUS_AUDIT", "data/audit.jsonl")
    file_root = file_root or os.environ.get("AGENT_BUS_FILE_ROOT", "data/files")
    auth = auth or AuthSettings.from_env()
    from .agent_sessions_config import AgentSessionsConfig
    if agent_sessions_config is None:
        agent_sessions_config = AgentSessionsConfig.from_env()
    if agent_sessions_config is not None:
        if type(agent_sessions_config) is not AgentSessionsConfig:
            raise ValueError('explicit_staging_agent_sessions_required')
        if agent_sessions_config.environment == 'production' and not auth.allowed_ips:
            raise ValueError('production_agent_sessions_source_ip_allowlist_required')
        if any(value and value.lower().startswith(('ehs_', 'ehi_', 'ehc_')) for value in
               (auth.hermes_token, auth.codex_token, auth.claude_token, auth.admin_token)):
            raise ValueError('legacy_session_credential_prefix_collision')
        selected = agent_sessions_config.database.resolve()
        legacy = [Path(db_path).resolve(), Path(audit_path).resolve()]
        files_root = Path(file_root).resolve()
        if (selected in legacy or selected == files_root or files_root in selected.parents
                or any(selected.exists() and other.exists() and selected.samefile(other) for other in legacy)):
            raise ValueError('separate_agent_sessions_database_required')
    agent_sessions = None
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
    if startup_cleanup:
        # Skipping preserves expired claims and retention-eligible rows, e.g.
        # while a migrated copy awaits parity verification.
        cleanup(repository, service.policy)
    if push_events_operational is not None and not isinstance(push_events_operational, bool):
        raise ValueError("push_events_operational must be True (require), False (disable), or None (auto)")
    require_native_push = push_events_operational is True
    disable_native_push = push_events_operational is False
    push_publisher = PushPublisher(enabled=False)
    push_state: dict[str, str] = {
        "state": "disabled_configuration" if disable_native_push else "starting",
        "backend": "linux-inotify-sqlite-wal",
    }
    cursor_reader = MessageCursorReader()
    event_source: SQLiteEventSource | None = None
    if agent_sessions_config is not None:
        from .agent_sessions import AgentSessionRepository
        agent_sessions = AgentSessionRepository(agent_sessions_config.database, agent_sessions_config.owner)
    mcp = build_mcp(service, push_publisher=push_publisher)
    if agent_sessions is not None:
        from .agent_sessions_mcp import install_session_tools
        install_session_tools(mcp, agent_sessions)
    if agent_sessions_config is not None and agent_sessions_config.private_origin is not None:
        selected_security = transport_security_settings or mcp.settings.transport_security
        if type(selected_security) is not TransportSecuritySettings:
            raise ValueError("private_agent_session_transport_policy_required")
        private_host = urlsplit(agent_sessions_config.transport_origin).netloc
        transport_security_settings = TransportSecuritySettings(
            enable_dns_rebinding_protection=selected_security.enable_dns_rebinding_protection,
            allowed_hosts=list(dict.fromkeys([*(selected_security.allowed_hosts or []), private_host])),
            allowed_origins=list(selected_security.allowed_origins or []),
        )
    if transport_security_settings is not None:
        # Must precede streamable_http_app(): FastMCP copies these settings into
        # its session manager when it creates the HTTP app.
        mcp.settings.transport_security = transport_security_settings
    app = mcp.streamable_http_app()  # Base app preserves FastMCP's session-manager lifespan.
    original_lifespan = app.router.lifespan_context
    transport_owners = []
    def capture_transport_owner(owner):
        if transport_owners:
            raise RuntimeError('single_mcp_transport_owner_required')
        transport_owners.append(owner)

    @asynccontextmanager
    async def application_lifespan(root_app):
        nonlocal event_source
        async with original_lifespan(root_app):
            push_publisher.bind_loop(asyncio.get_running_loop())
            if not disable_native_push:
                supported, support_reason = native_event_support(repository.path)
                if not supported:
                    push_state.update(state="disabled_unsupported", reason=support_reason)
                    if require_native_push:
                        operational_logger.critical("Native MCP subscriptions required but unavailable: %s", support_reason)
                        raise RuntimeError(f"native MCP subscriptions required but unavailable: {support_reason}")
                    operational_logger.warning("Native MCP subscriptions disabled: %s", support_reason)
                else:
                    def source_failed(exc: BaseException) -> None:
                        push_state.update(state="failed", reason=type(exc).__name__)
                        push_publisher.fail_source(f"{type(exc).__name__}: {exc}")

                    def deliver_recipients(recipients, _cursor: int) -> None:
                        for recipient in recipients:
                            if not push_publisher.publish_committed_from_thread(recipient):
                                raise RuntimeError("native commit invalidation could not reach the app event loop")

                    event_source = SQLiteEventSource(
                        repository.path,
                        cursor_reader,
                        deliver_recipients,
                        on_error=source_failed,
                    )
                    app.state.push_event_source = event_source
                    try:
                        # Permit event scheduling before the watcher can report a
                        # commit. ASGI still cannot accept clients until startup
                        # completes; a failed source disables the publisher below.
                        push_publisher.activate()
                        # Watcher setup and the one head-only catch-up finish before
                        # ASGI startup completes and any client can negotiate capabilities.
                        await asyncio.to_thread(event_source.start)
                    except BaseException as exc:
                        push_state.update(state="failed", reason=type(exc).__name__)
                        push_publisher.fail_source(f"startup {type(exc).__name__}: {exc}")
                        raise
                    push_state.update(state="healthy", reason=support_reason)
            try:
                yield
            finally:
                try:
                    if event_source is not None and event_source.running:
                        try:
                            await asyncio.to_thread(event_source.stop)
                        except BaseException as exc:
                            push_state.update(state="failed", reason=type(exc).__name__)
                            push_publisher.fail_source(f"shutdown {type(exc).__name__}: {exc}")
                            raise
                finally:
                    for owner in transport_owners:
                        owner.close()

    app.router.lifespan_context = application_lifespan
    app.add_middleware(MCPAuthMiddleware, auth=auth, push_publisher=push_publisher,
                       agent_sessions=agent_sessions, on_owner=capture_transport_owner)
    app.add_middleware(AgentSessionsBoundaryMiddleware, auth=auth, repository=agent_sessions, config=agent_sessions_config)
    app.add_middleware(OperationalLoggingMiddleware, auth=auth)
    # This pure ASGI observer is outermost so it sees the actual response start,
    # body writes, and disconnect completion beyond BaseHTTPMiddleware wrappers.
    app.add_middleware(MCPStreamPresenceMiddleware, publisher=push_publisher)
    if agent_sessions is not None:
        # Outside BaseHTTPMiddleware so revocation owns the actual response,
        # including a POST stream or initialization headers blocked in send().
        app.add_middleware(AgentSessionResponseGuardMiddleware, auth=auth, repository=agent_sessions)

    # FastAPI owns the REST dependency/validation layer, while the MCP app remains the root ASGI app.
    rest = FastAPI(title="Agent Message Bus REST", docs_url=None, redoc_url=None)
    @rest.exception_handler(RequestValidationError)
    async def safe_feedback_validation(request, exc):
        if request.url.path.startswith("/api/agent-sessions/v1"):
            return JSONResponse({"detail": {"code": "invalid_agent_session_input"}}, status_code=422)
        if request.url.path.startswith("/api/feedback"):
            return JSONResponse({"detail": {"code": "invalid_feedback", "message": "feedback input is invalid"}}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    rest.include_router(router(service))
    if agent_sessions is not None:
        from .agent_sessions import AgentSessionError
        from .agent_sessions_api import session_router
        @rest.exception_handler(AgentSessionError)
        async def session_error(request, exc):
            return JSONResponse({'detail': {'code': exc.code}}, status_code=exc.status,
                                headers={'Cache-Control': 'no-store'})
        rest.include_router(session_router(agent_sessions))
    app.mount("/", rest)

    async def health(request: Request):
        try:
            with closing(repository.connect()) as conn, conn:
                conn.execute("SELECT 1").fetchone()
            db_status = "ok"
        except Exception:
            db_status = "error"
        push_health = {
            "state": push_state["state"],
            "subscriptions_enabled": push_publisher.enabled,
            "backend": push_state["backend"],
        }
        if "reason" in push_state:
            push_health["reason"] = push_state["reason"]
        if event_source is not None:
            event_stats = event_source.stats
            push_health["cursor"] = event_stats["cursor"]
            push_health["degraded"] = bool(event_stats.get("degraded", False))
            push_health["busy_retry_count"] = event_stats.get("busy_retry_count", 0)
            if event_stats.get("degraded") and push_health["state"] != "failed":
                push_health["state"] = "degraded"
            # Safe fixed-stage and canonical SQLite diagnostics, never the
            # source's exception text (which can contain SQL or file paths).
            for key in ("failure_stage", "sqlite_errorcode", "sqlite_errorname"):
                if event_stats.get(key) is not None:
                    push_health[key] = event_stats[key]
            if not event_source.healthy:
                push_health["state"] = "failed"
        return StarletteJSONResponse({"ok": db_status == "ok" and push_health["state"] not in {"failed", "degraded"},
                                      "database": db_status,
                                      "schema_version": 4,
                                      "version": version("agent-message-bus"),
                                      "audit": "error" if audit.last_error else ("ok" if audit.verify() else "invalid"),
                                      "semantic": {"state": "enabled" if semantic.enabled else "disabled"},
                                      "push": push_health})

    # Keep this before the catch-all REST mount.
    app.routes.insert(0, __import__("starlette.routing", fromlist=["Route"]).Route("/health", health, methods=["GET"]))
    app.state.agent_sessions = agent_sessions
    app.state.agent_sessions_config = agent_sessions_config
    app.state.repository = repository
    app.state.audit = audit
    app.state.service = service
    app.state.files = files
    app.state.push_publisher = push_publisher
    app.state.push_event_source = event_source
    app.state.push_status = push_state
    app.state.push_cursor_reader = cursor_reader
    app.state.mcp = mcp
    app.state.auth = auth
    return app


_default_app = None
_default_app_lock = threading.Lock()


def __getattr__(name: str):
    # Lazy default instance for `uvicorn agent_message_bus.app:app`. Importing
    # this module for create_app must not open the database, files or run cleanup.
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _default_app
    with _default_app_lock:
        if _default_app is None:
            _default_app = create_app()
        return _default_app
