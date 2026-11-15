"""Optional composition of the private worker; never a second execution owner.

An absent/invalid optional file leaves the Hub running. No listener, daemon,
credential or Cloudflare policy is created here. Configuration only references
operator-provisioned private files and explicitly reviewed recipe names.
"""
from dataclasses import dataclass
import inspect
import re
from pathlib import Path
from types import MappingProxyType
import uuid

from .staging_client import load_optional_config, StagingClient, StagingConnectionConfig
from .staging_resources import StagingResourceStore
from .work_queue import WorkAction, WorkQueue, WorkWorker, ClosedClaimPolicy, STAGING_SERVICE_EXECUTOR, _key
from .repository import HubError, HubRepository
from .staging_action_key import staging_action_key


@dataclass
class StagingModule:
    controller: object = None
    config: object = None
    status: dict = None
    preview_broker: object = None
    preview_app: object = None
    client: object = None


@dataclass(frozen=True)
class SharedStagingDraft:
    """Uninstalled preparation, not an application activation capability."""
    queue: WorkQueue
    policies: object
    module: StagingModule


class SharedStagingPreparationError(HubError):
    """Caller retains any accepted client for asynchronous cleanup."""
    def __init__(self, draft=None):
        super().__init__('staging_shared_preparation_failed', 409)
        self.draft = draft


def configured_staging_actions(config, *, machine_id):
    """Pure canonical key derivation; no queue, grants, ledger or client writes."""
    if (type(config) is not StagingConnectionConfig
            or type(config.projects) not in (dict, MappingProxyType)
            or not 1 <= len(config.projects) <= 100):
        raise ValueError('staging_profiles_required')
    actions, registry = {}, {}
    for project, policy in config.projects.items():
        if (type(project) is not str or re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', project) is None
                or type(policy) not in (dict, MappingProxyType) or set(policy) != {'recipes'}
                or type(policy['recipes']) not in (list, tuple) or not 1 <= len(policy['recipes']) <= 32
                or any(type(name) is not str or re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', name) is None
                       for name in policy['recipes'])
                or len(set(policy['recipes'])) != len(policy['recipes'])):
            raise ValueError('staging_profiles_invalid')
        for recipe in policy['recipes']:
            key = staging_action_key(project, recipe)
            if key in registry:
                raise ValueError('staging_action_conflict')
            actions[project, recipe] = key
            registry[key] = WorkAction(machine_id, project=project)
    if len(registry) > 256:
        raise ValueError('staging_registry_full')
    return registry, actions


def _configured_projects(repo, config):
    with repo._connection() as db:
        for project in config.projects:
            repo._project(project, db)


def prepare_shared_staging(repo, *, path, machine_id, pilot_actions, target_verifier,
                           client_factory=StagingClient):
    """Prepare one complete queue before the shared registry snapshots it.

    Does not construct a pilot, install policies, recover work, mark readiness,
    open admission or start clients/loops. On failure after accepting an exact
    StagingClient, the raised error retains draft.module.client: its caller must
    await aclose(). Factories own invalid/unreturned resources and must be fresh,
    synchronous and network-free during construction.
    """
    config, status = load_optional_config(Path(path))
    if config is None:
        if status.get('state') == 'not_configured':
            return None
        raise SharedStagingPreparationError()
    if not config.enabled:
        return None
    draft = None
    try:
        if (type(repo) is not HubRepository or type(pilot_actions) is not dict or not pilot_actions
                or not callable(target_verifier) or inspect.iscoroutinefunction(target_verifier)
                or inspect.isasyncgenfunction(target_verifier)
                or inspect.iscoroutinefunction(getattr(target_verifier, '__call__', None))
                or inspect.isasyncgenfunction(getattr(target_verifier, '__call__', None))
                or not callable(client_factory) or inspect.iscoroutinefunction(client_factory)
                or inspect.isasyncgenfunction(client_factory)
                or inspect.iscoroutinefunction(getattr(client_factory, '__call__', None))
                or inspect.isasyncgenfunction(getattr(client_factory, '__call__', None))):
            raise ValueError('staging_shared_inputs')
        registry, actions = configured_staging_actions(config, machine_id=machine_id)
        _configured_projects(repo, config)
        for key, action in pilot_actions.items():
            _key(key)
            if (type(action) is not WorkAction or action.machine_id != machine_id
                    or action.environments != frozenset({'staging'}) or action.requires_execution_proof is not True):
                raise ValueError('staging_pilot_action_invalid')
        if set(pilot_actions) & set(registry) or len(pilot_actions) + len(registry) > 256:
            raise ValueError('staging_action_conflict')
        from .execution_policy import SharedExecutionPolicies
        from .staging_controller import StagingController
        queue = WorkQueue(repo, dict(pilot_actions) | registry,
                          target_verifier=target_verifier, claim_verifier=ClosedClaimPolicy())
        policies = SharedExecutionPolicies(repo, queue)
        module = StagingModule(config=config, status={'state': 'preparing', 'enabled': False})
        draft = SharedStagingDraft(queue, policies, module)
        principal = object()
        resources = StagingResourceStore(repo, admin_actors=config.admin_actors, control_principal=principal)
        accepted = client_factory(config)
        if type(accepted) is not StagingClient:
            if inspect.iscoroutine(accepted) and inspect.getcoroutinestate(accepted) == inspect.CORO_CREATED:
                accepted.close()
            raise ValueError('staging_client_factory_invalid')
        module.client = accepted
        if accepted.config is not config or accepted._client.is_closed:
            raise ValueError('staging_client_binding_invalid')
        module.controller = StagingController(repo, queue, accepted, resources=resources,
            control_principal=principal, actions=actions,
            worker=WorkWorker(STAGING_SERVICE_EXECUTOR, str(uuid.uuid4())), execution_policies=policies)
        module.status = {'state': 'prepared', 'enabled': True}
        return draft
    except Exception:
        raise SharedStagingPreparationError(draft) from None


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
        registry, actions = configured_staging_actions(config, machine_id=machine_id)
        _configured_projects(repo, config)
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
