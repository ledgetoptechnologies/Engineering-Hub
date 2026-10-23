"""Async effect boundaries with real owned SQLite, synthetic executor identity.

No host deployment or independent Engine/identity attestation is proved here.
"""
import asyncio
import hashlib
import secrets
import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4

import pytest

from engineering_hub.current_executor_observation import VerifiedExecutorObservation
from engineering_hub.session_acceptance import SessionAcceptanceBroker
from engineering_hub.startup_reconciliation import ExecutorSessionIdentity, OperatorReconciliation, StartupReconciliation
from engineering_hub.repository import HubError
from tests.test_hub_queued_pilot import rig, submit, machine_job

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='real Linux owned SQLite')


@pytest.fixture
def case(rig):
    state = SimpleNamespace(calls=0, block_at=None, entered=threading.Event(),
                            release=threading.Event(), delay=0, threads=[])
    target = rig.bridge.target
    identity = ExecutorSessionIdentity(str(uuid4()), 'sha256:' + 'e' * 64)
    def observe():
        state.calls += 1
        state.threads.append(threading.get_ident())
        started = time.monotonic()
        if state.calls == state.block_at:
            state.entered.set()
            if not state.release.wait(3):
                raise TimeoutError('Synthetic observer timeout')
        if state.delay:
            time.sleep(state.delay)
        return VerifiedExecutorObservation(identity, secrets.token_hex(32), str(uuid4()),
                                           started, started + 5)
    gate = StartupReconciliation(owner=rig.owner, database=rig.repo.path,
        target_id=target.target_id, manifest_digest=target.manifest_digest, executor=identity,
        lifetime_seconds=60, current_executor_identity=observe, async_admission=True)
    evidence = 'Synthetic host inventory; not real staging acceptance.'
    report = OperatorReconciliation(gate.challenge, rig.owner.epoch, target.target_id,
        target.manifest_digest, identity, 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(),
        True, True, True)
    broker = SessionAcceptanceBroker(gate=gate, owner=rig.owner, target_id=target.target_id,
        manifest_digest=target.manifest_digest, executor=identity, report=report, evidence=evidence,
        operator_actor='human:123', audit=lambda _: True, lifetime_seconds=60)
    rig.bridge.reconciliation.close()
    rig.bridge.reconciliation = gate
    rig.gate, rig.broker, rig.observation = gate, broker, state
    try:
        yield rig
    finally:
        state.release.set()
        broker.close()


async def accept(case):
    return await case.broker.decide_async(actor='human:123', request_id=case.broker._id,
        expected_revision=1, action='accept')


async def entered(case):
    async with asyncio.timeout(2):
        while not case.observation.entered.is_set():
            await asyncio.sleep(.001)


async def test_pending_metadata_submits_but_cannot_claim_or_start_network(case):
    _, job = submit(case)
    assert case.queue.claim(worker=case.bridge.worker) is None
    with pytest.raises(HubError):
        await case.bridge.run_once()
    assert case.observation.calls == 0
    assert case.client.submissions == [] and machine_job(case) is None
    assert case.queue.get_job(job['id'])['state'] == 'queued'


@pytest.mark.parametrize('rollback', [False, True])
async def test_success_and_healthy_rollback_each_use_fresh_phase_and_release(case, rollback):
    if rollback:
        case.client.outcomes['smoke_container'] = 'failed'
    await accept(case)
    _, job = submit(case)
    assert case.queue.claim(worker=case.bridge.worker) is None  # No consumed scope.
    result = await case.bridge.run_once()
    assert result['id'] == job['id']
    assert result['state'] == ('failed' if rollback else 'succeeded')
    assert len(case.client.submissions) == (4 if rollback else 2)
    assert case.observation.calls == (7 if rollback else 5)
    assert machine_job(case) is None
    assert all(thread != threading.get_ident() for thread in case.observation.threads)


async def test_changed_review_during_claim_observation_cannot_claim_another_job(case):
    await accept(case)
    task, original = submit(case)
    submit(case, 'next')
    case.observation.block_at = 2
    running = asyncio.create_task(case.bridge.run_once())
    await entered(case)
    current = case.repo.get_task(task['id'])
    case.repo.set_candidate('codex', task['id'], current['revision'], 'f' * 40)
    case.observation.release.set()
    assert await running is None
    assert case.queue.get_job(original['id'])['state'] == 'queued'
    assert not case.client.submissions and machine_job(case) is None


@pytest.mark.parametrize('change', ['close', 'cancel', 'review', 'lease'])
async def test_veto_during_command_preparation_sends_no_post_and_keeps_reservation(case, change):
    await accept(case)
    task, job = submit(case)
    case.observation.block_at = 3
    running = asyncio.create_task(case.bridge.run_once())
    await entered(case)
    if change == 'close':
        case.gate.close()
    elif change == 'cancel':
        running.cancel()
        await asyncio.sleep(.005)
        running.cancel()
        assert not running.done()
    elif change == 'review':
        # Normal candidate edits correctly reject an active run. Inject a
        # corrupted ledger review to exercise the final independent recheck.
        import json
        with case.repo._transaction() as db:
            current = case.repo._get(db, task['id'], 'task')
            current['review']['verdict'] = 'reject'
            db.execute('UPDATE hub_entities SET data=? WHERE id=?',
                       (json.dumps(current), task['id']))
    else:
        case.clock[0] += 601
    case.observation.release.set()
    if change == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await running
    else:
        assert (await running)['state'] in {'held', 'claimed'}
    assert not case.client.submissions
    assert machine_job(case) == job['id']


async def test_direct_cancellation_drains_admitted_remote_phase_and_retains_evidence(case):
    await accept(case)
    _, job = submit(case)
    entered_remote, release_remote = asyncio.Event(), asyncio.Event()
    original = case.client.submit_command
    async def submit_command(command):
        entered_remote.set()
        await release_remote.wait()
        return await original(command)
    case.client.submit_command = submit_command
    running = asyncio.create_task(case.bridge.run_once())
    await asyncio.wait_for(entered_remote.wait(), 2)
    running.cancel()
    await asyncio.sleep(.005)
    running.cancel()
    case.gate.close()
    assert not running.done()
    release_remote.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    with case.repo._connection() as db:
        row = db.execute('SELECT state,response_json FROM hub_container_pilot_commands').fetchone()
        assert row['state'] == 'succeeded' and row['response_json']
    assert len(case.client.submissions) == 1 and machine_job(case) == job['id']


async def test_slow_valid_observations_leave_shared_event_loop_responsive(case):
    case.observation.delay = .1
    await accept(case)
    submit(case)
    running = asyncio.create_task(case.bridge.run_once())
    gaps = []
    last = time.monotonic()
    while not running.done():
        await asyncio.sleep(.005)
        now = time.monotonic()
        gaps.append(now - last)
        last = now
        assert case.broker.snapshot(actor='human:123')['state'] == 'accepted'
    assert (await running)['state'] == 'succeeded'
    assert len(gaps) >= 10 and max(gaps) < .25


async def test_release_proof_expiry_during_local_validation_keeps_capacity_held(case, monkeypatch):
    from engineering_hub import startup_reconciliation as gate_module
    await accept(case)
    _, job = submit(case)
    original = case.bridge._proof
    def proof(db, intent):
        result = original(db, intent)
        scope = case.gate._active_scope
        if scope is not None and scope.subject.kind == 'reconcile':
            # Simulated elapsed proof work; only the gate's clock is advanced.
            after_proof = scope.deadline + .001
            monkeypatch.setattr(gate_module, 'time', SimpleNamespace(monotonic=lambda: after_proof))
        return result
    monkeypatch.setattr(case.bridge, '_proof', proof)
    assert (await case.bridge.run_once())['state'] == 'held'
    assert machine_job(case) == job['id'] and len(case.client.submissions) == 2
    assert case.gate.admission_state == 'closed'


async def test_real_unified_mcp_ping_and_overview_remain_responsive_during_observation(case, tmp_path, monkeypatch):
    """Real in-process Hub/bus routes, synthetic delayed identity; not NAS latency."""
    import httpx
    from agent_message_bus.app import create_app as create_bus
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from tests.test_hub_unified_mcp import AUTH, BASE, headers, result, security
    monkeypatch.setenv('AGENT_BUS_ALLOWED_HOSTS', '127.0.0.1:*')
    bus = create_bus(db_path=tmp_path / 'messages.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=AUTH, push_events_operational=False,
        transport_security_settings=security())
    core = create_app(Settings(Path(case.repo.path), AUTH, AuthConfig('https://hub.test',
        human_auth_mode='cloudflare_access', cloudflare_issuer='https://fixture.cloudflareaccess.com',
        cloudflare_audience='fixture', cloudflare_allowed_emails=('beau@example.test',)),
        agent_allowed_hosts=('127.0.0.1:8000',)), repository=case.repo,
        work_queue=case.queue, bus_app=bus)
    case.observation.delay = 1.0
    await accept(case)
    submit(case)
    async with core.router.lifespan_context(core):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core), base_url=BASE) as client:
            initialized = await client.post('/mcp', headers=headers(), json={'jsonrpc': '2.0', 'id': 1,
                'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'isolated-latency-check', 'version': '1'}}})
            result(initialized)
            session = initialized.headers['Mcp-Session-Id']
            notified = await client.post('/mcp', headers=headers(session=session), json={
                'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            assert notified.status_code == 202
            running = asyncio.create_task(case.bridge.run_once())
            latencies = []
            while not running.done():
                started = time.monotonic()
                response = await client.post('/mcp', headers=headers(session=session), json={
                    'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
                assert result(response) == {}
                overview = await client.get('/api/hub/v1/overview', headers=headers())
                assert overview.status_code == 200
                latencies.append(time.monotonic() - started)
                await asyncio.sleep(.005)
            assert (await running)['state'] == 'succeeded'
            assert len(latencies) >= 5 and max(latencies) < .25
            assert (await client.delete('/mcp', headers=headers(session=session))).status_code == 200
