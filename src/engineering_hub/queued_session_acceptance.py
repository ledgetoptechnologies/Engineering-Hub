"""Explicit pre-serving attachment for the exact existing queued manual gate.

This helper is not selected by default startup or any settings/CLI/environment
flag. It launches nothing, invents no report, and enrolls no operator identity.
"""
from pathlib import Path

from fastapi import FastAPI

from .app import Settings
from .auth import SessionStore
from .container_pilot import ContainerPilotTarget
from .pilot_ownership import PilotOwner
from .queued_pilot import QueuedContainerPilot
from .queued_pilot_runtime import QueuedPilotRuntime
from .repository import HubRepository
from .session_acceptance import (SessionAcceptanceBroker,
                                 attach_session_acceptance_routes)
from .startup_reconciliation import StartupReconciliation


class QueuedSessionAcceptanceError(RuntimeError):
    def __init__(self):
        super().__init__('queued session acceptance composition unavailable')


def attach_queued_session_acceptance(core, *, owner, repository, target, gate,
                                     operator_actor, audit, report, evidence,
                                     lifetime_seconds=600):
    """Build and attach one broker to the queued core before its first request.

    All inputs are trusted composition capabilities, not JSON or HTTP parameters.
    The operator actor must be independently enrolled and the report/identity
    evidence independently reconciled. No default audit or report is supplied.
    A successful return is attachment only, NOT operator/session/task acceptance.

    The caller retains lifecycle ownership: deny admission, drain the core's
    runtime/listeners, close the broker/client, and only then release PilotOwner.
    The existing runtime closes this same gate while retaining accepted effects.
    Serve the core and runtime on their one owned event loop; this helper cannot
    make arbitrary cross-thread direct gate permits transactional with auditing.
    """
    broker = None
    try:
        if (type(core) is not FastAPI or core.middleware_stack is not None
                or type(owner) is not PilotOwner or type(repository) is not HubRepository
                or repository.pilot_owner is not owner
                or type(target) is not ContainerPilotTarget
                or type(gate) is not StartupReconciliation
                or getattr(core.state, 'repository', None) is not repository
                or getattr(core.state, 'session_acceptance', None) is not None):
            raise QueuedSessionAcceptanceError()
        settings = getattr(core.state, 'settings', None)
        sessions = getattr(core.state, 'sessions', None)
        runtime = getattr(core.state, 'queued_pilot_runtime', None)
        bridge = getattr(repository, '_queued_pilot', None)
        if (type(settings) is not Settings or type(sessions) is not SessionStore
                or sessions.repository is not repository
                or Path(settings.database).absolute() != Path(repository.path).absolute()
                or type(runtime) is not QueuedPilotRuntime
                or runtime.status['state'] != 'not_started'
                or runtime.bridge is not bridge or type(bridge) is not QueuedContainerPilot
                or bridge.repo is not repository or bridge.owner is not owner
                or bridge.target is not target or bridge.reconciliation is not gate
                or getattr(core.state, 'event_feed', None) is not runtime.feed
                or getattr(runtime, '_application', None) is not core):
            raise QueuedSessionAcceptanceError()
        owner.assert_active(repository.path)
        target.validated_manifest()
        binding = gate.assert_local_binding if gate.async_admission_required else gate.assert_binding
        binding(owner=owner, target_id=target.target_id, manifest_digest=target.manifest_digest)
        broker = SessionAcceptanceBroker(gate=gate, owner=owner,
            target_id=target.target_id, manifest_digest=target.manifest_digest,
            executor=report.executor, report=report, evidence=evidence,
            operator_actor=operator_actor, audit=audit, lifetime_seconds=lifetime_seconds)
        broker.assert_binding(gate=gate, owner=owner, target_id=target.target_id,
                              manifest_digest=target.manifest_digest)
        attach_session_acceptance_routes(core, broker=broker,
            config=settings.auth_config, sessions=sessions)
        core.state.session_acceptance = broker
        return broker
    except BaseException as error:
        if broker is not None:
            broker.close()
        if isinstance(error, Exception):
            raise QueuedSessionAcceptanceError() from None
        raise
