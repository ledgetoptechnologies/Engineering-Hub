"""Real Linux owner; synthetic observer/audit, not remote/NAS acceptance."""
import asyncio
from dataclasses import replace
import hashlib
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engineering_hub.current_executor_observation import VerifiedExecutorObservation
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.session_acceptance import SessionAcceptanceBroker, SessionAcceptanceError
from engineering_hub.startup_reconciliation import ExecutorSessionIdentity, OperatorReconciliation, StartupReconciliation
from tests.test_hub_session_acceptance import browser

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='real Linux ownership')
TARGET, DIGEST, ACTOR = 'engineering-hub:staging', 'sha256:' + 'a' * 64, 'human:123'


@pytest.fixture
def case(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    database = root / 'hub.sqlite3'
    identity = ExecutorSessionIdentity(str(uuid4()), DIGEST)
    entered, release = threading.Event(), threading.Event()
    state = SimpleNamespace(calls=0, block=False, lifetime=1.0, identities=[], records=[], threads=[])
    def observe():
        state.calls += 1
        state.threads.append(threading.get_ident())
        started = time.monotonic()
        started_wall_ms = int(time.time() * 1000)
        entered.set()
        if state.block and not release.wait(2):
            raise TimeoutError('synthetic observer failed to drain')
        return VerifiedExecutorObservation(identity, 'a' * 64, str(uuid4()),
                                           started, started + state.lifetime,
                                           int(time.time() * 1000),
                                           started_wall_ms + int(state.lifetime * 1000))
    with PilotOwner(database, allow_ephemeral_test_storage=True) as owner:
        gate = StartupReconciliation(owner=owner, database=database, target_id=TARGET,
            manifest_digest=DIGEST, executor=identity, lifetime_seconds=60,
            current_executor_identity=observe, async_admission=True)
        evidence = 'Synthetic reconciled inventory, not independent host evidence.'
        report = OperatorReconciliation(gate.challenge, owner.epoch, TARGET, DIGEST, identity,
            'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(), True, True, True)
        def audit(record):
            state.records.append(record)
            return True
        broker = SessionAcceptanceBroker(gate=gate, owner=owner, target_id=TARGET,
            manifest_digest=DIGEST, executor=identity, report=report, evidence=evidence,
            operator_actor=ACTOR, audit=audit, lifetime_seconds=30)
        value = SimpleNamespace(broker=broker, gate=gate, owner=owner, state=state,
                                entered=entered, release=release)
        try:
            yield value
        finally:
            release.set()
            broker.close()


def answer(case, **changes):
    kwargs = dict(actor=ACTOR, request_id=case.broker._id, expected_revision=1, action='accept')
    kwargs.update(changes)
    return kwargs


async def entered(case):
    async with asyncio.timeout(1):
        while not case.entered.is_set():
            await asyncio.sleep(.001)


def test_snapshot_and_decline_do_not_observe(case):
    assert case.state.calls == 0
    assert case.broker.snapshot(actor=ACTOR)['state'] == 'pending'
    assert case.state.calls == 0
    assert case.broker.decide(**answer(case, action='decline'))['state'] == 'declined'
    assert case.state.calls == 0
    assert [r.outcome for r in case.state.records] == ['decline', 'declined']


def test_scope_free_legacy_accept_denied_before_audit_or_observation(case):
    with pytest.raises(SessionAcceptanceError):
        case.broker.decide(**answer(case))
    assert case.state.calls == 0 and not case.state.records
    assert case.gate.admission_state == 'closed'


async def test_accept_observes_off_loop_then_atomic_same_loop_audit_and_open(case):
    main = threading.get_ident()
    order = []
    def audit(record):
        assert asyncio.get_running_loop().is_running()
        assert threading.get_ident() == main
        order.append((record.event, case.gate.admission_state))
        case.state.records.append(record)
        asyncio.get_running_loop().call_soon(order.append, ('yielded', 'invalid'))
        return True
    case.broker._audit = audit
    result = await case.broker.decide_async(**answer(case))
    assert result['state'] == 'accepted' and not result['execution_authorization']
    assert order == [('intent', 'pending'), ('result', 'accepted')]
    assert case.state.calls == 1 and len(case.state.threads) == 1
    assert case.state.threads[0] != main
    assert case.broker.snapshot(actor=ACTOR)['state'] == 'accepted'
    assert case.state.calls == 1


async def test_slow_result_audit_cannot_return_late_accepted_proof(case, monkeypatch):
    from engineering_hub import startup_reconciliation as gate_module
    def audit(record):
        case.state.records.append(record)
        if record.event == 'result':
            # Deterministic elapsed callback time, only on the gate's clock;
            # do not patch the event loop clock or depend on a 30ms CI window.
            after_proof = time.monotonic() + 2
            monkeypatch.setattr(gate_module, 'time', SimpleNamespace(
                monotonic=lambda: after_proof, time=time.time))
        return True
    case.broker._audit = audit
    with pytest.raises(SessionAcceptanceError):
        await case.broker.decide_async(**answer(case))
    assert case.gate.admission_state == 'closed'
    assert [(item.event, item.outcome) for item in case.state.records] == [
        ('intent', 'accept'), ('result', 'accepted')]
    assert case.broker._state == 'closed'


@pytest.mark.parametrize('jump_after', ['intent', 'result'])
async def test_forward_wall_jump_after_local_audit_cannot_open_or_return_success(case, monkeypatch, jump_after):
    from engineering_hub import startup_reconciliation as gate_module
    def audit(record):
        case.state.records.append(record)
        if record.event == jump_after:
            # The monotonic permission is still live; only the independently
            # retained absolute proof deadline has expired after local work.
            after_expiry = time.time() + 10
            monkeypatch.setattr(gate_module, 'time', SimpleNamespace(
                monotonic=time.monotonic, time=lambda: after_expiry))
        return True
    case.broker._audit = audit
    with pytest.raises(SessionAcceptanceError):
        await case.broker.decide_async(**answer(case))
    assert case.gate.admission_state == 'closed' and case.broker._state == 'closed'
    assert case.state.calls == 1
    assert [(item.event, item.outcome) for item in case.state.records] == [
        ('intent', 'accept'), ('result', 'failed' if jump_after == 'intent' else 'accepted')]


async def test_blocked_observation_yields_and_concurrent_preparation_bounded(case):
    case.state.block = True
    task = asyncio.create_task(case.broker.decide_async(**answer(case)))
    await entered(case)
    ticks = []
    for _ in range(4):
        await asyncio.sleep(.005)
        ticks.append(case.broker.snapshot(actor=ACTOR)['state'])
    with pytest.raises(SessionAcceptanceError):
        await case.broker.decide_async(**answer(case))
    assert ticks == ['pending'] * 4 and case.state.calls == 1
    assert not case.state.records and not task.done()
    case.release.set()
    assert (await task)['state'] == 'accepted'


@pytest.mark.parametrize('change', ['actor', 'revision', 'report', 'deadline', 'owner', 'close'])
async def test_changes_during_preparation_veto_acceptance(case, change):
    case.state.block = True
    task = asyncio.create_task(case.broker.decide_async(**answer(case)))
    await entered(case)
    if change == 'actor':
        case.broker._operator = 'human:999'
    elif change == 'revision':
        case.broker._revision += 1
    elif change == 'report':
        case.broker._report = replace(case.broker._report, report_digest='sha256:' + 'b' * 64)
    elif change == 'deadline':
        case.broker._deadline = time.monotonic() - 1
    elif change == 'owner':
        case.owner.close()
    else:
        case.broker.close()
    case.release.set()
    with pytest.raises(SessionAcceptanceError):
        await task
    assert not case.state.records and case.gate.admission_state == 'closed'


async def test_repeated_cancellation_drains_observation_before_sealing(case):
    case.state.block = True
    task = asyncio.create_task(case.broker.decide_async(**answer(case)))
    await entered(case)
    task.cancel()
    await asyncio.sleep(.01)
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done() and not case.state.records
    case.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert case.gate.admission_state == 'closed' and not case.state.records
    assert case.broker._preparing is None


async def test_slow_intent_exhausts_scope_without_opening(case):
    case.state.lifetime = .02
    def audit(record):
        case.state.records.append(record)
        if record.event == 'intent':
            time.sleep(.04)
        return True
    case.broker._audit = audit
    with pytest.raises(SessionAcceptanceError):
        await case.broker.decide_async(**answer(case))
    assert [r.outcome for r in case.state.records] == ['accept', 'failed']
    assert case.gate.admission_state == 'closed' and not case.gate._accepted


async def test_decline_during_preparation_vetoes_inflight_accept(case):
    case.state.block = True
    task = asyncio.create_task(case.broker.decide_async(**answer(case)))
    await entered(case)
    assert (await case.broker.decide_async(**answer(case, action='decline')))['state'] == 'declined'
    case.release.set()
    with pytest.raises(SessionAcceptanceError):
        await task
    assert [r.outcome for r in case.state.records] == ['decline', 'declined']
    assert not case.gate._accepted


async def test_unrelated_consumed_operator_scope_cannot_open_broker(case):
    subject = replace(case.broker._accept_subject(ACTOR), subject_digest='sha256:' + '0' * 64)
    prepared = await case.gate.prepare_admission(owner=case.owner, subject=subject,
                                               deadline=time.monotonic() + 1)
    with pytest.raises(SessionAcceptanceError):
        with case.gate.admission_scope(prepared, owner=case.owner, subject=subject):
            case.broker.decide(**answer(case))
    assert case.state.calls == 1 and not case.state.records
    assert not case.gate._accepted


async def test_result_audit_failure_seals_future_admission_not_claimed_rollback(case):
    states = []
    def audit(record):
        states.append(case.gate.admission_state)
        if record.event == 'result':
            raise OSError('private synthetic audit failure')
        return True
    case.broker._audit = audit
    with pytest.raises(SessionAcceptanceError):
        await case.broker.decide_async(**answer(case))
    assert states == ['pending', 'accepted']
    assert case.gate._accepted and case.gate.admission_state == 'closed'


@pytest.mark.parametrize('change', ['actor', 'revision'])
async def test_intent_callback_revocation_or_cas_change_vetoes_opening(case, change):
    def audit(record):
        case.state.records.append(record)
        if record.event == 'intent':
            if change == 'actor':
                case.broker._operator = 'human:999'
            else:
                case.broker._revision += 1
        return True
    case.broker._audit = audit
    with pytest.raises(SessionAcceptanceError):
        await case.broker.decide_async(**answer(case))
    assert [r.outcome for r in case.state.records] == ['accept', 'failed']
    assert not case.gate._accepted and case.gate.admission_state == 'closed'


def test_real_human_route_uses_async_entrypoint_and_exact_auth_boundary(browser):
    client, login, case = browser
    path = '/api/hub/v1/staging/session-reconciliation'
    headers = login()
    response = client.get(path, headers=headers)
    assert response.status_code == 200 and case.state.calls == 0
    snapshot = response.json()
    payload = dict(request_id=snapshot['request_id'], expected_revision=1, action='accept')
    assert client.post(path, json=payload, headers={**headers, 'X-Hub-CSRF': 'wrong'}).status_code == 403
    assert client.post(path, json=payload, headers={**headers, 'Authorization': 'Bearer test-codex'}).status_code == 403
    other = login('thomas-fixture', 'thomas@example.test')
    assert client.post(path, json=payload, headers=other).status_code == 403
    assert case.state.calls == 0 and not case.state.records
    headers = login()
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 200 and response.json()['state'] == 'accepted'
    assert case.state.calls == 1
    assert response.headers['cache-control'] == 'no-store'
    assert client.post(path, json=payload, headers=headers).status_code == 409
