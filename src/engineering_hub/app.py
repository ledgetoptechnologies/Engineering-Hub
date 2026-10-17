from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.auth import AuthSettings
from .auth import AuthConfig, SessionStore, build_auth_router, require_human
from .repository import HubError, HubRepository
from .mcp import HubMCPAuthMiddleware, build_hub_mcp

PROJECTS = (
    ("engineering-hub", "Engineering Hub"), ("3d-viewer", "3D Viewer"),
    ("project-alpha", "Project Alpha"), ("ledge-top-ops", "LedgeTop Ops"),
    ("treedar", "TreeDar"),
)


@dataclass(frozen=True)
class Settings:
    database: Path
    agent_auth: AuthSettings
    auth_config: AuthConfig
    ui_directory: Path | None = None

    @classmethod
    def from_env(cls):
        path = Path(os.environ.get("HUB_DB", "data/hub.sqlite3"))
        bus_path = os.environ.get("AGENT_BUS_DB")
        if bus_path and path.resolve() == Path(bus_path).resolve():
            raise ValueError("Hub requires a separate database")
        numeric = os.environ.get("HUB_BEAU_GITHUB_ID")
        return cls(path, AuthSettings.from_env(), AuthConfig(
            base_url=os.environ.get("HUB_BASE_URL", "https://hub.invalid"),
            github_client_id=os.environ.get("HUB_GITHUB_CLIENT_ID", ""),
            github_client_secret=os.environ.get("HUB_GITHUB_CLIENT_SECRET", ""),
            beau_github_id=int(numeric) if numeric else None,
        ), Path(os.environ["HUB_UI_DIRECTORY"]) if os.environ.get("HUB_UI_DIRECTORY") else None)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewTask(Input):
    project: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    client_request_id: str = Field(min_length=1, max_length=128)


class Revision(Input):
    expected_revision: int = Field(ge=1)


class Candidate(Revision):
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class Review(Candidate):
    verdict: Literal["approve", "reject"]


class NewDecision(Input):
    project: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    options: list[str] = Field(min_length=2, max_length=5)
    protected: bool = False


class Answer(Revision):
    answer: str = Field(min_length=1, max_length=200)


class Pause(Input):
    paused: bool


class NewRun(Revision):
    task_id: str = Field(min_length=1, max_length=36)
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    target: str = Field(min_length=1, max_length=128)


def create_app(settings: Settings | None = None, *, repository=None, github_client=None,
               fixture_workflow=None):
    settings = settings or Settings.from_env()
    repo = repository or HubRepository(settings.database)
    if fixture_workflow is not None and fixture_workflow.repo is not repo:
        raise ValueError("fixture_repository_mismatch")
    sessions = SessionStore(settings.database)
    hub_mcp = build_hub_mcp(repo, settings.agent_auth)
    origin = urlsplit(settings.auth_config.origin)
    hub_mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[origin.netloc], allowed_origins=[settings.auth_config.origin],
    )
    mcp_app = hub_mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with hub_mcp.session_manager.run():
            yield

    app = FastAPI(title="Engineering Hub Control", version="0.1.0", docs_url=None,
                  redoc_url=None, lifespan=lifespan)
    app.state.repository, app.state.sessions, app.state.settings = repo, sessions, settings
    app.include_router(build_auth_router(settings.auth_config, sessions, github_client))
    app.mount("/mcp", HubMCPAuthMiddleware(mcp_app, settings.agent_auth))

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.exception_handler(HubError)
    async def domain_error(request, error):
        return JSONResponse({"detail": {"code": error.code}}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"detail": {"code": "invalid_input"}}, status_code=422)

    def human(request: Request, *, mutation=False):
        if request.headers.get("authorization"):
            raise HTTPException(403, detail={"code": "human_session_required"})
        return require_human(request, settings.auth_config, sessions, mutation=mutation)

    def actor(request: Request, *, mutation=False):
        # A supplied bearer is always validated as an agent; never falls back to
        # a human cookie if malformed. Actor/reviewer identity is never input.
        if request.headers.get("authorization"):
            return settings.agent_auth.agent(request)
        return human(request, mutation=mutation)

    def agent(request: Request):
        return settings.agent_auth.agent(request)

    def projects():
        return [{"id": key, "name": name, "environment": "staging",
                 "executor_enabled": False, "health": "unknown",
                 "reason": "inventory_and_acceptance_required"} for key, name in PROJECTS]

    def agents():
        return [{"id": name, "transport": "unknown", "chat_readiness": "unverified",
                 "reason": "live_presence_adapter_not_configured"}
                for name in ("hermes", "codex", "claude")]

    @app.get("/health")
    def health():
        # Control service liveness is not host, agent or deployment readiness.
        repo.get_paused()
        return {"ok": True, "service": "engineering-hub-control"}

    @app.get("/api/hub/v1/overview")
    def overview(request: Request):
        actor(request)
        return {"paused": repo.get_paused(), "projects": projects(),
                "tasks": repo.list_tasks(), "decisions": repo.list_decisions(),
                "runs": repo.list_runs(), "agents": agents(),
                "capabilities": {"production": False, "host_dispatch": False,
                                 "github_merge": False, "processing": False,
                                 "fixture_dispatch": fixture_workflow is not None,
                                 "merge_gate_strength": "not_enforced"}}

    @app.get("/api/hub/v1/projects")
    def list_projects(request: Request):
        actor(request)
        return {"items": projects()}

    @app.get("/api/hub/v1/agents")
    def list_agents(request: Request):
        actor(request)
        return {"items": agents()}

    @app.get("/api/hub/v1/tasks")
    def list_tasks(request: Request):
        actor(request)
        return {"items": repo.list_tasks()}

    @app.post("/api/hub/v1/tasks", status_code=201)
    def create_task(payload: NewTask, request: Request):
        return repo.create_task(human(request, mutation=True), payload.project,
                                payload.title, payload.client_request_id)

    @app.get("/api/hub/v1/tasks/{task_id}")
    def get_task(task_id: str, request: Request):
        actor(request)
        return repo.get_task(task_id)

    @app.post("/api/hub/v1/tasks/{task_id}/claim")
    def claim(task_id: str, payload: Revision, request: Request):
        return repo.claim_task(agent(request), task_id, payload.expected_revision)

    @app.post("/api/hub/v1/tasks/{task_id}/candidate")
    def candidate(task_id: str, payload: Candidate, request: Request):
        return repo.set_candidate(agent(request), task_id, payload.expected_revision, payload.commit)

    @app.post("/api/hub/v1/tasks/{task_id}/review")
    def review(task_id: str, payload: Review, request: Request):
        return repo.review_task(agent(request), task_id, payload.expected_revision,
                                payload.commit, payload.verdict)

    @app.get("/api/hub/v1/decisions")
    def list_decisions(request: Request):
        actor(request)
        return {"items": repo.list_decisions()}

    @app.post("/api/hub/v1/decisions", status_code=201)
    def create_decision(payload: NewDecision, request: Request):
        return repo.create_decision(actor(request, mutation=True), payload.project,
                                    payload.title, payload.options, payload.protected)

    @app.post("/api/hub/v1/decisions/{decision_id}/answer")
    def answer(decision_id: str, payload: Answer, request: Request):
        return repo.answer_decision(human(request, mutation=True), decision_id,
                                    payload.expected_revision, payload.answer)

    @app.post("/api/hub/v1/pause")
    def pause(payload: Pause, request: Request):
        return repo.set_pause(human(request, mutation=True), payload.paused)

    @app.get("/api/hub/v1/runs")
    def list_runs(request: Request):
        actor(request)
        return {"items": repo.list_runs()}

    @app.post("/api/hub/v1/runs", status_code=201)
    def create_run(payload: NewRun, request: Request):
        identity = agent(request)
        # Remains fail-closed until operator-owned manifests and an actual
        # executor integration pass acceptance. Not a pretend deploy button.
        if fixture_workflow is None:
            raise HTTPException(423, detail={"code": "executor_not_enabled"})
        return fixture_workflow.create_run(identity, payload.task_id, payload.expected_revision,
                                           payload.artifact_digest, payload.target)

    @app.post("/api/hub/v1/runs/{run_id}/dispatch")
    async def dispatch(run_id: str, request: Request):
        identity = agent(request)
        if fixture_workflow is None:
            raise HTTPException(423, detail={"code": "executor_not_enabled"})
        if repo.get_run(run_id)['created_by'] != identity:
            raise HTTPException(403, detail={"code": "forbidden"})
        return await fixture_workflow.advance(run_id)

    if settings.ui_directory and settings.ui_directory.is_dir():
        app.mount("/", StaticFiles(directory=settings.ui_directory, html=True), name="dashboard")
    return app
