"""Owner-only status/recovery for the explicit private shared composition."""
from fastapi import Request
from pydantic import BaseModel, ConfigDict
from typing import Literal

from .repository import HubError
from .shared_staging_lifecycle import SharedStagingLifecycle
from .staging_api import _payload
from .work_queue_api import _authorization


class _Recover(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SharedLifecycleStatus(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    state: Literal['not_started', 'pending', 'recovering', 'recovery_required',
                   'active', 'stopping', 'stopped', 'shutdown_failed']
    reason: Literal['operator_acceptance_required', 'original_operations_pending',
                    'original_operations_unverified', 'owned_drain_failed'] | None


def attach_shared_staging_routes(app, shared, human):
    def authorized(request, *, mutation=False):
        _authorization(request)
        principal = human(request, mutation=True) if mutation else human(request)
        if shared is None:
            raise HubError('shared_staging_not_configured', 503)
        if type(shared) is not SharedStagingLifecycle:
            raise HubError('shared_staging_lifecycle_changed', 409)
        shared.validate_application(app.state.repository, app.state.work_queue,
                                    app.state.queued_pilot_runtime, shared.feed)
        if principal not in shared.staging_module.config.admin_actors:
            raise HubError('staging_admin_required', 403)
        return shared

    @app.get('/api/hub/v1/staging/shared/status', response_model=SharedLifecycleStatus)
    async def status(request: Request):
        return authorized(request).status

    @app.post('/api/hub/v1/staging/shared/recover', response_model=SharedLifecycleStatus)
    async def recover(request: Request):
        original = authorized(request, mutation=True)
        await _payload(request, _Recover, maximum=1024)
        authorized(request, mutation=True)
        return await original.recover_and_open()
