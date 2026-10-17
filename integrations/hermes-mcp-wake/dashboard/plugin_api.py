"""FastAPI routes for the opt-in agent-bus-wake Hermes Desktop plugin."""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

from agent_bus_wake_runtime import StateStore, WakeError, WakeManager, configured_mcp_servers

from wake_backend import ASGIGateway, BackendDispatcher


def _owner_scope():
    from hermes_cli.web_server_profiles import _config_profile_scope
    return _config_profile_scope("default")


@asynccontextmanager
async def _lifespan(app):
    with _owner_scope():
        manager = _get_manager()
        if manager.state.enabled():
            try:
                # New subscription tasks inherit the explicit default-profile
                # secret scope; startup has no HTTP request scope of its own.
                await manager.start(wait_seconds=0.1)
            except Exception:
                manager._error = "restart_restore_failed"
    try:
        yield
    finally:
        await _shutdown()


router = APIRouter(lifespan=_lifespan)
_manager: WakeManager | None = None
_dispatcher: BackendDispatcher | None = None


def _enqueue(dispatch_id: str) -> None:
    global _dispatcher
    if _dispatcher is None:
        from hermes_cli import web_server
        if bool(getattr(web_server.app.state, "auth_required", False)):
            raise WakeError("This integration requires its explicitly owned loopback desktop backend")
        _dispatcher = BackendDispatcher(_get_manager(), ASGIGateway(
            web_server.app, lambda: web_server._SESSION_TOKEN))
    _dispatcher.enqueue(dispatch_id)


def _get_manager() -> WakeManager:
    global _manager
    if _manager is None:
        from hermes_constants import get_hermes_home
        state_path = Path(get_hermes_home()) / "state" / "agent_bus_wake.sqlite3"
        _manager = WakeManager(StateStore(state_path), config_loader=configured_mcp_servers, notifier=_enqueue)
    return _manager


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WakeError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="Agent Bus wake operation failed")


@router.get("/servers")
async def servers() -> dict[str, Any]:
    """Names only; configured URLs and authorization headers never leave the backend."""
    return {"servers": _get_manager().available_servers()}


@router.get("/status")
async def status() -> dict[str, Any]:
    return {**_get_manager().status(), "delivery_mode": "backend"}


@router.post("/start")
async def start(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        name = (payload or {}).get("server_name")
        return await _get_manager().start(name if name is not None else None)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/stop")
async def stop() -> dict[str, Any]:
    """Suspend the connection without clearing the user's saved opt-in."""
    try:
        return await _get_manager().stop(persist=False)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/pause")
async def pause() -> dict[str, Any]:
    """Explicitly disable and stop the listener until the user starts it again."""
    try:
        return await _get_manager().pause()
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/bindings")
async def bindings() -> dict[str, Any]:
    manager = _get_manager()
    return {"bindings": manager.binding_statuses()}


@router.post("/bindings")
async def add_binding(payload: dict[str, Any]) -> dict[str, Any]:
    manager = _get_manager()
    snapshot = manager.status()
    if snapshot.get("state") != "subscribed" or not snapshot.get("server_name") or not snapshot.get("recipient"):
        raise HTTPException(status_code=409, detail="Start the native inbox listener before binding a chat")
    topic = payload.get("topic")
    stored_session_id = payload.get("stored_session_id")
    owner = payload.get("owner")
    if not isinstance(owner, dict):
        raise HTTPException(status_code=422, detail="An exact focused-session owner is required")
    connection_id = owner.get("connection_id")
    profile = owner.get("profile")
    target_profile = owner.get("target_profile")
    if (connection_id, profile, target_profile) != ("local", "default", "default"):
        raise HTTPException(status_code=422, detail="Only local/default-profile chats can be linked")
    if not isinstance(stored_session_id, str) or not manager.validate_stored_session(stored_session_id):
        raise HTTPException(status_code=404, detail="The exact stored conversation was not found in the default profile")
    try:
        binding = manager.state.add_binding(
            server_name=str(snapshot["server_name"]), recipient=str(snapshot["recipient"]),
            topic=topic, stored_session_id=stored_session_id,
            connection_id=connection_id, profile=profile, target_profile=target_profile,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    presence = await manager.binding_added(binding)
    return {"binding": binding, "topic_presence": presence}


@router.delete("/bindings/{binding_id}")
async def remove_binding(binding_id: str) -> dict[str, Any]:
    manager = _get_manager()
    binding = next((row for row in manager.state.bindings() if row["id"] == binding_id), None)
    if binding is None:
        raise HTTPException(status_code=404, detail="Binding not found")
    try:
        presence = await manager.binding_removed(binding)
    except Exception as exc:
        raise _http_error(exc) from exc
    if not manager.state.remove_binding(binding_id):
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"removed": True, "topic_presence": presence}


@router.get("/dispatches")
async def dispatches() -> dict[str, Any]:
    return _get_manager().pending()


@router.post("/dispatches/{dispatch_id}/claim")
async def claim_dispatch(dispatch_id: str) -> dict[str, Any]:
    try:
        return await _get_manager().claim_dispatch(dispatch_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/dispatches/{dispatch_id}/complete")
async def complete_dispatch(dispatch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = payload.get("claim_token")
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=422, detail="claim_token is required")
    try:
        await _get_manager().complete_dispatch(dispatch_id, token)
    except Exception as exc:
        raise _http_error(exc) from exc
    return {"accepted": True}


@router.post("/dispatches/{dispatch_id}/uncertain")
async def uncertain_dispatch(dispatch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = payload.get("claim_token")
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=422, detail="claim_token is required")
    try:
        _get_manager().fail_dispatch(dispatch_id, token)
    except Exception as exc:
        raise _http_error(exc) from exc
    return {"accepted": True, "status": "uncertain"}


async def _shutdown() -> None:
    global _dispatcher
    if _dispatcher is not None:
        await _dispatcher.close()
        _dispatcher = None
    if _manager is not None:
        await _manager.stop(persist=False)
