from __future__ import annotations

import os
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
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
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette

from agent_message_bus.auth import AuthSettings
from .auth import AuthConfig, SessionStore, build_auth_router, require_human
from .build_info import read_build_commit
from .image_version_reports import image_versions_for
from .image_version_reports import ImageVersionsSummary
from .repository import HubError, HubRepository
from .mcp import HubMCPAuthMiddleware, build_hub_mcp
from .reachability import PublicReachability, project_topic_snapshot
from .delivery_guidance import DeliveryGuidance, topic_delivery_guidance
from .events import NativeHubEventFeed
from .sse_keepalive import with_keepalive
from .autonomy import StagingRuntime
from .container_pilot import ContainerPilotWorkflow
from .registry_bridge import CanonicalRegistryBridge
from .registry_client import (
    RegistryAccessDenied, RegistrySourceUnavailable, RegistryPointer,
    RegistrySnapshotRequired, validate_topic as validate_registry_topic,
    validate_origin as validate_registry_origin,
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
    ingress_mode: str = "combined"
    agent_allowed_hosts: tuple[str, ...] = ()
    work_queue_enabled: bool = False
    staging_machine_id: str = "staging-pc"
    registry_enabled: bool = False
    registry_origin: str | None = None
    registry_events_enabled: bool = False

    def __post_init__(self):
        if type(self.work_queue_enabled) is not bool:
            raise ValueError("Explicit work queue enable flag required")
        if type(self.registry_enabled) is not bool or type(self.registry_events_enabled) is not bool:
            raise ValueError('Explicit registry flags required')
        if self.registry_origin is not None:
            validate_registry_origin(self.registry_origin)
        if self.registry_enabled and self.registry_origin is None:
            raise ValueError('Configured registry origin required')
        if self.registry_events_enabled and not self.registry_enabled:
            raise ValueError('Registry events require explicit registry enablement')
        from .ingress import validate_agent_hosts
        validate_agent_hosts(self.agent_allowed_hosts)
        if self.ingress_mode not in {"combined", "agents", "browser"}:
            raise ValueError("Unknown Hub ingress mode")
        if self.ingress_mode == "browser" and (
                self.auth_config.human_auth_mode != "cloudflare_access"
                or self.auth_config.cloudflare_config is None):
            raise ValueError("Browser ingress requires configured Cloudflare Access")
        # Operator templates must never become usable public bearer credentials.
        for value in (self.agent_auth.hermes_token, self.agent_auth.codex_token,
                      self.agent_auth.claude_token, self.agent_auth.admin_token):
            if value and value.startswith("REPLACE_WITH_"):
                raise ValueError("Replace agent-token placeholders before starting Hub")

    @classmethod
    def from_env(cls, deployment=None):
        work_queue_flag = os.environ.get("HUB_WORK_QUEUE_ENABLED", "0")
        if work_queue_flag not in {"0", "1"}:
            raise ValueError("HUB_WORK_QUEUE_ENABLED must be 0 or 1")
        registry_flag = os.environ.get('HUB_REGISTRY_ENABLED', '0')
        registry_events_flag = os.environ.get('HUB_REGISTRY_EVENTS_ENABLED', '0')
        if registry_flag not in {'0', '1'} or registry_events_flag not in {'0', '1'}:
            raise ValueError('Hub registry flags must be 0 or 1')
        path = (deployment.hub_database if deployment is not None else
                Path(os.environ.get("HUB_DB", "data/hub.sqlite3")))
        bus_path = (deployment.bus_paths['AGENT_BUS_DB'] if deployment is not None else
                    os.environ.get("AGENT_BUS_DB"))
        if bus_path and path.resolve() == Path(bus_path).resolve():
            raise ValueError("Hub requires a separate database")
        numeric = os.environ.get("HUB_BEAU_GITHUB_ID")
        agent_auth = AuthSettings.from_env()
        if deployment is not None:
            agent_auth = replace(agent_auth, allowed_ips=frozenset(deployment.allowed_ips))
        auth_config = deployment.auth_config if deployment is not None else AuthConfig(
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
        )
        return cls(path, agent_auth, auth_config,
                   Path(os.environ["HUB_UI_DIRECTORY"]) if os.environ.get("HUB_UI_DIRECTORY") else None,
                   ingress_mode=os.environ.get("HUB_INGRESS_MODE", "combined"),
                   agent_allowed_hosts=(deployment.allowed_hosts if deployment is not None else
                       tuple(value.strip() for value in
                             os.environ.get("HUB_AGENT_ALLOWED_HOSTS", "").split(",") if value.strip())),
                   work_queue_enabled=work_queue_flag == "1",
                   staging_machine_id=os.environ.get("HUB_STAGING_MACHINE_ID", "staging-pc"),
                   registry_enabled=registry_flag == '1',
                   registry_origin=os.environ.get('HUB_REGISTRY_ORIGIN'),
                   registry_events_enabled=registry_events_flag == '1')


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
    tracking: Literal['not_configured', 'configured', 'unavailable'] = 'unavailable'


class TopicPreflight(Input):
    topic: str
    recipient: Literal['codex', 'hermes', 'claude']
    peer: PublicReachability
    advisory: Literal[True]
    delivery_guaranteed: Literal[False]
    guidance: DeliveryGuidance


def create_app(settings: Settings | None = None, *, repository=None, github_client=None,
               fixture_workflow=None, reachability_provider=None, event_feed=None,
               staging_dispatcher=None, container_pilot_workflow=None, registry_bridge=None,
               owned_pilot_service=None, work_queue=None, archive_reader=None,
               queued_pilot_runtime=None, bus_app=None):
    settings = settings or Settings.from_env()
    bus_mcp = None
    bus_health = None
    notes_repository = None
    if bus_app is not None:
        if (not isinstance(bus_app, Starlette)
                or settings.ingress_mode == 'browser'
                or getattr(bus_app.state, 'hub_owner', None) is not None):
            raise ValueError('Fresh private bus application required')
        bus_mcp = getattr(bus_app.state, 'mcp', None)
        service = getattr(bus_app.state, 'service', None)
        if (type(bus_mcp) is not FastMCP or service is None
                or service.auth is not settings.agent_auth
                or bus_mcp.settings.stateless_http
                or Path(service.repository.path).resolve() == settings.database.resolve()
                or (settings.database.exists()
                    and settings.database.samefile(service.repository.path))):
            raise ValueError('Unified bus identity, stateful transport and separate storage required')
        bus_health = next((route.endpoint for route in bus_app.routes
                           if getattr(route, 'path', None) == '/health'), None)
        if not callable(bus_health):
            raise ValueError('Unified bus health source required')
        from agent_message_bus.knowledge import KnowledgeRepository
        notes_repository = KnowledgeRepository(service.repository.path)
    if settings.registry_enabled:
        if registry_bridge is not None or reachability_provider is not None:
            raise ValueError('Choose one explicit registry configuration source')
        from .registry_client import RegistryClient
        from .registry_stream import RegistryStream
        registry_bridge = CanonicalRegistryBridge(
            RegistryClient(settings.registry_origin),
            stream=RegistryStream(settings.registry_origin) if settings.registry_events_enabled else None,
        )
    if archive_reader is not None:
        from .archive_messages import ArchiveMessageReader
        if type(archive_reader) is not ArchiveMessageReader:
            raise ValueError('Explicit approved archive reader required')
    if settings.ingress_mode == "browser" and any(component is not None for component in (
            fixture_workflow, staging_dispatcher, container_pilot_workflow, owned_pilot_service, work_queue)):
        raise ValueError("Browser ingress must not own staging execution")
    repo = repository or HubRepository(settings.database)
    if (work_queue is not None or settings.work_queue_enabled) and any(
            component is not None for component in (fixture_workflow, staging_dispatcher,
                                                   container_pilot_workflow, owned_pilot_service)):
        raise ValueError("Work queue and legacy dispatch require a shared reservation bridge")
    if work_queue is not None:
        from .work_queue import WorkQueue
        if type(work_queue) is not WorkQueue or work_queue.repo is not repo:
            raise ValueError("Work queue repository identity required")
    elif settings.work_queue_enabled:
        if settings.ingress_mode == "browser":
            raise ValueError("Browser ingress cannot own work scheduling")
        from .work_queue_config import configured_work_queue
        work_queue = configured_work_queue(repo, machine_id=settings.staging_machine_id)
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
    feed = event_feed if event_feed is not None else NativeHubEventFeed(settings.database)
    if queued_pilot_runtime is not None:
        from .queued_pilot_runtime import QueuedPilotRuntime
        if (type(queued_pilot_runtime) is not QueuedPilotRuntime
                or queued_pilot_runtime.bridge.repo is not repo
                or queued_pilot_runtime.bridge.queue is not work_queue
                or queued_pilot_runtime.feed is not feed
                or settings.ingress_mode == 'browser'
                or any(component is not None for component in (
                    fixture_workflow, staging_dispatcher, container_pilot_workflow, owned_pilot_service))
                or queued_pilot_runtime.status['state'] != 'not_started'
                or getattr(queued_pilot_runtime, '_application', None) is not None
                or feed._source is not None):
            raise ValueError('queued_pilot_application_binding_required')
    sessions = SessionStore(settings.database, repository=repo,
                            session_ttl=settings.auth_config.local_session_ttl_seconds)
    if staging_dispatcher is not None and (
            staging_dispatcher.repo is not repo or staging_dispatcher.workflow is not configured_workflow):
        raise ValueError('dispatcher_workflow_mismatch')
    runtime = StagingRuntime(staging_dispatcher, feed) if staging_dispatcher is not None else None

    def autonomy_for():
        if queued_pilot_runtime is not None:
            status = queued_pilot_runtime.status
            return {'configured': True, 'state': status['state'],
                    'running': status['running'], 'receipts': [], 'queued_pilot': status}
        if runtime is None:
            return {'configured': False, 'state': 'disabled', 'running': False, 'receipts': []}
        receipts = [{key: value for key, value in item.items() if key != 'owner'}
                    for item in staging_dispatcher.receipts()]
        result = {**runtime.status, 'receipts': receipts}
        if owned_pilot_service is not None:
            result['pilot_lifecycle'] = owned_pilot_service.status
        return result

    async def agents_for():
        result = [{"id": name, "transport": "unknown", "chat_readiness": "unverified",
                   "reason": "live_presence_adapter_not_configured"}
                  for name in ("hermes", "codex", "claude")]
        if bus_app is not None:
            from agent_message_bus.push import PushPublisher
            from .bus_connection import project_bus_connections, unavailable_connections
            try:
                publisher = bus_app.state.push_publisher
                if type(publisher) is not PushPublisher:
                    raise ValueError('Bus publisher unavailable')
                connections = project_bus_connections(await publisher.presence_status())
            except Exception:
                connections = unavailable_connections()
            for item in result:
                item['connection'] = connections[item['id']]
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
                return {**await registry_bridge.snapshot(principal, topic, agent_bearer=bearer),
                        'tracking': 'configured'}
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
                return {**project_topic_snapshot(topic, **evidence), 'tracking': 'configured'}
            except Exception:
                return {**project_topic_snapshot(topic), 'tracking': 'unavailable'}
        return {**project_topic_snapshot(topic), 'tracking': 'not_configured'}

    async def preflight_for(topic, recipient, *, principal=None, bearer=None):
        if recipient not in {'codex', 'hermes', 'claude'}:
            raise HubError('invalid_recipient', 422)
        snapshot = await peers_for(topic, principal=principal, bearer=bearer)
        peer = next(item for item in snapshot['items'] if item['agent'] == recipient)
        return {'topic': topic, 'recipient': recipient, 'peer': peer,
                'advisory': True, 'delivery_guaranteed': False,
                'guidance': topic_delivery_guidance(
                    PublicReachability.model_validate_json(json.dumps(peer))).model_dump()}

    hub_mcp = build_hub_mcp(repo, settings.agent_auth, topic_peers=peers_for,
                          topic_preflight=preflight_for, autonomy_status=autonomy_for,
                          work_queue=work_queue, mcp=bus_mcp,
                          agent_status=agents_for if bus_app is not None else None,
                          image_versions=lambda: ImageVersionsSummary.model_validate(
                              image_versions_for(notes_repository)).model_dump(mode='json'))
    origin = urlsplit(settings.auth_config.origin)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys([origin.netloc] + (
            list(settings.agent_allowed_hosts) if settings.ingress_mode != "browser" else []))),
        allowed_origins=[settings.auth_config.origin],
    )
    if bus_app is None:
        hub_mcp.settings.transport_security = transport_security
        mcp_app = hub_mcp.streamable_http_app()
    else:
        # The bus session manager already captured its transport policy during
        # factory construction. Never pretend changing settings afterward fixes it.
        actual = hub_mcp.settings.transport_security
        if (actual is None or not actual.enable_dns_rebinding_protection
                or set(actual.allowed_hosts or ()) != set(transport_security.allowed_hosts or ())
                or set(actual.allowed_origins or ()) != set(transport_security.allowed_origins or ())):
            raise ValueError('Unified transport policy must be installed before bus construction')

    @asynccontextmanager
    async def lifespan(app):
        cleanup_started = False

        async def stop_components():
            nonlocal cleanup_started
            cleanup_started = True
            if owned_pilot_service is not None:
                await owned_pilot_service.stop(runtime, feed)
            else:
                async def drain():
                    try:
                        if queued_pilot_runtime is not None:
                            await queued_pilot_runtime.stop()
                        if runtime is not None:
                            await runtime.stop()
                    finally:
                        await feed.stop()
                if queued_pilot_runtime is not None:
                    from .owned_tasks import finish_owned
                    await finish_owned(drain())
                else:
                    await drain()

        try:
            await feed.start()
            if owned_pilot_service is not None:
                await owned_pilot_service.start(feed)
            mcp_owner = (bus_app.router.lifespan_context(bus_app) if bus_app is not None
                         else hub_mcp.session_manager.run())
            async with mcp_owner:
                try:
                    if queued_pilot_runtime is not None:
                        status = await queued_pilot_runtime.start()
                        if status['state'] != 'active':
                            raise HubError('queued_pilot_startup_unavailable', 503)
                    if runtime is not None:
                        await runtime.start()
                    yield
                finally:
                    if bus_app is not None:
                        # Retain bus identity/publisher/native source while accepted
                        # Hub work drains; never close its transport underneath it.
                        await stop_components()
        finally:
            if not cleanup_started:
                await stop_components()

    app = FastAPI(title="Engineering Hub Control", version="0.1.0", docs_url=None,
                  redoc_url=None, lifespan=lifespan)
    app.state.repository, app.state.sessions, app.state.settings = repo, sessions, settings
    app.state.mcp = hub_mcp
    app.state.embedded_bus = bus_app
    if bus_app is not None:
        bus_app.state.hub_owner = app
    app.state.event_feed = feed
    app.state.staging_runtime = runtime
    app.state.queued_pilot_runtime = queued_pilot_runtime
    if queued_pilot_runtime is not None:
        # One-use application ownership, not transfer of its owner/client.
        queued_pilot_runtime._application = app
    app.state.owned_pilot_service = owned_pilot_service
    app.state.registry_bridge = registry_bridge
    # Explicit read-only capability; caller owns its lifetime. No environment
    # discovery, archive adoption or automatic close/ownership transfer.
    app.state.archive_reader = archive_reader
    if (settings.auth_config.human_auth_mode == "cloudflare_access"
            and settings.ingress_mode == "combined"):
        from .cloudflare_access import install_cloudflare_access
        install_cloudflare_access(app, settings.auth_config.cloudflare_config)
    app.include_router(build_auth_router(settings.auth_config, sessions, github_client))
    if bus_app is None:
        app.mount("/mcp", HubMCPAuthMiddleware(mcp_app, settings.agent_auth))
    else:
        from .unified_bus import attach_bus_routes
        attach_bus_routes(app, bus_app)

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

    from .notes_api import attach_notes_routes
    attach_notes_routes(app, repository=notes_repository, actor=actor)

    if archive_reader is not None:
        from .archive_api import attach_archive_routes
        attach_archive_routes(app, reader=archive_reader, actor=actor)

    def agent(request: Request):
        return settings.agent_auth.agent(request)

    from .task_proposal_api import attach_staging_task_routes
    attach_staging_task_routes(app, repo=repo, agent=agent)

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

    @app.head("/health", include_in_schema=False)
    @app.get("/health", response_model=HealthDTO if bus_app is None else None)
    async def health(request: Request):
        # Control service liveness is not host, agent or deployment readiness.
        if owned_pilot_service is not None:
            owned_pilot_service.assert_active()
        repo.get_paused()
        result = {"ok": True, "service": "engineering-hub-control",
                  "build_commit": read_build_commit()}
        if bus_health is not None:
            try:
                response = await bus_health(request)
                summary = json.loads(response.body)
                result['bus'] = summary
                # Existing native connectors read /health.push and its cursor.
                # Retain that wire contract as well as the nested component view.
                for key in ('database', 'schema_version', 'version', 'audit', 'semantic', 'push'):
                    if key in summary:
                        result[key] = summary[key]
                push = summary.get('push', {})
                result['ok'] = (summary.get('ok') is True
                                and summary.get('database') == 'ok'
                                and summary.get('audit') == 'ok'
                                and push.get('state') == 'healthy'
                                and push.get('subscriptions_enabled') is True)
            except Exception:
                result.update(ok=False, bus={'ok': False, 'state': 'unavailable'})
        return result

    @app.get("/api/hub/v1/overview", response_model=OverviewDTO, response_model_exclude_unset=True)
    async def overview(request: Request):
        actor(request)
        return {"paused": repo.get_paused(), "projects": projects(),
                "tasks": repo.list_task_views(), "decisions": repo.list_decisions(),
                "runs": repo.list_runs(), "agents": await agents_for(), "autonomy": autonomy_for(),
                "image_versions": image_versions_for(notes_repository),
                "capabilities": {"production": False, "host_dispatch": False,
                                 "github_merge": False, "processing": False,
                                 "fixture_dispatch": fixture_workflow is not None,
                                 "container_pilot_dispatch": container_pilot_workflow is not None,
                                 "merge_gate_strength": "not_enforced"}}

    @app.get("/api/hub/v1/projects", response_model=ProjectListDTO)
    def list_projects(request: Request):
        actor(request)
        return {"items": projects()}

    @app.get("/api/hub/v1/image-versions", response_model=ImageVersionsSummary)
    def image_versions(request: Request):
        actor(request)
        return image_versions_for(notes_repository)

    @app.get("/api/hub/v1/agents", response_model=AgentListDTO, response_model_exclude_unset=True)
    async def list_agents(request: Request):
        actor(request)
        return {"items": await agents_for()}

    @app.get("/api/hub/v1/tasks", response_model=TaskListDTO, response_model_exclude_unset=True)
    def list_tasks(request: Request):
        actor(request)
        return {"items": repo.list_task_views()}

    @app.post("/api/hub/v1/tasks", status_code=201, response_model=TaskDTO, response_model_exclude_unset=True)
    def create_task(payload: NewTask, request: Request):
        return repo.create_task(human(request, mutation=True), payload.project,
                                payload.title, payload.client_request_id)

    @app.get("/api/hub/v1/tasks/{task_id}", response_model=TaskDTO, response_model_exclude_unset=True)
    def get_task(task_id: str, request: Request):
        actor(request)
        return repo.get_task(task_id)

    @app.post("/api/hub/v1/tasks/{task_id}/claim", response_model=TaskDTO, response_model_exclude_unset=True)
    def claim(task_id: str, payload: Revision, request: Request):
        return repo.claim_task(agent(request), task_id, payload.expected_revision)

    @app.post("/api/hub/v1/tasks/{task_id}/candidate", response_model=TaskDTO, response_model_exclude_unset=True)
    def candidate(task_id: str, payload: Candidate, request: Request):
        return repo.set_candidate(agent(request), task_id, payload.expected_revision, payload.commit)

    @app.post("/api/hub/v1/tasks/{task_id}/review", response_model=TaskDTO, response_model_exclude_unset=True)
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
        if repo.queued_pilot_enrolled():
            raise HTTPException(423, detail={'code': 'queued_pilot_direct_route_disabled'})
        if container_pilot_workflow is None:
            raise HTTPException(423, detail={'code': 'container_pilot_not_enabled'})
        return container_pilot_workflow.create_run(identity, payload.task_id, payload.expected_revision,
                                                   payload.artifact_digest, payload.target)

    @app.post("/api/hub/v1/container-pilot/runs/{run_id}/dispatch", response_model=RunDTO)
    async def dispatch_container_pilot(run_id: str, request: Request):
        identity = agent(request)
        if repo.queued_pilot_enrolled():
            raise HTTPException(423, detail={'code': 'queued_pilot_direct_route_disabled'})
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
        if repo.queued_pilot_enrolled():
            raise HTTPException(423, detail={'code': 'queued_pilot_direct_route_disabled'})
        if owned_pilot_service is None:
            raise HTTPException(423, detail={'code': 'owned_pilot_not_enabled'})
        if repo.get_run(run_id)['created_by'] != identity:
            raise HTTPException(403, detail={'code': 'forbidden'})
        return await owned_pilot_service.recover(run_id)

    from .work_queue_api import attach_work_queue_routes
    attach_work_queue_routes(app, repo=repo, queue=work_queue,
                             actor=actor, agent=agent, human=human)
    app.state.work_queue = work_queue

    if settings.ui_directory and settings.ui_directory.is_dir():
        app.mount("/", StaticFiles(directory=settings.ui_directory, html=True), name="dashboard")
    if settings.ingress_mode != "combined":
        from .ingress import HubIngressMiddleware
        # Added last so the boundary precedes redirects, auth and static routing.
        app.add_middleware(HubIngressMiddleware, mode=settings.ingress_mode,
                           agent_auth=settings.agent_auth,
                           cloudflare_config=settings.auth_config.cloudflare_config,
                           legacy_bus_enabled=bus_app is not None)
    return app
