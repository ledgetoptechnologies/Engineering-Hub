"""Real Linux owner and real browser auth; synthetic report/observer/audit only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub import cloudflare_access as access
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub import session_acceptance as subject
from engineering_hub.session_acceptance import (SessionAcceptanceBroker,
    SessionAcceptanceError, attach_session_acceptance_routes)
from engineering_hub.startup_reconciliation import (ExecutorSessionIdentity,
    OperatorReconciliation, StartupReconciliation, StartupReconciliationError)

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='real Linux owner required')
DIGEST = 'sha256:' + 'a' * 64
TARGET = 'engineering-hub:staging'
OPERATOR = 'human:123'
BASE = '/api/hub/v1/staging/session-reconciliation'


@pytest.fixture
def case(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    database = private / 'hub.sqlite3'
    identity = ExecutorSessionIdentity(str(uuid4()), DIGEST)
    observed = [identity]
    records = []
    def audit(record):
        records.append(record)
        return True  # Synthetic durable acknowledgement, NOT durability proof.
    with PilotOwner(database, allow_ephemeral_test_storage=True) as owner:
        def make(**changes):
            gate = StartupReconciliation(owner=owner, database=database, target_id=TARGET,
                manifest_digest=DIGEST, executor=identity, lifetime_seconds=60,
                current_executor_identity=lambda: observed[0])
            evidence = 'Synthetic inventory reviewed; current writers manually excluded.'
            report = OperatorReconciliation(gate.challenge, owner.epoch, TARGET, DIGEST,
                identity, 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(), True, True, True)
            kwargs = dict(gate=gate, owner=owner, target_id=TARGET, manifest_digest=DIGEST,
                executor=identity, report=report, evidence=evidence, operator_actor=OPERATOR,
                audit=audit, lifetime_seconds=30)
            kwargs.update(changes)
            broker = SessionAcceptanceBroker(**kwargs)
            return SimpleNamespace(broker=broker, gate=gate, report=report, kwargs=kwargs)
        value = make()
        yield SimpleNamespace(**vars(value), owner=owner, identity=identity,
                              observed=observed, records=records, make=make)
        value.broker.close()


def decide(c, **changes):
    snap = c.broker.snapshot(actor=OPERATOR)
    kwargs = dict(actor=OPERATOR, request_id=snap['request_id'],
                  expected_revision=snap['revision'], action='accept')
    kwargs.update(changes)
    return c.broker.decide(**kwargs)


def permitted(c):
    c.gate.assert_current_permitted(owner=c.owner, target_id=TARGET, manifest_digest=DIGEST)


def test_pending_then_accept_audits_before_open_and_is_not_task_grant(case):
    c = case
    with pytest.raises(StartupReconciliationError):
        permitted(c)
    result = decide(c)
    assert result['state'] == 'accepted' and result['execution_authorization'] is False
    assert [(r.event, r.outcome) for r in c.records] == [('intent', 'accept'), ('result', 'accepted')]
    assert all(r.execution_authorization is False for r in c.records)
    permitted(c)


def test_decline_seals_without_opening(case):
    assert decide(case, action='decline')['state'] == 'declined'
    assert [(r.event, r.outcome) for r in case.records] == [('intent', 'decline'), ('result', 'declined')]
    with pytest.raises(StartupReconciliationError):
        permitted(case)


@pytest.mark.parametrize('changes', [dict(expected_revision=True), dict(expected_revision=9),
    dict(request_id='f' * 64), dict(request_id='é' * 64), dict(action='enable'),
    dict(actor='human:999'), dict(actor='human:é')])
def test_wrong_input_does_not_consume_real_pending_request(case, changes):
    with pytest.raises(SessionAcceptanceError):
        decide(case, **changes)
    assert case.records == []
    assert decide(case)['state'] == 'accepted'


def test_concurrent_replay_exactly_one_intent_and_result(case):
    snap = case.broker.snapshot(actor=OPERATOR)
    def attempt(_):
        try:
            return case.broker.decide(actor=OPERATOR, request_id=snap['request_id'],
                expected_revision=1, action='accept')['state']
        except SessionAcceptanceError:
            return 'denied'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['accepted', 'denied']
    assert len(case.records) == 2
    permitted(case)


def test_previous_request_cannot_open_fresh_process_local_broker(case):
    old = case.broker.snapshot(actor=OPERATOR)
    fresh = case.make()
    try:
        with pytest.raises(SessionAcceptanceError):
            fresh.broker.decide(actor=OPERATOR, request_id=old['request_id'],
                                expected_revision=1, action='accept')
        assert fresh.gate.admission_state == 'pending'
    finally:
        fresh.broker.close()


@pytest.mark.parametrize('elapsed', [31, -1, float('nan'), float('inf')])
def test_expiry_clock_regression_and_identity_loss_seal(case, monkeypatch, elapsed):
    snap = case.broker.snapshot(actor=OPERATOR)
    now = case.broker._last_time
    monkeypatch.setattr(subject, 'time', SimpleNamespace(monotonic=lambda: now + elapsed))
    with pytest.raises(SessionAcceptanceError):
        case.broker.decide(actor=OPERATOR, request_id=snap['request_id'], expected_revision=1, action='accept')
    monkeypatch.setattr(subject, 'time', SimpleNamespace(monotonic=lambda: now))
    with pytest.raises(SessionAcceptanceError):
        case.broker.snapshot(actor=OPERATOR)
    assert case.gate.admission_state == 'closed'


def test_owner_and_executor_changes_cannot_be_accepted(case):
    case.observed[0] = ExecutorSessionIdentity(str(uuid4()), DIGEST)
    with pytest.raises(SessionAcceptanceError):
        case.broker.snapshot(actor=OPERATOR)
    assert case.gate.admission_state == 'closed'


def test_owner_close_cannot_be_accepted(case):
    snap = case.broker.snapshot(actor=OPERATOR)
    case.owner.close()
    with pytest.raises(SessionAcceptanceError):
        case.broker.decide(actor=OPERATOR, request_id=snap['request_id'],
                          expected_revision=1, action='accept')
    assert case.gate.admission_state == 'closed' and case.records == []


def test_fork_denies_before_inherited_mutex_and_leaves_parent_owner(case):
    decide(case)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        class ForbiddenLock:
            def __enter__(self):
                os.write(write_fd, b'lock-touched')
                raise AssertionError
        case.broker._lock = ForbiddenLock()
        try:
            case.broker.snapshot(actor=OPERATOR)
        except SessionAcceptanceError:
            os.write(write_fd, b'closed')
            os._exit(0)
        os._exit(2)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 64) == b'closed'
        assert os.waitpid(pid, 0)[1] == 0
    finally:
        os.close(read_fd)
    permitted(case)


@pytest.mark.parametrize('stage', ['intent', 'result'])
def test_audit_failure_revokes_no_atomic_rollback_claim(case, stage):
    c = case
    accepted_during_result = []
    def audit(record):
        c.records.append(record)
        if record.event == stage:
            if stage == 'result':
                # Direct gate permits are NOT protected by the broker mutex.
                permitted(c)
                accepted_during_result.append(True)
            raise RuntimeError('private audit failure detail')
        assert c.gate.admission_state == 'pending'
        return True
    c.broker._audit = audit
    with pytest.raises(SessionAcceptanceError, match='^staging session acceptance unavailable$'):
        decide(c)
    with pytest.raises(StartupReconciliationError):
        permitted(c)
    assert accepted_during_result == ([True] if stage == 'result' else [])
    assert len(c.records) == (2 if stage == 'result' else 1)


def test_audit_reentry_fails_closed(case):
    c = case
    def reenter(_):
        with pytest.raises(SessionAcceptanceError):
            c.broker.snapshot(actor=OPERATOR)
        return True
    c.broker._audit = reenter
    with pytest.raises(SessionAcceptanceError):
        decide(c)
    assert c.gate.admission_state == 'closed'


@pytest.mark.parametrize('stage', ['intent', 'result'])
@pytest.mark.parametrize('nested_id', ['current', 'foreign'])
def test_audit_cannot_hide_nested_decide_failure_and_continue(case, stage, nested_id):
    c = case
    snap = c.broker.snapshot(actor=OPERATOR)
    outcomes = []
    def audit(record):
        outcomes.append(record.outcome)
        if record.event == stage:
            # A malicious/misbehaving callback swallows the nested denial and
            # claims its own durable work succeeded. Closure must survive it.
            with pytest.raises(SessionAcceptanceError):
                c.broker.decide(actor=OPERATOR,
                    request_id=snap['request_id'] if nested_id == 'current' else 'f' * 64,
                    expected_revision=1, action='accept')
        return True
    c.broker._audit = audit
    with pytest.raises(SessionAcceptanceError):
        decide(c)
    assert outcomes == (['accept'] if stage == 'intent' else ['accept', 'accepted'])
    assert c.gate.admission_state == 'closed'
    with pytest.raises(StartupReconciliationError):
        permitted(c)


def test_misdeclared_async_audit_ack_is_closed_without_execution(case):
    ran, values = [], []
    async def result():
        ran.append(True)
        return True
    def audit(_):
        value = result()
        values.append(value)
        return value
    case.broker._audit = audit
    with pytest.raises(SessionAcceptanceError):
        decide(case)
    assert ran == [] and len(values) == 1 and values[0].cr_frame is None
    assert case.gate.admission_state == 'closed'


def test_sqlite_audit_commits_intent_before_opening(case, tmp_path):
    path = tmp_path / 'acceptance-audit.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE audit(sequence INTEGER PRIMARY KEY, record TEXT NOT NULL)')
    def audit(record):
        if record.event == 'intent':
            assert case.gate.admission_state == 'pending'
        else:
            assert case.gate.admission_state == 'accepted'
            with sqlite3.connect(path) as db:
                assert json.loads(db.execute('SELECT record FROM audit').fetchone()[0])['event'] == 'intent'
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('INSERT INTO audit(record) VALUES(?)', (json.dumps(asdict(record)),))
        return True
    case.broker._audit = audit
    decide(case)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM audit').fetchone()[0] == 2


@pytest.mark.parametrize('seconds', [True, 0, 3601, 1.0])
def test_invalid_constructor_lifetime_rejected(case, seconds):
    with pytest.raises(SessionAcceptanceError):
        case.make(lifetime_seconds=seconds)


def test_report_digest_and_async_audit_are_not_accepted(case):
    with pytest.raises(SessionAcceptanceError):
        case.make(evidence='Different evidence')
    async def audit(_):
        return True
    with pytest.raises(SessionAcceptanceError):
        case.make(audit=audit)


@pytest.fixture
def browser(case, tmp_path, monkeypatch):
    issuer, audience = 'https://fixture-team.cloudflareaccess.com', 'fixture-session-acceptance'
    emails = ('beau@example.test', 'thomas@example.test')
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    key.update(kid='fixture-key', alg='RS256', use='sig')
    config = AuthConfig('https://hub.example', human_auth_mode='cloudflare_access',
        cloudflare_issuer=issuer, cloudflare_audience=audience, cloudflare_allowed_emails=emails)
    verifier = access.CloudflareAccessVerifier(config.cloudflare_config,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'keys': [key]})))
    monkeypatch.setattr(access, 'CloudflareAccessVerifier', lambda _: verifier)
    app = create_app(Settings(tmp_path / 'browser.sqlite3',
        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'), config))
    operator = 'human:cf:' + hashlib.sha256('\0'.join((issuer, audience, 'beau-fixture')).encode()).hexdigest()
    case.broker._operator = operator  # Trusted synthetic enrollment, not first-login selection.
    attach_session_acceptance_routes(app, broker=case.broker, config=config, sessions=app.state.sessions)
    def assertion(sub='beau-fixture', email=emails[0]):
        now = int(time.time())
        return jwt.encode(dict(iss=issuer, aud=[audience], sub=sub, email=email,
            type='app', iat=now-1, exp=now+300), private, algorithm='RS256', headers={'kid': 'fixture-key'})
    with TestClient(app, base_url=config.origin) as client:
        def login(sub='beau-fixture', email=emails[0]):
            client.cookies.clear()
            headers = {'Cf-Access-Jwt-Assertion': assertion(sub, email)}
            assert client.get('/api/hub/v1/auth/login', headers=headers, follow_redirects=False).status_code == 303
            me = client.get('/api/hub/v1/auth/me', headers=headers).json()
            return {**headers, 'Origin': config.origin, 'X-Hub-CSRF': me['csrf_token']}
        case.browser_app = app
        yield client, login, case


def test_actual_cloudflare_human_csrf_and_separately_pinned_operator(browser):
    client, login, c = browser
    assert client.get(BASE).status_code == 401
    headers = login()
    response = client.get(BASE, headers=headers)
    assert response.status_code == 200
    snap = response.json()
    assert response.headers['cache-control'] == 'no-store'
    body = dict(request_id=snap['request_id'], expected_revision=1, action='accept')
    assert client.post(BASE, json=body, headers={**headers, 'Authorization': 'Bearer test-codex'}).status_code == 403
    assert client.post(BASE, json=body, headers={**headers, 'Origin': 'https://wrong.example'}).status_code == 403
    assert client.post(BASE, json=body, headers={**headers, 'X-Hub-CSRF': 'wrong'}).status_code == 403
    other = login('thomas-fixture', 'thomas@example.test')
    assert client.get('/api/hub/v1/auth/me', headers=other).json()['authenticated'] is True
    assert client.get(BASE, headers=other).status_code == 403
    assert client.post(BASE, json=body, headers=other).status_code == 403
    assert c.records == []
    headers = login()
    result = client.post(BASE, json=body, headers=headers)
    assert result.json()['state'] == 'accepted' and result.headers['cache-control'] == 'no-store'
    assert client.post(BASE, json=body, headers=headers).status_code == 409
    assert len(c.records) == 2


def test_route_keeps_open_and_result_audit_on_same_event_loop(browser):
    client, login, c = browser
    headers = login()
    snap = client.get(BASE, headers=headers).json()
    loop_ids, later_states = [], []
    def audit(record):
        loop = asyncio.get_running_loop()  # Would fail in a synchronous route's threadpool.
        loop_ids.append(id(loop))
        if record.event == 'result':
            loop.call_soon(lambda: later_states.append(c.gate.admission_state))
            raise RuntimeError('synthetic result audit failure')
        return True
    c.broker._audit = audit
    response = client.post(BASE, json=dict(request_id=snap['request_id'],
        expected_revision=1, action='accept'), headers=headers)
    assert response.status_code == 503
    assert len(loop_ids) == 2 and len(set(loop_ids)) == 1
    assert later_states == ['closed']


@pytest.mark.parametrize('extra', ['owner_epoch', 'executor', 'report', 'evidence', 'operator_actor', 'writers_excluded'])
def test_request_cannot_supply_authority_or_echo_private_values(browser, extra):
    client, login, c = browser
    headers = login()
    snap = client.get(BASE, headers=headers).json()
    response = client.post(BASE, json=dict(request_id=snap['request_id'], expected_revision=1,
        action='accept', **{extra: 'PRIVATE-supplied-value'}), headers=headers)
    assert response.status_code == 422 and 'PRIVATE' not in response.text
    assert c.records == [] and c.gate.admission_state == 'pending'


def test_bounded_duplicate_malformed_json_and_strict_revision(browser):
    client, login, c = browser
    headers = login()
    snap = client.get(BASE, headers=headers).json()
    headers['Content-Type'] = 'application/json'
    assert client.post(BASE, content='x' * 2049, headers=headers).status_code == 413
    assert client.post(BASE, content='{"action":"accept","action":"decline"}', headers=headers).status_code == 422
    response = client.post(BASE, json=dict(request_id=snap['request_id'], expected_revision=True,
        action='accept'), headers=headers)
    assert response.status_code == 422 and c.records == []


def test_oversized_chunk_is_rejected_before_copying(browser, monkeypatch):
    client, login, c = browser
    headers = login()
    copied = []
    class ObservedBuffer(bytearray):
        def extend(self, value):
            copied.append(len(value))
            return super().extend(value)
    monkeypatch.setattr(subject, 'bytearray', ObservedBuffer, raising=False)
    response = client.post(BASE, content=b'PRIVATE' * 10000,
        headers={**headers, 'Content-Type': 'application/json'})
    assert response.status_code == 413 and 'PRIVATE' not in response.text
    assert copied == [] and c.records == [] and c.gate.admission_state == 'pending'


@pytest.mark.asyncio
async def test_direct_asgi_slow_body_has_bounded_sanitized_timeout(browser, monkeypatch):
    client, login, c = browser
    headers = login()
    monkeypatch.setattr(subject, '_BODY_READ_TIMEOUT_SECONDS', 0.02)
    request_headers = {**headers, 'Host': 'hub.example', 'Content-Type': 'application/json',
        'Cookie': '; '.join(f'{key}={value}' for key, value in client.cookies.items())}
    scope = dict(type='http', asgi={'version': '3.0', 'spec_version': '2.3'},
        http_version='1.1', method='POST', scheme='https', path=BASE,
        raw_path=BASE.encode(), query_string=b'', root_path='',
        headers=[(key.lower().encode(), value.encode()) for key, value in request_headers.items()],
        server=('hub.example', 443), client=('127.0.0.1', 12345))
    sent, cancelled = [], []
    async def receive():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return {'type': 'http.request', 'body': b'PRIVATE', 'more_body': True}
    async def send(message):
        sent.append(message)
    async with asyncio.timeout(1):
        await c.browser_app(scope, receive, send)
    start = next(message for message in sent if message['type'] == 'http.response.start')
    body = b''.join(message.get('body', b'') for message in sent if message['type'] == 'http.response.body')
    assert start['status'] == 408 and b'PRIVATE' not in body
    assert json.loads(body)['detail']['code'] == 'session_acceptance_unavailable'
    assert cancelled and c.records == [] and c.gate.admission_state == 'pending'
