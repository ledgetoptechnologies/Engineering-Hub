"""Exact queued gate + real Linux owner/auth/events; synthetic executor facts."""
import asyncio
from dataclasses import replace
import hashlib
import json
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub import cloudflare_access as access
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.queued_session_acceptance import (QueuedSessionAcceptanceError,
    attach_queued_session_acceptance)
from engineering_hub.repository import HubError
from engineering_hub.session_acceptance import SessionAcceptanceError
from engineering_hub.startup_reconciliation import OperatorReconciliation, StartupReconciliation
from engineering_hub.work_queue import WorkTarget
from tests.test_hub_queued_manual_session import composition as queued_composition
from tests.test_hub_queued_pilot import ACTION, MACHINE
from tests.test_hub_container_pilot import COMMIT, IMAGE, reviewed_task

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='real Linux owner/native events')
BASE = '/api/hub/v1/staging/session-reconciliation'
ORIGIN = 'https://hub.example'


@pytest.fixture
def composed(queued_composition, monkeypatch):
    s = queued_composition
    issuer, audience = 'https://fixture-team.cloudflareaccess.com', 'fixture-queued-session'
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    key.update(kid='fixture-key', alg='RS256', use='sig')
    config = AuthConfig(ORIGIN, human_auth_mode='cloudflare_access',
        cloudflare_issuer=issuer, cloudflare_audience=audience,
        cloudflare_allowed_emails=('beau@example.test', 'thomas@example.test'))
    verifier = access.CloudflareAccessVerifier(config.cloudflare_config,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'keys': [key]})))
    monkeypatch.setattr(access, 'CloudflareAccessVerifier', lambda _: verifier)
    feed = NativeHubEventFeed(s.repo.path)
    runtime = QueuedPilotRuntime(s.bridge, feed)
    core = create_app(Settings(s.repo.path,
        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'), config,
        staging_machine_id=MACHINE), repository=s.repo,
        event_feed=feed, work_queue=s.queue, queued_pilot_runtime=runtime)
    operator = 'human:cf:' + hashlib.sha256('\0'.join((issuer, audience, 'beau-fixture')).encode()).hexdigest()
    evidence = 'Synthetic manually reviewed inventory/exclusion report; NOT live host proof.'
    report = OperatorReconciliation(s.gate.challenge, s.owner.epoch, s.target.target_id,
        s.target.manifest_digest, s.identity, 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(),
        True, True, True)
    records = []
    def audit(record):
        records.append(record)
        return True  # Synthetic durability acknowledgement only.
    kwargs = dict(owner=s.owner, repository=s.repo, target=s.target, gate=s.gate,
        operator_actor=operator, audit=audit, report=report, evidence=evidence)
    def attach(**changes):
        return attach_queued_session_acceptance(core, **(kwargs | changes))
    def assertion(sub='beau-fixture', email='beau@example.test'):
        now = int(time.time())
        return jwt.encode(dict(iss=issuer, aud=[audience], sub=sub, email=email,
            type='app', iat=now-1, exp=now+300), private, algorithm='RS256', headers={'kid': 'fixture-key'})
    yield SimpleNamespace(s=s, core=core, runtime=runtime, feed=feed, kwargs=kwargs,
                          attach=attach, assertion=assertion, operator=operator, records=records)
    broker = getattr(core.state, 'session_acceptance', None)
    if broker is not None:
        broker.close()


async def login(browser, fixture, *, other=False):
    browser.cookies.clear()
    browser.headers.clear()
    browser.headers['Cf-Access-Jwt-Assertion'] = fixture.assertion(
        'thomas-fixture' if other else 'beau-fixture',
        'thomas@example.test' if other else 'beau@example.test')
    assert (await browser.get('/api/hub/v1/auth/login')).status_code == 303
    me = (await browser.get('/api/hub/v1/auth/me')).json()
    assert me['authenticated'] is True
    browser.headers.update({'Origin': ORIGIN, 'X-Hub-CSRF': me['csrf_token']})


def submit(s, *, reviewed=True):
    task = (reviewed_task(s.repo, str(uuid4())) if reviewed else
            s.repo.create_task('human:1234', 'engineering-hub', 'Unreviewed', str(uuid4())))
    return s.queue.submit(actor='codex', request_id=str(uuid4()), action_key=ACTION,
        environment='staging', target=WorkTarget(task['id'], COMMIT, IMAGE))


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


def test_same_gate_attachment_is_pending_and_duplicate_attach_is_rejected(composed):
    f = composed
    broker = f.attach()
    broker.assert_binding(gate=f.s.gate, owner=f.s.owner,
        target_id=f.s.target.target_id, manifest_digest=f.s.target.manifest_digest)
    assert f.s.gate.admission_state == 'pending'
    assert f.core.state.session_acceptance is broker
    assert len([r for r in f.core.routes if getattr(r, 'path', None) == BASE]) == 2
    with pytest.raises(QueuedSessionAcceptanceError):
        f.attach()
    assert f.s.gate.admission_state == 'pending' and f.records == []


@pytest.mark.parametrize('field', ['owner', 'repository', 'target', 'gate'])
def test_mismatched_capability_never_installs_routes(composed, field):
    f = composed
    if field == 'target':
        value = replace(f.s.target)  # Equal data is not the same trusted object.
    elif field == 'gate':
        value = StartupReconciliation(owner=f.s.owner, database=f.s.repo.path,
            target_id=f.s.target.target_id, manifest_digest=f.s.target.manifest_digest,
            executor=f.s.identity, lifetime_seconds=60, current_executor_identity=lambda: f.s.identity)
    else:
        value = object()
    try:
        with pytest.raises(QueuedSessionAcceptanceError):
            f.attach(**{field: value})
        assert not any(getattr(r, 'path', None) == BASE for r in f.core.routes)
        assert getattr(f.core.state, 'session_acceptance', None) is None
        assert f.s.gate.admission_state == 'pending'
    finally:
        if field == 'gate':
            value.close()


def test_no_audit_callback_denies_attachment_without_opening(composed):
    with pytest.raises(QueuedSessionAcceptanceError):
        composed.attach(audit=None)
    assert composed.s.gate.admission_state == 'closed'
    assert not any(getattr(r, 'path', None) == BASE for r in composed.core.routes)
    composed.s.owner.assert_active(composed.s.repo.path)


def test_public_broker_binding_does_not_accept_equal_but_different_gate(composed):
    f = composed
    broker = f.attach()
    other = StartupReconciliation(owner=f.s.owner, database=f.s.repo.path,
        target_id=f.s.target.target_id, manifest_digest=f.s.target.manifest_digest,
        executor=f.s.identity, lifetime_seconds=60, current_executor_identity=lambda: f.s.identity)
    try:
        with pytest.raises(SessionAcceptanceError):
            broker.assert_binding(gate=other, owner=f.s.owner,
                target_id=f.s.target.target_id, manifest_digest=f.s.target.manifest_digest)
        assert f.s.gate.admission_state == 'closed' and other.admission_state == 'pending'
        f.s.owner.assert_active(f.s.repo.path)
    finally:
        other.close()


@pytest.mark.asyncio
async def test_human_accepts_exact_gate_then_native_reviewed_queue_runs(composed):
    f, s = composed, composed.s
    broker = f.attach()
    async with f.core.router.lifespan_context(f.core):
        assert f.runtime.status['state'] == 'startup_reconciliation_required'
        with pytest.raises(HubError):
            submit(s)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.core), base_url=ORIGIN,
                                     follow_redirects=False) as browser:
            await login(browser, f)
            snap = (await browser.get(BASE)).json()
            body = dict(request_id=snap['request_id'], expected_revision=1, action='accept')
            assert (await browser.post(BASE, json=body,
                headers={'Authorization': 'Bearer test-codex'})).status_code == 403
            assert (await browser.post(BASE, json=body,
                headers={'X-Hub-CSRF': 'wrong'})).status_code == 403
            await login(browser, f, other=True)
            assert (await browser.get(BASE)).status_code == 403
            assert (await browser.post(BASE, json=body)).status_code == 403
            assert s.gate.admission_state == 'pending' and f.records == []
            await login(browser, f)
            response = await browser.post(BASE, json=body)
            assert response.status_code == 200 and response.json()['execution_authorization'] is False
            broker.assert_binding(gate=s.gate, owner=s.owner,
                target_id=s.target.target_id, manifest_digest=s.target.manifest_digest)
            assert s.bridge.reconciliation is s.gate and s.gate.admission_state == 'accepted'
            with pytest.raises(HubError):
                submit(s, reviewed=False)  # Session acceptance is not candidate/task approval.
            assert s.client.submissions == []
            job = submit(s)
            await until(lambda: s.queue.get_job(job['id'])['state'] == 'succeeded')
            assert len(s.client.submissions) == 2
            assert [(r.event, r.outcome) for r in f.records] == [('intent', 'accept'), ('result', 'accepted')]
    assert s.gate.admission_state == 'closed'
    s.owner.assert_active(s.repo.path)  # Caller lifecycle, not broker, owns this capability.


@pytest.mark.asyncio
async def test_attachment_after_core_has_served_is_rejected(composed):
    f = composed
    async with f.core.router.lifespan_context(f.core):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.core), base_url=ORIGIN) as browser:
            await browser.get('/health')
        with pytest.raises(QueuedSessionAcceptanceError):
            f.attach()
        assert not any(getattr(r, 'path', None) == BASE for r in f.core.routes)


@pytest.mark.asyncio
async def test_close_revokes_next_phase_but_accepted_effect_drains_under_owner(composed):
    f, s = composed, composed.s
    f.attach()
    entered, release = asyncio.Event(), asyncio.Event()
    ready, stop = asyncio.Event(), asyncio.Event()
    async def serve():
        # One task owns both entry/exit of SDK/AnyIO lifespan scopes.
        async with f.core.router.lifespan_context(f.core):
            ready.set()
            await stop.wait()
            f.runtime.close_admission()
    serving = asyncio.create_task(serve())
    await asyncio.wait_for(ready.wait(), 5)
    original = s.client.submit_command
    async def held(command):
        entered.set()
        await release.wait()
        s.owner.assert_active(s.repo.path)
        return await original(command)
    s.client.submit_command = held
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.core), base_url=ORIGIN,
                                     follow_redirects=False) as browser:
            await login(browser, f)
            snap = (await browser.get(BASE)).json()
            response = await browser.post(BASE, json=dict(request_id=snap['request_id'],
                expected_revision=1, action='accept'))
            assert response.status_code == 200
            job = submit(s)
            await asyncio.wait_for(entered.wait(), 5)
            stop.set()
            await until(lambda: s.gate.admission_state == 'closed')
            assert not serving.done()
            s.owner.assert_active(s.repo.path)
            release.set()
            await asyncio.wait_for(serving, 5)
            assert len(s.client.submissions) == 1  # No later smoke after revocation.
            assert s.queue.get_job(job['id'])['state'] == 'held'
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(serving, 5)
    s.owner.assert_active(s.repo.path)
