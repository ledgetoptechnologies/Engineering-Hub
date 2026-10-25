"""Private shared-queue policy foundation; not application composition or enrollment.

Install explicitly, recover both original owners, mark every family ready, then
open admissions. Closing is terminal for new admissions but leaves intact bound
completion/reconciliation callbacks available to drain original operations.
Approval and global production-readiness policy remain WorkQueue's responsibility.
"""
from dataclasses import dataclass
import inspect
import re
from types import MappingProxyType

from .repository import HubError
from .work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker


_CALLBACKS = ('target_verifier', 'claim_verifier', 'completion_verifier', 'reconciliation_verifier')


def _require(value, code='execution_policy_changed'):
    if not value:
        raise HubError(code, 409)


def _action(value):
    return (value.machine_id, value.environments, value.project, value.requires_execution_proof)


def _handler_state(callback):
    """Detect handler/owner-method replacement without freezing live policy state."""
    function = callback.__func__ if inspect.ismethod(callback) else callback
    owner = callback.__self__ if inspect.ismethod(callback) else None
    return (function, getattr(function, '__code__', None), owner,
            getattr(type(callback), '__call__', None))


def _handler_intact(callback, state):
    if any(current is not original for current, original in zip(_handler_state(callback), state)):
        return False
    function, _, owner, _ = state
    if owner is not None:
        current = getattr(owner, function.__name__, None)
        return (inspect.ismethod(current) and current.__self__ is owner
                and current.__func__ is function)
    return True


@dataclass(frozen=True)
class ActionPolicyBinding:
    family: str
    actions: object
    worker: WorkWorker
    target: object
    claim: object
    completion: object
    reconciliation: object


class SharedExecutionPolicies:
    def __init__(self, repo, queue, *, expected_families=('pilot', 'modular')):
        _require(type(queue) is WorkQueue and queue.repo is repo, 'invalid_execution_policy_queue')
        _require(type(expected_families) is tuple and 1 <= len(expected_families) <= 16
                 and all(type(n) is str and re.fullmatch('[a-z][a-z0-9_-]{0,63}', n)
                         for n in expected_families)
                 and len(set(expected_families)) == len(expected_families), 'invalid_execution_policy_families')
        self.repo, self.queue = repo, queue
        self._repo, self._queue = repo, queue
        self._families = frozenset(expected_families)
        self._actions_object = queue.actions
        self._actions = dict(queue.actions)
        self._action_values = {k: _action(v) for k, v in self._actions.items()}
        self._initial = tuple(getattr(queue, n) for n in _CALLBACKS)
        self._authority = (queue.approval_verifier, queue.production_readiness_verifier)
        self._bindings = MappingProxyType({})
        self._snapshots = {}
        self._routing = MappingProxyType({})
        self._ready = frozenset()
        self._installed = self._open = self._closed = False

        # Store actual closure objects, rather than newly allocated bound methods.
        def target(actor, action, environment, value):
            return self._dispatch('target', action, (actor, action, environment, value), admission=True)

        def claim(worker, action, environment, machine, value):
            return self._dispatch('claim', action, (worker, action, environment, machine, value), admission=True)

        def completion(job, evidence_id, outcome):
            return self._proof('completion', job, evidence_id, outcome)

        def reconciliation(job, evidence_id, outcome):
            return self._proof('reconciliation', job, evidence_id, outcome)

        self._dispatchers = (target, claim, completion, reconciliation)
        self._dispatcher_code = tuple(callback.__code__ for callback in self._dispatchers)
        for name, callback in zip(_CALLBACKS, self._dispatchers):
            setattr(self, name, callback)

    def _intact(self, *, installed=True):
        if (self.repo is not self._repo or self.queue is not self._queue
                or self.queue.repo is not self._repo
                or self.queue.actions is not self._actions_object
                or set(self.queue.actions) != set(self._actions)
                or any(type(self.queue.actions[k]) is not WorkAction
                       or self.queue.actions[k] is not v or _action(v) != self._action_values[k]
                       for k, v in self._actions.items())
                or self.queue.approval_verifier is not self._authority[0]
                or self.queue.production_readiness_verifier is not self._authority[1]):
            return False
        for family, binding in self._bindings.items():
            snapshot = self._snapshots[family]
            if (binding is not snapshot[0] or binding.family != family
                    or binding.actions is not snapshot[1] or binding.worker is not snapshot[2]
                    or (binding.worker.agent, binding.worker.instance_id) != snapshot[3]):
                return False
            for key, (handler, state) in snapshot[4].items():
                if getattr(binding, key) is not handler or not _handler_intact(handler, state):
                    return False
        if installed:
            return (self._installed and getattr(self.queue, '_shared_execution_policies', None) is self
                    and getattr(self.queue, '_shared_execution_required', None) is True
                    and self._bindings is self._installed_bindings
                    and self._routing is self._installed_routing
                    and self._snapshots is self._installed_snapshots
                    and all(getattr(self.queue, n) is callback
                    and getattr(self, n) is callback
                    and callback.__code__ is code
                    for n, callback, code in zip(_CALLBACKS, self._dispatchers, self._dispatcher_code)))
        return (not hasattr(self.queue, '_shared_execution_policies')
                and not hasattr(self.queue, '_shared_execution_required'))

    def bind(self, family, actions, worker, *, target, claim, completion, reconciliation):
        _require(not self._installed and not self._closed and family in self._families
                 and family not in self._bindings, 'execution_policy_binding_closed')
        _require(self._intact(installed=False))
        _require(type(actions) is dict and bool(actions) and type(worker) is WorkWorker,
                 'invalid_execution_policy_binding')
        _require(all(k in self._actions and type(v) is WorkAction
                     and v is self._actions[k] and v.requires_execution_proof is True
                     for k, v in actions.items()), 'invalid_execution_policy_binding')
        _require(not set(actions) & set(self._routing), 'execution_policy_action_overlap')
        callbacks = dict(target=target, claim=claim, completion=completion, reconciliation=reconciliation)
        _require(all(callable(v) and not inspect.iscoroutinefunction(v) for v in callbacks.values()),
                 'invalid_execution_policy_handler')
        binding = ActionPolicyBinding(family, MappingProxyType(dict(actions)), worker, **callbacks)
        self._snapshots[family] = (binding, binding.actions, worker, (worker.agent, worker.instance_id),
                                  {k: (v, _handler_state(v)) for k, v in callbacks.items()})
        self._bindings = MappingProxyType(dict(self._bindings) | {family: binding})
        self._routing = MappingProxyType(dict(self._routing) | {k: family for k in actions})
        return binding

    def install(self):
        _require(not self._installed and not self._closed, 'execution_policy_install_closed')
        _require(self._intact(installed=False))
        _require(all(getattr(self.queue, n) is callback
                     for n, callback in zip(_CALLBACKS, self._initial)))
        _require(set(self._bindings) == self._families, 'execution_policy_not_ready')
        self._snapshots = MappingProxyType(dict(self._snapshots))
        self._installed_bindings = self._bindings
        self._installed_routing = self._routing
        self._installed_snapshots = self._snapshots
        for name, callback in zip(_CALLBACKS, self._dispatchers):
            setattr(self.queue, name, callback)
        self.queue._shared_execution_policies = self
        self.queue._shared_execution_required = True
        self._installed = True

    def assert_integrity(self):
        """Raise on drift; closing admissions does not invalidate drain policy."""
        _require(self._intact())

    def action_bound(self, action_key):
        try:
            self.assert_integrity()
            return type(action_key) is str and action_key in self._routing
        except Exception:
            return False

    def assert_binding(self, family, binding=None, *, admission=False):
        _require(type(admission) is bool, 'invalid_execution_policy_guard')
        _require(self._intact() and family in self._bindings)
        actual = self._bindings[family]
        _require(binding is None or binding is actual)
        if admission:
            _require(self._open and not self._closed and self._ready == self._families,
                     'execution_policy_admission_closed')
        return actual

    def ready(self, family):
        self.assert_binding(family)
        _require(not self._closed, 'execution_policy_admission_closed')
        self._ready = self._ready | {family}

    def _reserved_families(self):
        # Read-only: this is not an owner/receipt/reconciliation exemption.
        with self._repo._connection() as db:
            rows = db.execute('''SELECT m.machine_id,m.job_id,j.action_key,j.machine_id AS job_machine,j.state
                FROM hub_work_machines m LEFT JOIN hub_work_jobs j ON j.id=m.job_id
                WHERE m.job_id IS NOT NULL''').fetchall()
        return all(row['action_key'] in self._routing and row['state'] in {'claimed', 'held'}
                   and row['machine_id'] == row['job_machine'] == self._actions[row['action_key']].machine_id
                   for row in rows)

    def open(self):
        self._open = False
        _require(self._intact() and not self._closed)
        _require(self._reserved_families(), 'foreign_hold_family_absent')
        _require(set(self._routing) == set(self._actions) and self._ready == self._families,
                 'execution_policy_not_ready')
        self._open = True

    def close(self):
        self._open, self._closed = False, True

    def _dispatch(self, kind, action_key, args, *, admission):
        try:
            family = self._routing.get(action_key)
            if family is None:
                return False
            binding = self.assert_binding(family, admission=admission)
            action = self._actions[action_key]
            if kind == 'target':
                if args[2] not in action.environments or type(args[3]) is not WorkTarget:
                    return False
            elif kind == 'claim':
                if (type(args[0]) is not WorkWorker or args[0] != binding.worker
                        or args[2] not in action.environments or args[3] != action.machine_id
                        or type(args[4]) is not WorkTarget):
                    return False
            result = getattr(binding, kind)(*args)
            if inspect.iscoroutine(result) and inspect.getcoroutinestate(result) == inspect.CORO_CREATED:
                result.close()
            # Recheck after arbitrary trusted policy code as well as before it.
            self.assert_binding(family, binding, admission=admission)
            return result is True
        except Exception:
            return False

    def _proof(self, kind, job, evidence_id, outcome):
        try:
            if type(job) is not dict or type(job.get('id')) is not str:
                return False
            with self._repo._connection() as db:
                row = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (job['id'],)).fetchone()
            if row is None:
                return False
            stored = WorkQueue._public(row)
            immutable = ('id', 'action_key', 'machine_id', 'project', 'environment',
                         'target', 'requires_execution_proof', 'worker', 'fence')
            if any(job.get(k) != stored[k] for k in immutable):
                return False
            family = self._routing.get(stored['action_key'])
            binding = self.assert_binding(family)
            action = self._actions[stored['action_key']]
            if (stored['worker'] != binding.worker.encoded() or stored['machine_id'] != action.machine_id
                    or stored['project'] != action.project or stored['environment'] not in action.environments
                    or stored['requires_execution_proof'] is not True):
                return False
            return self._dispatch(kind, stored['action_key'], (job, evidence_id, outcome), admission=False)
        except Exception:
            return False
