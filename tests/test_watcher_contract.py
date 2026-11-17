"""Contract fixtures only: no claim of live app delivery or reboot acceptance."""
import pytest
from pydantic import ValidationError

from engineering_hub.watcher_contract import (
    AdapterCapabilities, DeliveryReceipt, SessionTarget, TaskBinding, TaskMessage,
    TaskRoute, plan_task_delivery,
)

TASK = '6b3d39bb-8e68-4da2-808c-63e6c3b81067'
OTHER = '6b3d39bb-8e68-4da2-808c-63e6c3b81068'
MESSAGE = '8da43983-ce78-4b5a-8db8-a81b3a2db7f0'


def message(**changes):
    return TaskMessage(**(dict(message_id=MESSAGE, topic='engineering-hub',
                              task_id=TASK, from_role='reviewer', to_role='owner',
                              recipient='codex', correlation_id=f'engineering-hub/task-{TASK}')
                          | changes))


def target(**changes):
    return SessionTarget(**(dict(agent='codex', host_id='pc-one', session_id='session-one',
                                generation='runtime-incarnation-one') | changes))


def binding(**changes):
    return TaskBinding(route=TaskRoute(**(dict(project='engineering-hub', task_id=TASK,
                                               role='owner') | changes)), target=target())


CAPABLE = AdapterCapabilities(authoritative_generation=True, conditional_accept=True)


def plan(bindings=None, observed=None, capabilities=CAPABLE, msg=None):
    return plan_task_delivery(msg or message(), (binding(),) if bindings is None else bindings,
                              capabilities=capabilities, observed=observed)


def test_exact_binding_preserves_message_and_entire_target():
    result = plan(observed=target())
    assert (result.action, result.escalate, result.reason) == ('ready', False, 'exact_binding')
    assert result.request.message_id == MESSAGE
    assert result.request.binding == binding()


@pytest.mark.parametrize('change', [dict(task_id=OTHER), dict(role='reviewer'),
                                    dict(role='lead'), dict(project='other-project')])
def test_no_task_role_or_project_fallback(change):
    result = plan(bindings=(binding(**change),), observed=target())
    assert result.reason == 'binding_missing'
    assert result.action == 'hold' and result.escalate and result.request is None


def test_duplicates_hold_even_identical_targets():
    assert plan(bindings=(binding(), binding()), observed=target()).reason == 'binding_ambiguous'


@pytest.mark.parametrize('change', [dict(generation='new'), dict(session_id='new'),
                                    dict(host_id='other-pc'), dict(agent='claude')])
def test_stale_observation_never_retargets(change):
    result = plan(observed=target(**change))
    assert result.reason == 'session_stale'
    assert result.action == 'hold' and result.escalate and result.request is None


def test_missing_session_holds():
    assert plan().reason == 'session_missing'


def test_authenticated_recipient_must_match_binding():
    assert plan(msg=message(recipient='claude'), observed=target()).reason == 'recipient_mismatch'


@pytest.mark.parametrize('cap', [AdapterCapabilities(),
                                AdapterCapabilities(authoritative_generation=True),
                                AdapterCapabilities(conditional_accept=True)])
def test_missing_atomic_generation_capability_holds(cap):
    assert plan(observed=target(), capabilities=cap).reason == 'adapter_unsupported'


@pytest.mark.parametrize('generation', [None, '', ' ', 1, ' x', 'x\n'])
def test_missing_or_invalid_generation_rejected(generation):
    with pytest.raises(ValidationError):
        target(generation=generation)


def test_omitted_generation_cannot_be_fabricated():
    with pytest.raises(ValidationError):
        SessionTarget(agent='codex', host_id='pc', session_id='session')


@pytest.mark.parametrize('change', [dict(task_id=TASK[:8]), dict(task_id=TASK.upper()),
                                    dict(task_id=TASK.replace('-', '')),
                                    dict(correlation_id='engineering-hub'),
                                    dict(correlation_id=f'engineering-hub/task-{OTHER}'),
                                    dict(correlation_id='projects-lead'),
                                    dict(topic='other-project'), dict(to_role='lead'),
                                    dict(kind='escalation'), dict(message_id='short'),
                                    dict(from_role='worker')])
def test_malformed_or_cross_topic_worker_message_rejected(change):
    with pytest.raises(ValidationError):
        message(**change)


def test_explicit_worker_escalation_requires_exact_lead_binding():
    msg = message(to_role='lead', kind='escalation')
    assert plan(msg=msg, observed=target()).reason == 'binding_missing'
    result = plan(msg=msg, bindings=(binding(role='lead'),), observed=target())
    assert result.request.binding.route.role == 'lead'
    with pytest.raises(ValidationError):
        message(to_role='lead', kind='escalation', from_role='lead')


@pytest.mark.asyncio
async def test_adapter_contract_rejects_restart_between_preflight_and_acceptance():
    class FakeAdapter:
        """Fixture of atomic receiver semantics, not an actual app implementation."""
        current = target()
        accepted = []

        def capabilities(self):
            return CAPABLE

        async def observe(self, expected):
            return self.current

        async def accept_if_current(self, request):
            if request.binding.target != self.current:
                return DeliveryReceipt(request=request, outcome='stale')
            self.accepted.append(request.message_id)
            return DeliveryReceipt(request=request, outcome='accepted')

    adapter = FakeAdapter()
    result = plan(observed=await adapter.observe(target()), capabilities=adapter.capabilities())
    adapter.current = target(generation='restarted')
    receipt = await adapter.accept_if_current(result.request)
    assert receipt.outcome == 'stale'
    assert adapter.accepted == []
