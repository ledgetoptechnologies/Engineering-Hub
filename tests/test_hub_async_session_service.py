"""Actual unified listeners/manual audit/queue lifecycle; synthetic host identity.

This is not Docker, TLS enrollment, live staging or writer-exclusion proof.
"""
import asyncio
import hashlib
import secrets
import threading
import time
from uuid import uuid4

import pytest

from engineering_hub.current_executor_observation import VerifiedExecutorObservation
from engineering_hub.queued_session_acceptance import attach_queued_session_acceptance
from engineering_hub.session_acceptance_audit import OwnedSessionAcceptanceAudit
from engineering_hub.startup_reconciliation import (
    ExecutorSessionIdentity, OperatorReconciliation, StartupReconciliation)
from tests.test_hub_unified_queued_pilot_service import (
    rig, start, connections, submit, until, closed, BASE, checked)


@pytest.mark.asyncio
@pytest.mark.parametrize('rollback', [False, True], ids=['healthy', 'healthy-rollback'])
async def test_pending_manual_acceptance_wakes_existing_job_without_new_work(rig, rollback):
    observations = []
    entered = threading.Event()
    identity = ExecutorSessionIdentity(str(uuid4()), 'sha256:' + 'e' * 64)
    def observe():
        started = time.monotonic()
        observations.append(threading.get_ident())
        entered.set()
        time.sleep(.15)
        return VerifiedExecutorObservation(identity, secrets.token_hex(32), str(uuid4()),
                                           started, started + 5)

    def gate_factory(owner, target):
        gate = StartupReconciliation(owner=owner, database=rig.settings.database,
            target_id=target.target_id, manifest_digest=target.manifest_digest,
            executor=identity, lifetime_seconds=60, current_executor_identity=observe,
            async_admission=True)
        rig.manual_gate = gate
        return gate

    def acceptance_factory(core, owner, repository, target, gate):
        evidence = 'Synthetic inventory and writer exclusion; not live host proof.'
        report = OperatorReconciliation(gate.challenge, owner.epoch, target.target_id,
            target.manifest_digest, identity,
            'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(), True, True, True)
        config = rig.settings.auth_config
        actor = 'human:cf:' + hashlib.sha256('\0'.join((config.cloudflare_issuer,
            config.cloudflare_audience, 'synthetic-human')).encode()).hexdigest()
        audit = OwnedSessionAcceptanceAudit(repository)
        rig.manual_audit = audit
        return attach_queued_session_acceptance(core, owner=owner, repository=repository,
            target=target, gate=gate, operator_actor=actor, audit=audit,
            report=report, evidence=evidence)

    if rollback:
        rig.host.outcomes['smoke_container'] = 'failed'
    stop, serving, addresses = await start(rig, reconciliation_factory=gate_factory,
                                         session_acceptance_factory=acceptance_factory)
    try:
        assert observations == []
        async with connections(addresses, rig) as (agent, browser):
            headers = {'Accept': 'application/json, text/event-stream'}
            initialized = await agent.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2025-06-18', 'capabilities': {},
                    'clientInfo': {'name': 'manual-session-test', 'version': '1'}}})
            assert initialized.status_code == 200
            headers.update({'Mcp-Session-Id': initialized.headers['mcp-session-id'],
                            'MCP-Protocol-Version': '2025-06-18'})
            assert (await agent.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'method': 'notifications/initialized'})).status_code == 202
            _, job = await submit(agent, browser, 'pending-manual-job')
            runtime = rig.core.state.queued_pilot_runtime
            with rig.core.state.repository._connection() as db:
                pending_head = db.execute('SELECT MAX(sequence) FROM hub_outbox').fetchone()[0]
            await until(lambda: runtime.feed.broker.cursor >= pending_head)
            # Let the native feed's original task/job events finish while the
            # gate is pending. Acceptance must not depend on a lucky older wake.
            await asyncio.sleep(.1)
            await until(lambda: not runtime._wakeup.is_set() and runtime._active is None)
            assert observations == [] and rig.requests == []
            assert checked(await agent.get(BASE + '/work/jobs/' + job['id']))['state'] == 'queued'
            path = BASE + '/staging/session-reconciliation'
            pending = checked(await browser.get(path))
            assert observations == []
            accepting = asyncio.create_task(browser.post(path, json={
                'request_id': pending['request_id'], 'expected_revision': pending['revision'],
                'action': 'accept'}))
            try:
                await until(entered.is_set)
                started = time.monotonic()
                ping, overview = await asyncio.gather(
                    agent.post('/mcp', headers=headers, json={
                        'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}),
                    browser.get(BASE + '/overview'))
                assert ping.status_code == overview.status_code == 200
                assert time.monotonic() - started < .25
                accepted = checked(await accepting)
                assert accepted['state'] == 'accepted'
                with rig.core.state.repository._connection() as db:
                    audit_events = list(db.execute(
                        'SELECT event FROM hub_session_acceptance_audit ORDER BY rowid'))
                assert [row['event'] for row in audit_events] == ['intent', 'result']
            finally:
                if not accepting.done():
                    accepting.cancel()
                await asyncio.gather(accepting, return_exceptions=True)
            # No synthetic _wakeup.set(), queue submission or unrelated Hub
            # mutation is allowed between acceptance and this original result.
            await until(lambda: runtime.status['last_job'] is not None)
            assert runtime.status['last_job'] == {
                'id': job['id'], 'state': 'failed' if rollback else 'succeeded'}
            assert len(rig.requests) == (4 if rollback else 2)
            assert len(observations) == (7 if rollback else 5)
            assert all(value != threading.get_ident() for value in observations)
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
        rig.manual_audit.close()
    closed(rig)
