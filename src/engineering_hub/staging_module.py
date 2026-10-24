"""Optional composition of the private worker; never a second execution owner.

An absent/invalid optional file leaves the Hub running. No listener, daemon,
credential or Cloudflare policy is created here. Configuration only references
operator-provisioned private files and explicitly reviewed recipe names.
"""
from dataclasses import dataclass
from pathlib import Path
import uuid

from .staging_client import load_optional_config, StagingClient
from .staging_resources import StagingResourceStore
from .work_queue import WorkAction, WorkQueue, WorkWorker, ClosedClaimPolicy


@dataclass
class StagingModule:
    controller: object = None
    config: object = None
    status: dict = None
    preview_broker: object = None
    preview_app: object = None
    client: object = None


def compose_staging(repo, *, path, machine_id, queue=None, conflicts=False,
                    client_factory=StagingClient):
    config, status = load_optional_config(Path(path))
    result = StagingModule(config=config, status=status)
    if config is None or not config.enabled:
        return result
    if conflicts:
        result.status = {'state': 'existing_executor_owner', 'enabled': False}
        return result
    if not config.projects:
        result.status = {'state': 'no_profiles', 'enabled': False}
        return result
    client = None
    original_actions = dict(queue.actions) if queue is not None else None
    try:
        # Import only for enabled, explicitly configured installations.
        from .staging_controller import StagingController
        from .work_queue import STAGING_SERVICE_EXECUTOR
        actions = {}
        registry = {}
        for project, policy in config.projects.items():
            with repo._connection() as db:
                repo._project(project, db)
            for recipe in policy['recipes']:
                # Hashing keeps dynamic project/profile names inside queue key bounds.
                import hashlib
                key = 'modular.' + hashlib.sha256((project + '\0' + recipe).encode()).hexdigest()
                actions[project, recipe] = key
                registry[key] = WorkAction(machine_id, project=project)
        if queue is None:
            queue = WorkQueue(repo, registry, claim_verifier=ClosedClaimPolicy())
        else:
            # Add only disjoint actions to the same physical-machine queue;
            # existing production verifiers, pauses, reservations and holds stay.
            if any(key in queue.actions and queue.actions[key] != value for key, value in registry.items()):
                raise ValueError('staging_action_conflict')
            if len(set(queue.actions) | set(registry)) > 256:
                raise ValueError('staging_registry_full')
            queue.actions.update(registry)
        principal = object()
        resources = StagingResourceStore(repo, admin_actors=config.admin_actors,
                                        control_principal=principal)
        client = client_factory(config)
        result.client = client
        result.controller = StagingController(repo, queue, client, resources=resources,
            control_principal=principal, actions=actions,
            worker=WorkWorker(STAGING_SERVICE_EXECUTOR, str(uuid.uuid4())))
        result.status = {'state': 'configured', 'enabled': True}
    except Exception:
        if original_actions is not None:
            queue.actions = original_actions
        # No exception text, private filenames, endpoint or credential is exposed.
        # Client construction does not open sockets. If a future constructor
        # acquires an async resource, it must own rollback before raising.
        result.status = {'state': 'unavailable', 'enabled': False}
    return result


def configure_preview(module, *, repo, sessions, hub_origin):
    if module.controller is None or module.config.preview_origin is None:
        return
    from .staging_preview import PreviewBroker
    try:
        broker = PreviewBroker(repo, module.controller.resources,
            hub_origin=hub_origin, preview_origin=module.config.preview_origin,
            session_alive=sessions.preview_session_alive)
        module.preview_broker = broker
        module.preview_app = broker.app(module.controller.preview_file)
    except ValueError:
        # Invalid optional preview origin cannot take the primary Hub offline.
        module.status = dict(module.status, preview_state='invalid')
