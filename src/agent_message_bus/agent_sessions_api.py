"""Versioned environment-bound connections; bearers never become family actors."""
from typing import Literal
import hashlib
from importlib.resources import files
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response


AGENT_SESSION_PREFIX = "/api/agent-sessions/v1"
OWNER_CONNECTION_PREFIX = "/api/hub/v1/agent-connections"
CLIENT_MANIFEST_PATH = AGENT_SESSION_PREFIX + "/client/manifest"
CLIENT_HELPER_PATH = AGENT_SESSION_PREFIX + "/client/agent_session_client.py"
CLIENT_DOWNLOAD_PATHS = frozenset({CLIENT_MANIFEST_PATH, CLIENT_HELPER_PATH})
CLIENT_HELPER_VERSION = "0.7"


def _client_bytes():
    # Fixed installed package file only: no caller-controlled path or source execution.
    try:
        with files("agent_message_bus").joinpath("agent_session_client.py").open("rb") as source:
            body = source.read(65537)
        if not body or len(body) > 65536:
            raise ValueError()
        return body
    except (OSError, ValueError):
        raise HTTPException(503, detail={"code": "agent_client_unavailable"},
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}) from None


def _client_response(request, body, media_type, *, attachment=False):
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "Content-Length": str(len(body))}
    if attachment:
        headers["Content-Disposition"] = 'attachment; filename="agent_session_client.py"'
    return Response(b"" if request.method == "HEAD" else body, media_type=media_type, headers=headers)


def bearer(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise HTTPException(401, detail={"code": "agent_session_required"})
    scheme, separator, secret = values[0].partition(" ")
    if scheme.lower() != "bearer" or not separator or not secret or any(ch.isspace() for ch in secret):
        raise HTTPException(401, detail={"code": "agent_session_required"})
    return secret


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Enrollment(Closed):
    family: Literal["codex", "claude", "hermes"]
    name: str = Field(min_length=1, max_length=100)
    host_label: str = Field(min_length=1, max_length=100)
    credential_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Exchange(Closed):
    label: str = Field(default="", max_length=100)


class Revision(Closed):
    expected_revision: int = Field(ge=1)


class Approval(Revision):
    lifetime_seconds: int = Field(default=86400, ge=60, le=604800)


class Rename(Revision):
    name: str = Field(min_length=1, max_length=100)


class MessageSend(Closed):
    recipient: str = Field(min_length=36, max_length=36)
    body: str = Field(min_length=1, max_length=8192)
    client_request_id: str = Field(min_length=1, max_length=128)


class Claim(Closed):
    lease_seconds: int = Field(default=30, ge=1, le=120)


class ClaimFinish(Closed):
    claim_handle: str = Field(min_length=20, max_length=128)
    generation: int = Field(ge=1)


class ClaimRenew(ClaimFinish):
    lease_seconds: int = Field(default=30, ge=1, le=120)


def session_router(repository):
    router = APIRouter(prefix=AGENT_SESSION_PREFIX)

    @router.api_route("/client/manifest", methods=["GET", "HEAD"])
    def client_manifest(request: Request):
        body = _client_bytes()
        manifest = {"schema_version": 1, "helper_version": CLIENT_HELPER_VERSION,
                    "url": CLIENT_HELPER_PATH, "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest()}
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return _client_response(request, encoded, "application/json")

    @router.api_route("/client/agent_session_client.py", methods=["GET", "HEAD"])
    def client_helper(request: Request):
        return _client_response(request, _client_bytes(), "application/octet-stream", attachment=True)

    @router.post("/enrollments")
    def enroll(data: Enrollment):
        return repository.request_enrollment(**data.model_dump())

    @router.post("/exchange")
    def exchange(data: Exchange, request: Request):
        return repository.exchange(bearer(request), data.label)

    @router.get("/identity")
    def identity(request: Request):
        principal = repository.authenticate_session(bearer(request))
        return {"installation_id": principal.installation_id, "session_id": principal.session_id,
                "family": principal.family, "expires_at": principal.expires_at}

    @router.get("/peers")
    def peers(request: Request):
        return repository.peers(bearer(request))

    @router.get("/messages")
    def messages(request: Request, after: int = 0, limit: int = 50):
        return repository.messages(bearer(request), after=after, limit=limit)

    @router.post("/messages")
    def send(data: MessageSend, request: Request):
        return repository.send(bearer(request), **data.model_dump())

    @router.get("/messages/{message_id}")
    def get(message_id: str, request: Request):
        return repository.get(bearer(request), message_id)

    @router.post("/messages/{message_id}/claim")
    def claim(message_id: str, data: Claim, request: Request):
        return repository.claim(bearer(request), message_id, **data.model_dump())

    @router.post("/messages/{message_id}/renew")
    def renew(message_id: str, data: ClaimRenew, request: Request):
        return repository.renew(bearer(request), message_id, **data.model_dump())

    @router.post("/messages/{message_id}/acknowledge")
    def acknowledge(message_id: str, data: ClaimFinish, request: Request):
        return repository.acknowledge(bearer(request), message_id, **data.model_dump())

    return router


def attach_owner_routes(app, repository, human, *, environment="staging"):
    if type(environment) is not str or environment not in {"staging", "production"}:
        raise ValueError("invalid_agent_connections_environment")
    router = APIRouter(prefix=OWNER_CONNECTION_PREFIX)

    def owner(request, mutation=False):
        if request.headers.getlist('authorization'):
            raise HTTPException(403, detail={"code": "human_session_required"})
        actor = human(request, mutation=mutation)
        if actor != repository.owner:
            raise HTTPException(403, detail={"code": "agent_connections_owner_required"})
        return actor

    @router.get("/capabilities")
    def capabilities(request: Request):
        owner(request)
        return {"enabled": True, "environment": environment, "owner": True}

    @router.get("/installations")
    def installations(request: Request):
        return repository.list_installations(owner(request))

    @router.post("/installations/{installation_id}/approve")
    def approve(installation_id: str, data: Approval, request: Request):
        return repository.approve(owner(request, True), installation_id, **data.model_dump())

    @router.patch("/installations/{installation_id}")
    def rename(installation_id: str, data: Rename, request: Request):
        return repository.rename(owner(request, True), installation_id, **data.model_dump())

    @router.post("/installations/{installation_id}/revoke")
    def revoke_installation(installation_id: str, data: Revision, request: Request):
        return repository.revoke_installation(owner(request, True), installation_id, **data.model_dump())

    @router.get("/installations/{installation_id}/sessions")
    def sessions(installation_id: str, request: Request):
        return repository.list_sessions(owner(request), installation_id)

    @router.post("/sessions/{session_id}/revoke")
    def revoke_session(session_id: str, data: Revision, request: Request):
        return repository.revoke_session(owner(request, True), session_id, **data.model_dump())

    @router.get("/audit")
    def audit(request: Request):
        return repository.audit(owner(request))

    app.include_router(router)
