"""Closed staging HTTP contracts over explicitly composed control-plane services.

No executor, worker, policy, preview origin or credential is discovered here.
Authentication runs before bounded JSON parsing. Human writes use the existing
mutation callback (including CSRF); job submissions accept only agent identity.
"""
import inspect
import json
from typing import Annotated, Literal

from fastapi import Path, Query, Request
from starlette.responses import Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .repository import AGENTS, HubError, HubRepository


Kind = Literal['workspace', 'build', 'test_run', 'preview']
State = Literal['provisioning', 'ready', 'failed', 'expiring', 'deleting', 'deleted', 'delete_failed']
Action = Literal['read', 'retain', 'cleanup', 'build', 'test', 'preview']
Project = Annotated[str, Field(pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')]
Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$')]
Identity = Annotated[str, Path(min_length=36, max_length=36,
    pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')]
MAX_SOURCE_BASE64 = ((8 * 1024 * 1024 + 2) // 3) * 4


class ClosedInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class StagingSubmission(ClosedInput):
    request_id: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    review_task_id: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    review_task_revision: int = Field(ge=1)
    project: Project
    recipe: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    source_commit: str = Field(pattern=r'^[a-f0-9]{40}$')
    source_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    source_zip_base64: str = Field(min_length=1, max_length=MAX_SOURCE_BASE64)
    source_commit_object_base64: str = Field(min_length=1, max_length=87384)
    topic: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')


class StagingKeep(ClosedInput):
    mode: Literal['until', 'pinned', 'none']
    until: str | None = Field(default=None, max_length=64)
    reason: str = Field(default='', max_length=1000)
    expected_revision: int = Field(ge=1)


class StagingCleanupPlan(ClosedInput):
    project: Project | None = None
    kinds: list[Kind] | None = Field(default=None, min_length=1, max_length=4)


class StagingCleanupApply(ClosedInput):
    plan_id: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    digest: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')


class StagingPolicy(ClosedInput):
    expected_revision: int = Field(ge=0)
    grants: dict[str, Annotated[list[Action], Field(max_length=6)]] = Field(max_length=1000)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HubError('invalid_staging_json')
        result[key] = value
    return result


async def _payload(request, model, *, maximum=65536):
    """Bound actual streamed bytes, not only the untrusted length header."""
    sizes = request.headers.getlist('content-length')
    if len(sizes) > 1 or sizes and (not sizes[0].isdigit() or int(sizes[0]) > maximum):
        raise HubError('staging_request_too_large', 413)
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        raise HubError('staging_json_required', 415)
    body = bytearray()
    async for part in request.stream():
        if len(body) + len(part) > maximum:
            raise HubError('staging_request_too_large', 413)
        body.extend(part)
    try:
        value = json.loads(body, object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        return model.model_validate(value)
    except ValidationError as exc:
        # Keep raw source archives/credentials out of validation response bodies.
        errors = [{key: value for key, value in error.items() if key not in ('input', 'ctx')}
                  for error in exc.errors()]
        raise RequestValidationError(errors) from None
    except (ValueError, UnicodeError, RecursionError):
        raise HubError('invalid_staging_json') from None


def attach_staging_routes(app, controller, actor, human, preview_broker=None, preview_session=None,
                          module_status=None, diagnostic_session=None, observer=None,
                          human_admission=None):
    """Compose existing authenticated callbacks; attaching starts no work."""
    def configured():
        if controller is None:
            raise HubError('staging_not_configured', 503)
        return controller

    def authenticated(request, *, mutation=False, human_only=False):
        # Duplicate/malformed bearer input must not fall through to human cookies.
        from .work_queue_api import _authorization
        _authorization(request)
        principal = (human if human_only else actor)(request, mutation=True) if mutation else (human if human_only else actor)(request)
        HubRepository._actor(principal, human=human_only)
        return principal

    def revalidate(request, principal, *, human_only=False):
        if authenticated(request, mutation=True, human_only=human_only) != principal:
            raise HubError('staging_identity_changed', 403)

    def admission(request, principal):
        if not principal.startswith('human:'):
            return None
        if not callable(human_admission):
            raise HubError('staging_human_admission_unavailable', 503)
        return human_admission(request, principal)

    @app.get('/api/hub/v1/staging/status')
    async def status(request: Request):
        principal = authenticated(request)
        if controller is None:
            return {'configured': False, 'running': False, 'reachable': False,
                    'profiles': {}, 'previews_configured': False, **({'state': module_status['state']}
                    if module_status is not None else {})}
        result = await controller.status(principal)
        # Only the controller's public status fields are surfaced. Never expose
        # private endpoint, credentials, configured host paths or raw exceptions.
        return dict({'configured': False, 'running': False, 'reachable': False, 'profiles': {}},
            **{key: value for key, value in result.items() if key in {
            'configured', 'running', 'reachable', 'worker_reachable', 'state', 'reason',
            'profiles', 'supported_profiles', 'projects', 'active_job_id', 'held_jobs', 'can_reconcile'}},
            previews_configured=preview_broker is not None)

    @app.get('/api/hub/v1/staging/runtime')
    async def runtime(request: Request):
        principal = authenticated(request, human_only=True)
        if observer is False:
            raise HubError('staging_not_configured', 503)
        if observer is not None:
            if principal not in observer.admin_actors:
                raise HubError('staging_admin_required', 403)
            result = await observer.runtime(principal)
        else:
            if controller is None:
                raise HubError('staging_not_configured', 503)
            if principal not in controller.resources.admin_actors:
                raise HubError('staging_admin_required', 403)
            result = await controller.runtime(principal)
        if result.get('snapshot') is not None:
            from .staging_runtime import validate_runtime_snapshot
            try:
                validate_runtime_snapshot(result['snapshot'])
            except ValueError:
                raise HubError('staging_runtime_unavailable', 503) from None
        return {key: result[key] for key in ('configured', 'state', 'snapshot')}

    @app.get('/api/hub/v1/staging/resources')
    def resources(request: Request, project: Project | None = None,
                  kind: Kind | None = None, state: State | None = None,
                  after: Annotated[str | None, Query(min_length=36, max_length=36,
                    pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')] = None,
                  limit: int = Query(default=100, ge=1, le=100)):
        principal = authenticated(request)
        return configured().resources.list_resources(principal, project, kind=kind,
                                                      state=state, after=after, limit=limit)

    @app.get('/api/hub/v1/staging/resources/{resource_id}')
    def resource(resource_id: Identity, request: Request):
        principal = authenticated(request)
        return configured().resources.get_resource(principal, resource_id)

    def diagnostic_context(request):
        principal = authenticated(request, human_only=True)
        service = configured()
        if not callable(diagnostic_session):
            raise HubError('staging_diagnostic_unavailable', 503)
        alive = diagnostic_session(request, principal)
        if not callable(alive):
            raise HubError('staging_diagnostic_unavailable', 503)
        return service, principal, alive

    @app.get('/api/hub/v1/staging/diagnostics/{resource_id}')
    async def diagnostic_files(resource_id: Identity, request: Request):
        from .staging_diagnostics import diagnostic_manifest
        service, principal, alive = diagnostic_context(request)
        return await diagnostic_manifest(service.resources, service.client, principal, resource_id,
                                         session_alive=alive)

    @app.get('/api/hub/v1/staging/diagnostics/{resource_id}/files/{relative_path:path}',
             response_class=Response, responses={200: {'content': {
                 'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}})
    async def diagnostic_file(resource_id: Identity, relative_path: str, request: Request):
        from .staging_diagnostics import diagnostic_download
        service, principal, alive = diagnostic_context(request)
        return await diagnostic_download(service.resources, service.client, principal, resource_id,
                                         relative_path, session_alive=alive)

    @app.post('/api/hub/v1/staging/resources/{resource_id}/keep')
    async def keep(resource_id: Identity, request: Request):
        principal = authenticated(request, mutation=True)
        service = configured()
        payload = await _payload(request, StagingKeep)
        revalidate(request, principal)
        return service.resources.set_keep(principal, resource_id, **payload.model_dump(),
                                          admission_guard=admission(request, principal))

    @app.post('/api/hub/v1/staging/cleanup/plan')
    async def cleanup_plan(request: Request):
        principal = authenticated(request, mutation=True)
        service = configured()
        payload = await _payload(request, StagingCleanupPlan)
        revalidate(request, principal)
        return service.resources.cleanup_plan(principal, **payload.model_dump(),
                                              admission_guard=admission(request, principal))

    @app.post('/api/hub/v1/staging/cleanup/apply')
    async def cleanup_apply(request: Request):
        principal = authenticated(request, mutation=True)
        service = configured()
        payload = await _payload(request, StagingCleanupApply)
        revalidate(request, principal)
        if principal.startswith('human:'):
            if not callable(human_admission):
                raise HubError('staging_human_admission_unavailable', 503)
            guard = human_admission(request, principal)
            return await service.cleanup_apply(principal, payload.plan_id, payload.digest,
                                               admission_guard=guard)
        return await service.cleanup_apply(principal, payload.plan_id, payload.digest)

    @app.post('/api/hub/v1/staging/jobs', status_code=202)
    async def submit(request: Request):
        principal = authenticated(request, mutation=True)
        if principal not in AGENTS:
            raise HubError('staging_agent_required', 403)
        service = configured()
        payload = await _payload(request, StagingSubmission, maximum=12 * 1024 * 1024)
        revalidate(request, principal)
        return await service.submit(principal, payload.model_dump())

    @app.get('/api/hub/v1/staging/jobs/{job_id}')
    def job(job_id: Identity, request: Request):
        principal = authenticated(request)
        return configured().get_job(principal, job_id)

    @app.post('/api/hub/v1/staging/jobs/{job_id}/reconcile')
    async def reconcile(job_id: Identity, request: Request):
        principal = authenticated(request, mutation=True, human_only=True)
        service = configured()
        await _payload(request, ClosedInput)
        revalidate(request, principal, human_only=True)
        return await service.reconcile_for(principal, job_id, admission_guard=admission(request, principal))

    @app.get('/api/hub/v1/staging/projects/{project}/policy')
    def get_policy(project: Project, request: Request):
        principal = authenticated(request, human_only=True)
        return configured().resources.get_policy(principal, project)

    @app.put('/api/hub/v1/staging/projects/{project}/policy')
    async def set_policy(project: Project, request: Request):
        principal = authenticated(request, mutation=True, human_only=True)
        service = configured()
        payload = await _payload(request, StagingPolicy)
        revalidate(request, principal, human_only=True)
        return service.resources.set_policy(principal, project, **payload.model_dump(),
                                            admission_guard=admission(request, principal))

    @app.post('/api/hub/v1/staging/resources/{resource_id}/preview-session')
    async def preview(resource_id: Identity, request: Request):
        principal = authenticated(request, mutation=True, human_only=True)
        configured()
        if preview_broker is None or not callable(preview_session):
            raise HubError('staging_preview_not_configured', 503)
        await _payload(request, ClosedInput)
        session = preview_session(request)
        if inspect.isawaitable(session):
            session = await session
        revalidate(request, principal, human_only=True)
        result = preview_broker.issue(principal, resource_id, *session)
        return {'url': result['preview_url'], 'expires_at': result['expires_at']}
