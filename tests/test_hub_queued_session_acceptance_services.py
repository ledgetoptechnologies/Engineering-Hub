"""Explicit private/unified service hook, real Linux sockets/auth, fake effects."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import sys
from uuid import uuid4

import httpx
import pytest

from engineering_hub.private_browser_socket import create_private_browser_client
from engineering_hub.private_gateway import PublicBrowserGateway
from engineering_hub.private_gateway import browser_route_allowed
from engineering_hub.queued_session_acceptance import attach_queued_session_acceptance
from engineering_hub.queued_pilot_service import QueuedPilotServiceError
from engineering_hub.startup_reconciliation import (ExecutorSessionIdentity,
    OperatorReconciliation, StartupReconciliation)
from tests.test_hub_queued_pilot_service import (setup as private_service,
    options as private_options, start as private_start, agent_client,
    review_task, human_task)
from tests.test_hub_unified_queued_pilot_service import (rig as unified_service,
    options as unified_options, start as unified_start, connections)
from tests.test_hub_gateway_queued_pilot import checked, ORIGIN
from tests.test_hub_queued_pilot import ACTION
from tests.test_hub_container_pilot import COMMIT, IMAGE

pytestmark = [pytest.mark.asyncio,
    pytest.mark.skipif(sys.platform != 'linux', reason='real Linux owner/socket service')]
BASE = '/api/hub/v1'
SESSION = BASE + '/staging/session-reconciliation'


def factories(s):
    s.gates, s.brokers, s.acceptance_records = [], [], []
    identity = ExecutorSessionIdentity(str(uuid4()), 'sha256:' + 'e' * 64)
    def reconcile(owner, target):
        gate = StartupReconciliation(owner=owner, database=s.settings.database,
            target_id=target.target_id, manifest_digest=target.manifest_digest,
            executor=identity, lifetime_seconds=3600, current_executor_identity=lambda: identity)
        s.gates.append(gate)
        return gate
    def accept(core, owner, repo, target, gate):
        auth = s.settings.auth_config
        operator = 'human:cf:' + hashlib.sha256('\0'.join((auth.cloudflare_issuer,
            auth.cloudflare_audience, 'synthetic-human')).encode()).hexdigest()
        evidence = 'Synthetic service fixture reconciliation; no real inventory proof.'
        report = OperatorReconciliation(gate.challenge, owner.epoch, target.target_id,
            target.manifest_digest, identity, 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(),
            True, True, True)
        def audit(record):
            s.acceptance_records.append(record)
            return True
        broker = attach_queued_session_acceptance(core, owner=owner, repository=repo,
            target=target, gate=gate, operator_actor=operator, audit=audit,
            report=report, evidence=evidence)
        original = broker.close
        def close():
            s.events.append('broker-close')
            original()
        broker.close = close
        s.brokers.append(broker)
        return broker
    return reconcile, accept


@asynccontextmanager
async def clients(mode, address, s):
    if mode == 'unified':
        async with connections(address, s) as pair:
            yield pair
    else:
        async with create_private_browser_client(s.endpoint) as private:
            gateway = PublicBrowserGateway(private)
            async with (agent_client(address) as agent,
                httpx.AsyncClient(transport=httpx.ASGITransport(gateway), base_url=ORIGIN,
                    follow_redirects=False, headers={'Cf-Access-Jwt-Assertion': s.assertion}) as browser):
                yield agent, browser


def state(request, mode):
    return request.getfixturevalue('private_service' if mode == 'private' else 'unified_service')


@pytest.mark.parametrize('mode', ['private', 'unified'])
async def test_real_service_human_accepts_same_gate_and_queue_keeps_review(mode, request):
    s = state(request, mode)
    reconcile, accept = factories(s)
    start = private_start if mode == 'private' else unified_start
    stop, serving, address = await start(s, reconciliation_factory=reconcile,
                                         session_acceptance_factory=accept)
    try:
        assert s.gates[0].admission_state == 'pending'
        async with clients(mode, address, s) as (agent, browser):
            task = await human_task(s, browser, 'session-service-' + mode)
            assert (await agent.post(SESSION, json={})).status_code == 403
            snap = checked(await browser.get(SESSION))
            body = dict(request_id=snap['request_id'], expected_revision=1, action='accept')
            assert (await browser.post(SESSION, json=body,
                headers={'X-Hub-CSRF': 'wrong'})).status_code == 403
            assert s.gates[0].admission_state == 'pending'
            result = checked(await browser.post(SESSION, json=body))
            assert result['state'] == 'accepted' and result['execution_authorization'] is False
            submission = dict(request_id='service-job-' + mode, action_key=ACTION,
                environment='staging', target=dict(task_id=task['id'], source_commit=COMMIT,
                                                  artifact_digest=IMAGE))
            assert (await agent.post(BASE + '/work/jobs', json=submission)).status_code >= 400
            task = await review_task(agent, task)
            job = checked(await agent.post(BASE + '/work/jobs', json=submission), 201)
            async with asyncio.timeout(8):
                while checked(await browser.get(BASE + '/work/jobs/' + job['id']))['state'] != 'succeeded':
                    await asyncio.sleep(0.001)
            assert len(s.requests) == 2
            assert s.core.state.queued_pilot_runtime.bridge.reconciliation is s.gates[0]
    finally:
        stop.set()
        await asyncio.wait_for(serving, 8)
    assert s.events.count('broker-close') == 1
    assert s.events.index('broker-close') < s.events.index('client-close') < s.events.index('owner-close')
    if mode == 'unified':
        assert s.events.index('bus-stop') < s.events.index('broker-close')
    assert s.gates[0].admission_state == 'closed'


@pytest.mark.parametrize('mode', ['private', 'unified'])
@pytest.mark.parametrize('kind', ['none', 'true', 'coroutine', 'current_task'])
async def test_service_rejects_invalid_and_async_broker_factory_result(mode, kind, request):
    s = state(request, mode)
    reconcile, _ = factories(s)
    ran, values = [], []
    async def result():
        ran.append(True)
        return None
    def factory(*_):
        value = (None if kind == 'none' else True if kind == 'true' else
                 result() if kind == 'coroutine' else asyncio.current_task())
        values.append(value)
        return value
    kwargs = dict(reconciliation_factory=reconcile, session_acceptance_factory=factory)
    with pytest.raises(QueuedPilotServiceError):
        if mode == 'private':
            await s.service.serve_owned_queued_pilot(s.settings, s.manifest, s.client_factory,
                                                   **private_options(s, **kwargs))
        else:
            from engineering_hub.unified_queued_pilot_service import serve_owned_unified_queued_pilot
            await serve_owned_unified_queued_pilot(s.settings, s.manifest, s.client_factory,
                                                   **unified_options(s, **kwargs))
    assert not ran and len(s.clients) == 1 and s.clients[0].close_count == 1
    if kind == 'coroutine':
        assert values[0].cr_frame is None
    assert asyncio.current_task().cancelling() == 0
    assert s.gates[0].admission_state == 'closed'


async def test_returned_local_factory_task_cleanup_drains_before_owner(private_service):
    s = private_service
    reconcile, _ = factories(s)
    cleaning, release = asyncio.Event(), asyncio.Event()
    async def work():
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
            s.owners[0].assert_active(s.settings.database)
    # A task cancelled before it enters cannot exercise asynchronous cleanup.
    # Use an existing entered task returned by the trusted callback instead.
    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    serving = asyncio.create_task(s.service.serve_owned_queued_pilot(s.settings, s.manifest,
        s.client_factory, **private_options(s, reconciliation_factory=reconcile,
                                            session_acceptance_factory=lambda *_: task)))
    try:
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not serving.done() and s.owners[0].close_count == 0
        release.set()
        with pytest.raises(QueuedPilotServiceError):
            await asyncio.wait_for(serving, 5)
        assert task.done() and s.owners[0].close_count == 1
    finally:
        release.set()
        await asyncio.gather(serving, task, return_exceptions=True)


async def test_gateway_allowlist_exposes_only_exact_browser_session_surface():
    for method in ('GET', 'POST'):
        assert browser_route_allowed(method, SESSION)
        for suffix in ('/', '/accept', '/report', '/create', '-other'):
            assert not browser_route_allowed(method, SESSION + suffix)
    for method in ('PUT', 'PATCH', 'DELETE', 'OPTIONS'):
        assert not browser_route_allowed(method, SESSION)


@pytest.mark.parametrize('mode', ['private', 'unified'])
async def test_broker_cleanup_waits_for_accepted_service_effect_and_core_drain(mode, request):
    s = state(request, mode)
    reconcile, accept = factories(s)
    entered, release = asyncio.Event(), asyncio.Event()
    cancelled = []
    async def block(_):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    s.block = block
    start = private_start if mode == 'private' else unified_start
    stop, serving, address = await start(s, reconciliation_factory=reconcile,
                                         session_acceptance_factory=accept)
    owner = s.owners[0] if mode == 'private' else s.owner
    try:
        async with clients(mode, address, s) as (agent, browser):
            task = await human_task(s, browser, 'acceptance-drain-' + mode)
            snap = checked(await browser.get(SESSION))
            checked(await browser.post(SESSION, json=dict(request_id=snap['request_id'],
                expected_revision=1, action='accept')))
            task = await review_task(agent, task)
            job = checked(await agent.post(BASE + '/work/jobs', json=dict(
                request_id='acceptance-drain-job-' + mode, action_key=ACTION,
                environment='staging', target=dict(task_id=task['id'], source_commit=COMMIT,
                                                  artifact_digest=IMAGE))), 201)
            await asyncio.wait_for(entered.wait(), 8)
            serving.cancel()
            async with asyncio.timeout(5):
                while s.gates[0].admission_state != 'closed':
                    await asyncio.sleep(0.001)
            serving.cancel()
            assert not serving.done() and 'broker-close' not in s.events
            assert s.clients[0].close_count == owner.close_count == 0
            owner.assert_active(s.settings.database)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(serving, 8)
            assert not cancelled and len(s.requests) == 1
            assert s.core.state.work_queue.get_job(job['id'])['state'] == 'held'
    finally:
        release.set()
        stop.set()
        await asyncio.gather(serving, return_exceptions=True)
    assert s.events.count('broker-close') == 1
    assert s.events.index('broker-close') < s.events.index('client-close') < s.events.index('owner-close')
    if mode == 'unified':
        assert s.events.index('bus-stop') < s.events.index('broker-close')
