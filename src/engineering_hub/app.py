from __future__ import annotations

import os
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.auth import AuthSettings
from .auth import AuthConfig, SessionStore, build_auth_router, require_human
from .build_info import read_build_commit
from .repository import HubError, HubRepository
from .mcp import HubMCPAuthMiddleware, build_hub_mcp
from .reachability import PublicReachability, project_topic_snapshot
from .events import NativeHubEventFeed
from .sse_keepalive import with_keepalive
from .autonomy import StagingRuntime
from .container_pilot import ContainerPilotWorkflow
from .registry_bridge import CanonicalRegistryBridge
from .registry_client import (
    RegistryAccessDenied, RegistrySourceUnavailable, RegistryPointer,
    RegistrySnapshotRequired, validate_topic as validate_registry_topic,
)
from .dto import (
    AgentListDTO, AutonomyDTO, DecisionDTO, DecisionListDTO, EventStatusDTO,
    HealthDTO, OverviewDTO, PauseDTO, ProjectListDTO, RunDTO, RunListDTO, RecoveryDTO,
    TaskDTO, TaskListDTO,
)

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
            human_auth_mode=os.environ.get("HUB_HUMAN_AUTH_MODE", "github"),
            cloudflare_issuer=os.environ.get("HUB_CF_ACCESS_ISSUER", ""),
            cloudflare_audience=os.environ.get("HUB_CF_ACCESS_AUDIENCE", ""),
            cloudflare_allowed_emails=tuple(email.strip().lower() for email in
                os.environ.get("HUB_CF_ACCESS_ALLOWED_EMAILS", "").split(",") if email.strip()),
            alternate_origins=tuple(value.strip() for value in
                os.environ.get("HUB_ALTERNATE_ORIGINS", "").split(",") if value.strip()),
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


class TopicSnapshot(Input):
    topic: str
    items: list[PublicReachability]
    advisory: Literal[True]
    delivery_guaranteed: Literal[False]
    snapshot_revision: str | None
    registry_epoch: str | None = None
    event_source: Literal['hub_only', 'canonical_registry', 'none'] = 'hub_only'


class TopicPreflight(Input):
    topic: str
    recipient: Literal['codex', 'hermes', 'claude']
    peer: PublicReachability
    advisory: Literal[True]
    delivery_guaranteed: Literal[False]


def create_app(settings: Settings | None = None, *, repository=None, github_client=None,
               fixture_workflow=None, reachability_provider=None, event_feed=None,
               staging_dispatcher=None, container_pilot_workflow=None, registry_bridge=None,
               owned_pilot_service=None):
    settings = settings or Settings.from_env()
    repo = repository or HubRepository(settings.database)
    if registry_bridge is not None and (type(registry_bridge) is not CanonicalRegistryBridge
                                        or reachability_provider is not None):
        raise ValueError('one_reachability_source_required')
    if fixture_workflow is not None and fixture_workflow.repo is not repo:
        raise ValueError("fixture_repository_mismatch")
    if container_pilot_workflow is not None and (
            type(container_pilot_workflow) is not ContainerPilotWorkflow
            or container_pilot_workflow.repo is not repo):
        raise ValueError('container_pilot_repository_mismatch')
    if fixture_workflow is not None and container_pilot_workflow is not None:
        raise ValueError('one_staging_workflow_required')
    configured_workflow = container_pilot_workflow or fixture_workflow
    if owned_pilot_service is not None:
        from .pilot_service import OwnedPilotService
        if (type(owned_pilot_service) is not OwnedPilotService
                or owned_pilot_service.repo is not repo
                or owned_pilot_service.workflow is not container_pilot_workflow
                or owned_pilot_service.dispatcher is not staging_dispatcher):
            raise ValueError('owned_pilot_service_mismatch')
    sessions = SessionStore(settings.database, repository=repo)
    feed = event_feed if event_feed is not None else NativeHubEventFeed(settings.database)
    if staging_dispatcher is not None and (
            staging_dispatcher.repo is not repo or staging_dispatcher.workflow is not configured_workflow):
        raise ValueError('dispatcher_workflow_mismatch')
    runtime = StagingRuntime(staging_dispatcher, feed) if staging_dispatcher is not None else None

    def autonomy_for():
        if runtime is None:
            return {'configured': False, 'state': 'disabled', 'running': False, 'receipts': []}
        receipts = [{key: value for key, value in item.items() if key != 'owner'}
                    for item in staging_dispatcher.receipts()]
        result = {**runtime.status, 'receipts': receipts}
        if owned_pilot_service is not None:
            result['pilot_lifecycle'] = owned_pilot_service.status
        return result

    async def peers_for(topic, *, principal=None, bearer=None):
        if not isinstance(topic, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', topic):
            raise HubError('invalid_topic', 422)
        if registry_bridge is not None:
            try:
                validate_registry_topic(topic)
            except ValueError:
                raise HubError('invalid_topic', 422) from None
            try:
                return await registry_bridge.snapshot(principal, topic, agent_bearer=bearer)
            except RegistryAccessDenied:
                raise HubError('topic_read_forbidden', 403) from None
            except Exception:
                raise HubError('reachability_source_unavailable', 503) from None
        if reachability_provider is not None:
            try:
                # An internal authenticated registry adapter supplies evidence,
                # never public ready flags or a second writable binding store.
                evidence = reachability_provider(topic)
                evidence = dict(evidence)
                observer = evidence.get('observer')
                if observer is not None:
                    evidence['observer'] = observer.model_copy(
                        update={'now': datetime.now(timezone.utc)})
                return project_topic_snapshot(topic, **evidence)
            except Exception:
                pass  # Unavailable/malformed source is unknown, not unlinked.
        return project_topic_snapshot(topic)

    async def preflight_for(topic, recipient, *, principal=None, bearer=None):
        if recipient not in {'codex', 'hermes', 'claude'}:
            raise HubError('invalid_recipient', 422)
        snapshot = await peers_for(topic, principal=principal, bearer=bearer)
        peer = next(item for item in snapshot['items'] if item['agent'] == recipient)
        return {'topic': topic, 'recipient': recipient, 'peer': peer,
                'advisory': True, 'delivery_guaranteed': False}

    hub_mcp = build_hub_mcp(repo, settings.agent_auth, topic_peers=peers_for,
                          topic_preflight=preflight_for, autonomy_status=autonomy_for)
    origin = urlsplit(settings.auth_config.origin)
    hub_mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[origin.netloc], allowed_origins=[settings.auth_config.origin],
    )
    mcp_app = hub_mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        try:
            await feed.start()
            if owned_pilot_service is not None:
                await owned_pilot_service.start(feed)
            async with hub_mcp.session_manager.run():
                if runtime is not None:
                    await runtime.start()
                yield
        finally:
            if owned_pilot_service is not None:
                await owned_pilot_service.stop(runtime, feed)
            else:
                try:
                    if runtime is not None:
                        await runtime.stop()
                finally:
                    await feed.stop()

    app = FastAPI(title="Engineering Hub Control", version="0.1.0", docs_url=None,
                  redoc_url=None, lifespan=lifespan)
    app.state.repository, app.state.sessions, app.state.settings = repo, sessions, settings
    app.state.event_feed = feed
    app.state.staging_runtime = runtime
    app.state.owned_pilot_service = owned_pilot_service
    if settings.auth_config.human_auth_mode == "cloudflare_access":
        from .cloudflare_access import install_cloudflare_access
        install_cloudflare_access(app, settings.auth_config.cloudflare_config)
    app.include_router(build_auth_router(settings.auth_config, sessions, github_client))
    app.mount("/mcp", HubMCPAuthMiddleware(mcp_app, settings.agent_auth))

    @app.middleware("http")
    async def security_headers(request, call_next):
        alternate_hosts = {urlsplit(value).netloc for value in settings.auth_config.alternate_origins}
        if request.headers.get("host", "").lower() in alternate_hosts:
            # One browser origin, one cookie scope and one CSRF origin. Never
            # redirect authenticated API/MCP requests or forward query secrets.
            if request.method in {"GET", "HEAD"} and not request.url.path.startswith(("/api/", "/mcp")):
                response = RedirectResponse(settings.auth_config.origin + "/", status_code=303)
            else:
                response = JSONResponse({"detail": {"code": "canonical_origin_required"}}, status_code=421)
        else:
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

    def original_bearer(request):
        scheme, _, bearer = request.headers.get('authorization', '').partition(' ')
        return bearer if scheme.lower() == 'bearer' else None

    @app.get('/api/hub/v1/autonomy', response_model=AutonomyDTO, response_model_exclude_unset=True)
    def autonomy_status(request: Request):
        actor(request)
        return autonomy_for()

    @app.get('/api/hub/v1/topics/{topic}/peers', response_model=TopicSnapshot)
    async def topic_peers(topic: str, request: Request):
        principal = actor(request)
        snapshot = await peers_for(topic, principal=principal, bearer=original_bearer(request))
        return TopicSnapshot.model_validate_json(json.dumps(snapshot))

    @app.get('/api/hub/v1/topics/{topic}/preflight', response_model=TopicPreflight)
    async def topic_preflight(topic: str, request: Request,
                        recipient: Literal['codex', 'hermes', 'claude']):
        principal = actor(request)
        result = await preflight_for(topic, recipient, principal=principal, bearer=original_bearer(request))
        return TopicPreflight.model_validate_json(json.dumps(result))

    @app.get('/api/hub/v1/topics/{topic}/events', response_class=StreamingResponse,
             responses={200: {'content': {'text/event-stream': {'schema': {'type': 'string'}}}}})
    async def topic_events(topic: str, request: Request, after: int | None = None, epoch: str | None = None):
        principal = actor(request)
        if registry_bridge is None or registry_bridge.stream is None:
            raise HubError('reachability_source_unavailable', 503)
        if request.headers.get('last-event-id'):
            raise HubError('registry_cursor_query_required', 422)
        if (after is None) != (epoch is None) or type(after) not in {int, type(None)} or (after is not None and not 0 <= after <= 2**63 - 1):
            raise HubError('invalid_registry_cursor', 422)
        if epoch is not None:
            from uuid import UUID
            try:
                if str(UUID(epoch)) != epoch:
                    raise ValueError
            except ValueError:
                raise HubError('invalid_registry_cursor', 422) from None

        # Reject malformed local cursors before any upstream I/O. This explicit
        # connection-time ACL read is not a watcher or freshness cache; the
        # upstream subscribe still registers before its own catch-up/head.
        await peers_for(topic, principal=principal, bearer=original_bearer(request))

        def check_access():
            if actor(request) != principal:
                raise HTTPException(401)

        async def stream():
            source = registry_bridge.events(principal, topic, agent_bearer=original_bearer(request),
                                            after=after, epoch=epoch, check_access=check_access)
            pulses = with_keepalive(source)
            try:
                async for event in pulses:
                    try:
                        if actor(request) != principal:
                            return
                    except HTTPException:
                        return
                    if event is None:
                        yield ': keepalive\n\n'
                        continue
                    name = ('registry.invalidated' if isinstance(event, RegistryPointer) else
                            'registry.snapshot_required' if isinstance(event, RegistrySnapshotRequired) else
                            'registry.unavailable')
                    identity = f'id: {event.registry_epoch}:{topic}:{event.cursor}\n' if isinstance(event, RegistryPointer) else ''
                    yield identity + 'event: ' + name + '\ndata: ' + event.model_dump_json() + '\n\n'
            except Exception:
                yield 'event: registry.unavailable\ndata: ' + json.dumps(
                    {'topic': topic, 'registry_epoch': None, 'reason': 'observer_unavailable'}) + '\n\n'
            finally:
                await pulses.aclose()

        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @app.get('/api/hub/v1/events/status', response_model=EventStatusDTO)
    def event_status(request: Request):
        actor(request)
        return feed.status

    @app.get('/api/hub/v1/events', response_class=StreamingResponse,
             responses={200: {'content': {'text/event-stream': {'schema': {'type': 'string'}}}}})
    async def events(request: Request):
        actor(request)
        if not feed.available:
            raise HTTPException(503, detail={'code': 'native_events_unavailable'})

        async def stream():
            source = feed.broker.events()
            pulses = with_keepalive(source)
            try:
                async for pointer in pulses:
                    try:
                        actor(request)  # Recheck revocation/expiry on each event.
                    except HTTPException:
                        return
                    if pointer is None:
                        yield ': keepalive\n\n'
                        continue
                    yield 'data: ' + json.dumps(pointer, separators=(',', ':')) + '\n\n'
            finally:
                await pulses.aclose()

        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    def projects():
        return [{"id": key, "name": name, "environment": "staging",
                 "executor_enabled": False, "health": "unknown",
                 "reason": "inventory_and_acceptance_required"} for key, name in PROJECTS]

    def agents():
        return [{"id": name, "transport": "unknown", "chat_readiness": "unverified",
                 "reason": "live_presence_adapter_not_configured"}
                for name in ("hermes", "codex", "claude")]

    @app.get("/health", response_model=HealthDTO)
    def health():
        # Control service liveness is not host, agent or deployment readiness.
        if owned_pilot_service is not None:
            owned_pilot_service.assert_active()
        repo.get_paused()
        return {"ok": True, "service": "engineering-hub-control",
                "build_commit": read_build_commit()}

    @app.get("/api/hub/v1/overview", response_model=OverviewDTO, response_model_exclude_unset=True)
    def overview(request: Request):
        actor(request)
        return {"paused": repo.get_paused(), "projects": projects(),
                "tasks": repo.list_tasks(), "decisions": repo.list_decisions(),
                "runs": repo.list_runs(), "agents": agents(), "autonomy": autonomy_for(),
                "capabilities": {"production": False, "host_dispatch": False,
                                 "github_merge": False, "processing": False,
                                 "fixture_dispatch": fixture_workflow is not None,
                                 "container_pilot_dispatch": container_pilot_workflow is not None,
                                 "merge_gate_strength": "not_enforced"}}

    @app.get("/api/hub/v1/projects", response_model=ProjectListDTO)
    def list_projects(request: Request):
        actor(request)
        return {"items": projects()}

    @app.get("/api/hub/v1/agents", response_model=AgentListDTO)
    def list_agents(request: Request):
        actor(request)
        return {"items": agents()}

    @app.get("/api/hub/v1/tasks", response_model=TaskListDTO)
    def list_tasks(request: Request):
        actor(request)
        return {"items": repo.list_tasks()}

    @app.post("/api/hub/v1/tasks", status_code=201, response_model=TaskDTO)
    def create_task(payload: NewTask, request: Request):
        return repo.create_task(human(request, mutation=True), payload.project,
                                payload.title, payload.client_request_id)

    @app.get("/api/hub/v1/tasks/{task_id}", response_model=TaskDTO)
    def get_task(task_id: str, request: Request):
        actor(request)
        return repo.get_task(task_id)

    @app.post("/api/hub/v1/tasks/{task_id}/claim", response_model=TaskDTO)
    def claim(task_id: str, payload: Revision, request: Request):
        return repo.claim_task(agent(request), task_id, payload.expected_revision)

    @app.post("/api/hub/v1/tasks/{task_id}/candidate", response_model=TaskDTO)
    def candidate(task_id: str, payload: Candidate, request: Request):
        return repo.set_candidate(agent(request), task_id, payload.expected_revision, payload.commit)

    @app.post("/api/hub/v1/tasks/{task_id}/review", response_model=TaskDTO)
    def review(task_id: str, payload: Review, request: Request):
        return repo.review_task(agent(request), task_id, payload.expected_revision,
                                payload.commit, payload.verdict)

    @app.get("/api/hub/v1/decisions", response_model=DecisionListDTO, response_model_exclude_unset=True)
    def list_decisions(request: Request):
        actor(request)
        return {"items": repo.list_decisions()}

    @app.post("/api/hub/v1/decisions", status_code=201, response_model=DecisionDTO, response_model_exclude_unset=True)
    def create_decision(payload: NewDecision, request: Request):
        return repo.create_decision(actor(request, mutation=True), payload.project,
                                    payload.title, payload.options, payload.protected)

    @app.post("/api/hub/v1/decisions/{decision_id}/answer", response_model=DecisionDTO, response_model_exclude_unset=True)
    def answer(decision_id: str, payload: Answer, request: Request):
        return repo.answer_decision(human(request, mutation=True), decision_id,
                                    payload.expected_revision, payload.answer)

    @app.post("/api/hub/v1/pause", response_model=PauseDTO)
    def pause(payload: Pause, request: Request):
        return repo.set_pause(human(request, mutation=True), payload.paused)

    @app.get("/api/hub/v1/runs", response_model=RunListDTO)
    def list_runs(request: Request):
        actor(request)
        return {"items": repo.list_runs()}

    @app.post("/api/hub/v1/runs", status_code=201, response_model=RunDTO)
    def create_run(payload: NewRun, request: Request):
        identity = agent(request)
        # Remains fail-closed until operator-owned manifests and an actual
        # executor integration pass acceptance. Not a pretend deploy button.
        if fixture_workflow is None:
            raise HTTPException(423, detail={"code": "executor_not_enabled"})
        return fixture_workflow.create_run(identity, payload.task_id, payload.expected_revision,
                                           payload.artifact_digest, payload.target)

    @app.post("/api/hub/v1/runs/{run_id}/dispatch", response_model=RunDTO)
    async def dispatch(run_id: str, request: Request):
        identity = agent(request)
        if fixture_workflow is None:
            raise HTTPException(423, detail={"code": "executor_not_enabled"})
        if repo.get_run(run_id)['created_by'] != identity:
            raise HTTPException(403, detail={"code": "forbidden"})
        return await fixture_workflow.advance(run_id)

    @app.post("/api/hub/v1/container-pilot/runs", status_code=201, response_model=RunDTO)
    def create_container_pilot_run(payload: NewRun, request: Request):
        identity = agent(request)
        if container_pilot_workflow is None:
            raise HTTPException(423, detail={'code': 'container_pilot_not_enabled'})
        return container_pilot_workflow.create_run(identity, payload.task_id, payload.expected_revision,
                                                   payload.artifact_digest, payload.target)

    @app.post("/api/hub/v1/container-pilot/runs/{run_id}/dispatch", response_model=RunDTO)
    async def dispatch_container_pilot(run_id: str, request: Request):
        identity = agent(request)
        if container_pilot_workflow is None:
            raise HTTPException(423, detail={'code': 'container_pilot_not_enabled'})
        if repo.get_run(run_id)['created_by'] != identity:
            raise HTTPException(403, detail={'code': 'forbidden'})
        if owned_pilot_service is not None:
            return await owned_pilot_service.advance(run_id)
        return await container_pilot_workflow.advance(run_id)

    @app.post('/api/hub/v1/container-pilot/runs/{run_id}/recover', response_model=RecoveryDTO)
    async def recover_container_pilot(run_id: str, request: Request):
        identity = agent(request)
        if owned_pilot_service is None:
            raise HTTPException(423, detail={'code': 'owned_pilot_not_enabled'})
        if repo.get_run(run_id)['created_by'] != identity:
            raise HTTPException(403, detail={'code': 'forbidden'})
        return await owned_pilot_service.recover(run_id)

    if settings.ui_directory and settings.ui_directory.is_dir():
        app.mount("/", StaticFiles(directory=settings.ui_directory, html=True), name="dashboard")
    return app
