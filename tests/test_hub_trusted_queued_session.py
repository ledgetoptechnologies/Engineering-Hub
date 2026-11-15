"""Fixed composition with real SQLite owner/audit and synthetic host facts only."""
from contextlib import contextmanager
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import threading
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import httpx

pytest.importorskip('fcntl', reason='Linux protected ownership')

from agent_message_bus.auth import AuthSettings
from engineering_hub import trusted_queued_session as module
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubRepository
from engineering_hub.startup_reconciliation import StartupReconciliationError
from engineering_hub.current_executor_observation import VerifiedExecutorObservation
from engineering_hub.work_queue import WorkAction, WorkQueue
from tests.test_hub_queued_pilot import DistinctContainerClient, ACTION, MACHINE
from tests.test_hub_unified_pilot_startup import profile_document, encode, installed
from engineering_hub.unified_pilot_startup import parse_profile


def document():
    profile = parse_profile(encode(profile_document()))
    manifest = profile.manifest
    now = int(time.time() * 1000)
    evidence = 'Synthetic independently reviewed inventory and writer exclusion; NOT real host proof.'
    result = {'schema': module.SCHEMA_SCOPED,
        'scope': {'actors': sorted(profile.actors), 'task_ids': sorted(profile.task_ids)}, 'release': {
        'issuer_id': 'synthetic-issuer', 'public_key_hex': 'a' * 64,
        'implementation_commit': manifest.driver.implementation_commit,
        'runnable_digest': 'sha256:' + '1' * 64, 'platform_manifest_digest': 'sha256:' + '2' * 64,
        'image_config_id': 'sha256:' + '3' * 64,
        'platform': manifest.runner_binding.platform.model_dump(mode='json'),
        'launch_profile_digest': 'sha256:' + '4' * 64}, 'launch': {
            'launch_id': str(uuid4()), 'boot_id': str(uuid4()), 'authenticated_host_pid': 42,
            'process_start_ticks': 123, 'container_id': 'b' * 64,
            'engine_id': manifest.runner_binding.daemon_identity, 'engine_pid': 43,
            'engine_start_ticks': 124, 'approval_expires_at_ms': now + 300_000},
        'operator_actor': 'human:cf:' + 'c' * 64, 'report': {
            'target_id': manifest.target_id, 'manifest_digest': manifest.digest(), 'machine_id': 'staging-pc',
            'source': 'synthetic-readonly-inventory', 'observed_at_ms': now - 1000,
            'expires_at_ms': now + 60_000, 'evidence': evidence,
            'evidence_sha256': 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(),
            'attestations': {'inventory_reconciled': True, 'writers_excluded': True,
                             'unresolved_effects_absent': True}}}
    result['report']['executor'] = dict(result['launch'], image_config_id=result['release']['image_config_id'])
    return result


def test_closed_parser_roundtrip_does_not_start_observation(monkeypatch):
    monkeypatch.setattr(module, 'CurrentExecutorObserver', lambda **_: pytest.fail('Observer construction'))
    value = module.parse_enrollment(encode(document()))
    assert module.parse_enrollment(module.enrollment_bytes(value)) == value
    assert value.writers_excluded is True


@pytest.mark.parametrize('scope', [
    {'actors': [], 'task_ids': ['00000000-0000-4000-8000-000000000001']},
    {'actors': ['codex', 'codex'], 'task_ids': ['00000000-0000-4000-8000-000000000001']},
    {'actors': ['hermes', 'codex'], 'task_ids': ['00000000-0000-4000-8000-000000000001']},
    {'actors': ['codex'], 'task_ids': []},
    {'actors': ['codex'], 'task_ids': ['invalid']},
    {'actors': ['codex'], 'task_ids': ['00000000-0000-4000-8000-000000000001'] * 2},
])
def test_session_scope_is_closed_canonical_and_nonempty(scope):
    data = document(); data['scope'] = scope
    with pytest.raises(module.TrustedSessionError):
        module.parse_enrollment(encode(data))


def test_assembled_session_evidence_enforces_exact_utf8_byte_boundary():
    data = document()
    data['scope']['task_ids'] = [str(UUID(int=i)) for i in range(1, 33)]
    data['report']['source'] = 's' * 128
    data['launch']['engine_id'] = data['report']['executor']['engine_id'] = 'e' * 128
    data['report']['evidence'] = 'x' * 6000
    data['report']['evidence_sha256'] = 'sha256:' + hashlib.sha256(data['report']['evidence'].encode()).hexdigest()
    enrollment = module.parse_enrollment(encode(data))
    with pytest.raises(module.TrustedSessionError):
        module._session_evidence(enrollment)
    base = replace(enrollment, evidence='é')
    available = module.MAX_ACCEPTANCE_EVIDENCE_BYTES - len(module._session_evidence(base).encode('utf-8'))
    exact = replace(base, evidence='é' + 'x' * available)
    assert len(module._session_evidence(exact).encode('utf-8')) == module.MAX_ACCEPTANCE_EVIDENCE_BYTES
    with pytest.raises(module.TrustedSessionError):
        module._session_evidence(replace(exact, evidence=exact.evidence + 'é'))


def test_unified_prevalidation_rejects_assembled_evidence_above_broker_cap(monkeypatch):
    profile = parse_profile(encode(profile_document()))
    enrollment = module.parse_enrollment(encode(document()))
    assert module.validate_unified_session(profile, enrollment)[0] == enrollment
    monkeypatch.setattr(module, 'MAX_ACCEPTANCE_EVIDENCE_BYTES', 1)
    with pytest.raises(module.TrustedSessionError):
        module.validate_unified_session(profile, enrollment)


@pytest.mark.parametrize('change', ['legacy', 'actor', 'task'])
def test_session_scope_cannot_expand_profile_or_use_legacy(tmp_path, dependencies, change):
    data = document()
    if change == 'legacy':
        data['schema'] = module.SCHEMA; del data['scope']
    elif change == 'actor': data['scope']['actors'] = ['hermes']
    else: data['scope']['task_ids'] = ['00000000-0000-4000-8000-000000000002']
    with pytest.raises(module.TrustedSessionError):
        construct(tmp_path, data)
    assert dependencies.events == []


def test_session_scope_veto_and_expiry(tmp_path, dependencies, monkeypatch):
    composition = construct(tmp_path)
    try:
        task = document()['scope']['task_ids'][0]
        assert composition.scope_allows('codex', task) is True
        assert composition.scope_allows('hermes', task) is False
        assert composition.scope_allows('codex', '00000000-0000-4000-8000-000000000002') is False
        monkeypatch.setattr(module, '_clock', lambda: (composition._deadline + 1, composition._last_wall))
        assert composition.scope_allows('codex', task) is False
    finally:
        composition.close()


def test_fresh_clock_and_watermarks_are_serialized_across_threads(tmp_path, dependencies, monkeypatch):
    composition = construct(tmp_path)
    entered, release, second_inside = threading.Event(), threading.Event(), threading.Event()
    base_mono, base_wall = composition._last_mono, composition._last_wall
    calls = []
    def clock():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        else:
            second_inside.set()
        return base_mono + len(calls), base_wall + len(calls)
    monkeypatch.setattr(module, '_clock', clock)
    results, errors = [], []
    def check():
        try:
            results.append(composition._fresh())
        except BaseException as error:
            errors.append(error)
    first = threading.Thread(target=check); second = threading.Thread(target=check)
    try:
        first.start(); assert entered.wait(3)
        second.start()
        assert not second_inside.wait(.1)
        release.set()
        first.join(3); second.join(3)
        assert not first.is_alive() and not second.is_alive() and not errors
        assert sorted(results) == [base_mono + 1, base_mono + 2]
        assert composition._last_mono == base_mono + 2 and composition._last_wall == base_wall + 2
    finally:
        release.set(); first.join(3); second.join(3); composition.close()


def test_forked_process_rejects_before_inherited_lock(tmp_path, dependencies, monkeypatch):
    composition = construct(tmp_path)
    class NeverAcquire:
        def __enter__(self): pytest.fail('child acquired inherited lock')
        def __exit__(self, *_): pass
    composition._clock_lock = NeverAcquire()
    monkeypatch.setattr(module.os, 'getpid', lambda: composition._pid + 1)
    try:
        with pytest.raises(module.TrustedSessionError):
            composition._fresh()
    finally:
        monkeypatch.undo()
        composition.close()


@pytest.mark.asyncio
async def test_restart_with_narrower_session_retains_omitted_held_task(tmp_path, dependencies):
    """Synthetic ledger restart; neither host fencing nor execution approval."""
    from tests.test_hub_container_pilot import COMMIT, IMAGE, PRINCIPAL, pilot_manifest, reviewed_task
    from tests.test_hub_queued_pilot import ACTION as ACTION_KEY, MACHINE as MACHINE_ID, machine_job
    from tests.manual_session_fixtures import synthetic_workflow_reconciliation
    from engineering_hub.work_queue import WorkTarget
    from engineering_hub.repository import HubError
    root = tmp_path / 'restart'; root.mkdir(mode=0o700)
    path = root / 'hub.sqlite3'
    owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
    task = reviewed_task(repo, 'restart-scope')
    other = reviewed_task(repo, 'outside-session')['id']
    profile = parse_profile(encode(profile_document()))
    profile = replace(profile, task_ids=frozenset({task['id'], other}))
    def session(task_id):
        data = document(); data['scope']['task_ids'] = [task_id]
        return module.TrustedQueuedSessionComposition(profile=profile, database=path,
            enrollment=module.parse_enrollment(encode(data)), ca_pem=b'captured-ca', credential=b'captured-token')
    first, second = session(task['id']), None
    def build(owner, repo, composition):
        client = DistinctContainerClient(); client.outcomes['deploy_container'] = 'held'
        workflow = ContainerPilotWorkflow(repo, client,
            [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
        def policy(actor, _action, _environment, target):
            return composition.scope_allows(actor, target.task_id)
        queue = WorkQueue(repo, {ACTION_KEY: WorkAction(MACHINE_ID, project='engineering-hub')},
                          target_verifier=policy)
        bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION_KEY, machine_id=MACHINE_ID,
                                      reconciliation=synthetic_workflow_reconciliation(workflow))
        return client, queue, bridge
    try:
        client, queue, bridge = build(owner, repo, first)
        with pytest.raises(HubError, match='work_target_not_verified'):
            queue.submit(actor='codex', request_id='outside-session', action_key=ACTION_KEY,
                         environment='staging', target=WorkTarget(other, COMMIT, IMAGE))
        with repo._connection() as db:
            assert db.execute('SELECT count(*) FROM hub_work_jobs').fetchone()[0] == 0
        assert client.submissions == []
        job = queue.submit(actor='codex', request_id='restart-scope', action_key=ACTION_KEY,
                           environment='staging', target=WorkTarget(task['id'], COMMIT, IMAGE))
        assert (await bridge.run_once())['state'] == 'held'
        assert len(client.submissions) == 1
        first.close(); owner.close()
        owner = PilotOwner(path, allow_ephemeral_test_storage=True)
        repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
        second = session(other)
        client, queue, bridge = build(owner, repo, second)
        with pytest.raises(HubError, match='queued_pilot_review_changed'):
            await bridge.recover(job['id'])
        assert queue.get_job(job['id'])['state'] == 'held'
        assert machine_job(SimpleNamespace(repo=repo)) == job['id']
        assert client.submissions == [] and client.inspections == []
    finally:
        if second is not None: second.close()
        first.close(); owner.close()


@pytest.mark.parametrize('change', ['extra', 'accepted-bit', 'module', 'false', 'missing-claim',
    'integer-claim', 'wrong-evidence-hash', 'oversized', 'nul', 'bad-actor', 'github-actor',
    'wrong-target', 'wrong-machine', 'float-time', 'long-window', 'duplicate', 'bad-key', 'extra-launch',
    'old-launch-report', 'old-release-report'])
def test_invalid_enrollment_has_no_defaults_or_code_selection(change):
    data = document()
    if change in {'extra', 'accepted-bit', 'module'}:
        data[change] = 'sensitive invalid input'
    elif change == 'false': data['report']['attestations']['writers_excluded'] = False
    elif change == 'missing-claim': del data['report']['attestations']['writers_excluded']
    elif change == 'integer-claim': data['report']['attestations']['writers_excluded'] = 1
    elif change == 'wrong-evidence-hash': data['report']['evidence_sha256'] = 'sha256:' + '0' * 64
    elif change == 'oversized': data['report']['evidence'] = 'x' * 6001
    elif change == 'nul': data['report']['evidence'] += '\x00'
    elif change == 'bad-actor': data['operator_actor'] = 'claude'
    elif change == 'github-actor': data['operator_actor'] = 'human:123'
    elif change == 'wrong-target': data['report']['target_id'] = 'production'
    elif change == 'wrong-machine': data['report']['machine_id'] = 'production'
    elif change == 'float-time': data['report']['observed_at_ms'] += .5
    elif change == 'long-window': data['report']['expires_at_ms'] += module.MAX_REPORT_LIFETIME_MS
    elif change == 'bad-key': data['release']['public_key_hex'] = 'bad'
    elif change == 'extra-launch': data['launch']['accepted'] = True
    elif change == 'old-launch-report': data['launch']['launch_id'] = str(uuid4())
    elif change == 'old-release-report': data['release']['image_config_id'] = 'sha256:' + '0' * 64
    raw = encode(data)
    if change == 'duplicate': raw = raw[:-1] + b',"schema":"duplicate"}'
    with pytest.raises(module.TrustedSessionError) as error:
        module.parse_enrollment(raw)
    assert str(error.value) == 'trusted manual staging session unavailable'


def synthetic_dependencies(monkeypatch):
    events, observers, captured = [], [], []
    def transport(origin, **kwargs):
        captured.append((origin, kwargs))
        events.append('transport')
        return object()
    monkeypatch.setattr(module.CurrentExecutorHTTPSTransport, 'from_captured_material',
                        transport, raising=False)
    class Observer:
        def __init__(self, *, release, launch, transport):
            self.identity = module.ExecutorSessionIdentity(launch.launch_id, release.image_config_id)
            self.closed = False
            observers.append(self)
            events.append('observer')
        def observe_verified(self):
            assert not self.closed
            events.append('observe')
            started = time.monotonic()
            return VerifiedExecutorObservation(self.identity, os.urandom(32).hex(), str(uuid4()),
                started, started + 5, verified_wall_ms=int(time.time() * 1000),
                expires_wall_ms=int(time.time() * 1000) + 5000)
        def __call__(self):
            pytest.fail('Installed async composition must request verified proof, not bare identity')
        def close(self):
            self.closed = True
            events.append('observer-close')
    monkeypatch.setattr(module, 'CurrentExecutorObserver', Observer)
    return SimpleNamespace(events=events, observers=observers, captured=captured)


@pytest.fixture
def dependencies(monkeypatch):
    return synthetic_dependencies(monkeypatch)


def construct(tmp_path, data=None):
    profile = parse_profile(encode(profile_document()))
    return module.TrustedQueuedSessionComposition(profile=profile, database=tmp_path / 'hub.sqlite3',
        enrollment=module.parse_enrollment(encode(document() if data is None else data)),
        ca_pem=b'captured-ca', credential=b'captured-executor-credential')


@pytest.mark.parametrize('change', ['stale', 'future', 'expired', 'manifest', 'driver', 'engine'])
def test_no_transport_before_fresh_independent_profile_binding(tmp_path, dependencies, change):
    data = document()
    if change == 'stale':
        data['report']['observed_at_ms'] -= 400_000
    elif change == 'future':
        data['report']['observed_at_ms'] += 10_000
    elif change == 'expired':
        data['report']['observed_at_ms'] -= 120_000
        data['report']['expires_at_ms'] -= 120_000
    elif change == 'manifest': data['report']['manifest_digest'] = 'sha256:' + '0' * 64
    elif change == 'driver': data['release']['implementation_commit'] = '0' * 40
    else: data['launch']['engine_id'] = 'another-engine'
    with pytest.raises(module.TrustedSessionError):
        construct(tmp_path, data)
    assert dependencies.events == []


def test_typed_enrollment_cannot_bypass_false_claims(tmp_path, dependencies):
    profile = parse_profile(encode(profile_document()))
    parsed = module.parse_enrollment(encode(document()))
    with pytest.raises(module.TrustedSessionError):
        module.TrustedQueuedSessionComposition(profile=profile, database=tmp_path / 'hub.sqlite3',
            enrollment=replace(parsed, writers_excluded=False), ca_pem=b'ca', credential=b'token')
    assert dependencies.events == []


@contextmanager
def composed(tmp_path, dependencies):
    tmp_path.chmod(0o700)
    composition = construct(tmp_path)
    with PilotOwner(tmp_path / 'hub.sqlite3', allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(tmp_path / 'hub.sqlite3',
            execution_principal='executor:container-pilot', pilot_owner=owner)
        target = ContainerPilotTarget.from_manifest(parse_profile(encode(profile_document())).manifest)
        gate = composition.reconciliation_factory(owner, target)
        client = DistinctContainerClient()
        workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
        queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project='engineering-hub')})
        bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
            reconciliation=gate)
        feed = NativeHubEventFeed(repo.path)
        runtime = QueuedPilotRuntime(bridge, feed)
        config = AuthConfig('https://hub.example', human_auth_mode='cloudflare_access',
            cloudflare_issuer='https://fixture.cloudflareaccess.com', cloudflare_audience='fixture',
            cloudflare_allowed_emails=('beau@example.test',))
        core = create_app(Settings(repo.path, AuthSettings('h', 'c', claude_token='cl'), config,
            staging_machine_id=MACHINE), repository=repo, event_feed=feed, work_queue=queue,
            queued_pilot_runtime=runtime)
        broker = composition.session_acceptance_factory(core, owner, repo, target, gate)
        try:
            yield SimpleNamespace(composition=composition, owner=owner, repo=repo, target=target,
                gate=gate, broker=broker, core=core, queue=queue, client=client)
        finally:
            composition.close()


async def test_exact_same_gate_manual_acceptance_and_real_owned_audit(tmp_path, dependencies):
    with composed(tmp_path, dependencies) as state:
        assert state.gate.admission_state == 'pending'
        actor = state.composition._enrollment.operator_actor
        snapshot = state.broker.snapshot(actor=actor)
        assert 'Original observed_at_ms:' in snapshot['evidence']
        assert 'Session scope:' in snapshot['evidence']
        assert state.composition._enrollment.task_ids[0] in snapshot['evidence']
        assert snapshot['report_digest'] == 'sha256:' + hashlib.sha256(snapshot['evidence'].encode()).hexdigest()
        assert state.client.submissions == [] and not dependencies.observers[0].closed
        assert state.gate.async_admission_required
        assert dependencies.events.count('observe') == 0
        await state.broker.decide_async(actor=actor, request_id=snapshot['request_id'],
            expected_revision=snapshot['revision'], action='accept')
        assert state.gate.admission_state == 'accepted' and state.client.submissions == []
        with state.repo._connection() as db:
            assert [r[0] for r in db.execute('SELECT event FROM hub_session_acceptance_audit ORDER BY rowid')] == ['intent', 'result']
        assert not dependencies.observers[0].closed
    assert dependencies.observers[0].closed


async def test_expired_original_report_cannot_be_accepted_as_new_facts(tmp_path, dependencies, monkeypatch):
    with composed(tmp_path, dependencies) as state:
        actor = state.composition._enrollment.operator_actor
        snapshot = state.broker.snapshot(actor=actor)
        mono, _ = module._clock()
        expiry = state.composition._enrollment.expires_at_ms
        monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: mono + 120,
            time=lambda: (expiry + 1000) / 1000))
        with pytest.raises(Exception):
            await state.broker.decide_async(actor=actor, request_id=snapshot['request_id'],
                expected_revision=snapshot['revision'], action='accept')
        assert state.gate.admission_state == 'closed'
        assert state.client.submissions == []


def test_cli_is_explicit_fixed_selector_not_automatic_import(monkeypatch):
    from engineering_hub import trusted_session_startup as cli
    captured = []
    async def serve(*args, **kwargs):
        captured.append((args, kwargs))
    monkeypatch.setattr(cli, 'serve_configuration', serve)
    assert cli.main(['--config', '/protected/settings.json', '--pilot-config', '/protected/pilot.json',
        '--session-config', '/protected/manual-session.json']) == 0
    assert captured == [(('/protected/settings.json', '/protected/pilot.json'),
                         {'session_enrollment_path': '/protected/manual-session.json'})]


@pytest.mark.parametrize('cancelled', [False, True])
async def test_installed_selector_uses_one_capture_and_closes_only_after_drain(installed, monkeypatch, cancelled):
    import asyncio
    from engineering_hub import unified_pilot_startup as startup
    from engineering_hub import unified_queued_pilot_service as serving
    from engineering_hub.container_pilot_client import ContainerPilotClient
    with PilotOwner(installed['database'], allow_ephemeral_test_storage=True):
        pass  # Explicit synthetic enrollment, never a runtime override.
    original_owner = startup.PilotOwner
    monkeypatch.setattr(startup, 'PilotOwner', lambda path, **kw:
        original_owner(path, allow_ephemeral_test_storage=True, **kw))
    selected = installed['profile'].parent / 'manual-session.json'
    selected.write_bytes(encode(document()))
    selected.chmod(0o600)
    events, captured = [], {}
    class Composition:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            events.append('composition')
        def reconciliation_factory(self, owner, target):
            pytest.fail('Fake lifecycle does not construct runtime gates')
        def session_acceptance_factory(self, *args):
            pytest.fail('Fake lifecycle does not construct runtime brokers')
        def close(self):
            events.append('composition-close')
    monkeypatch.setattr(module, 'TrustedQueuedSessionComposition', Composition)
    original_client = ContainerPilotClient.for_verified_https
    def client(origin, credential, ca):
        assert ca is captured['ca_pem']
        assert credential.encode() == captured['credential'] == installed['token']
        assert origin == captured['profile'].origin
        events.append('client')
        return original_client(origin, credential, ca)
    monkeypatch.setattr(ContainerPilotClient, 'for_verified_https', client)
    async def serve(settings, manifest, client_factory, **kwargs):
        assert kwargs['reconciliation_factory'].__self__.__class__ is Composition
        assert kwargs['session_acceptance_factory'].__self__ is kwargs['reconciliation_factory'].__self__
        assert captured['database'] == settings.database
        kwargs['owner'].assert_active(settings.database)
        # Neither client may reopen these changed paths after the initial capture.
        installed['ca_path'].write_bytes(b'changed CA path')
        installed['token_path'].write_bytes(b'changed credential path')
        resource = client_factory()
        await resource.aclose()
        events.append('accepted-work-drained')
        if cancelled:
            raise asyncio.CancelledError()
    monkeypatch.setattr(serving, 'serve_owned_unified_queued_pilot', serve)
    coroutine = startup.serve_configuration(installed['config'], installed['profile'],
        session_enrollment_path=selected)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await coroutine
    else:
        await coroutine
    assert events == ['composition', 'client', 'accepted-work-drained', 'composition-close']


@pytest.mark.parametrize('path,callback', [('relative.json', None), ('/protected/session.json', lambda *_: None)])
async def test_selector_rejects_ambiguous_or_relative_activation_before_io(monkeypatch, path, callback):
    from engineering_hub import unified_pilot_startup as startup
    monkeypatch.setattr(startup, 'validate_configuration', lambda *_: pytest.fail('No configuration I/O'))
    with pytest.raises(startup.UnifiedPilotStartupError):
        await startup.serve_configuration('/config', '/pilot', session_enrollment_path=path,
                                         reconciliation_factory=callback)


async def test_unified_oversized_evidence_denied_before_owner_and_credentials(installed, monkeypatch):
    from engineering_hub import unified_pilot_startup as startup
    selected = installed['profile'].parent / 'oversized-session.json'
    selected.write_bytes(encode(document()))
    selected.chmod(0o600)
    validated = startup.validate_configuration(installed['config'], installed['profile'])
    monkeypatch.setattr(startup, 'validate_configuration', lambda *_: validated)
    read = startup.read_protected_file
    def read_enrollment_only(path, **kwargs):
        assert Path(path) == selected, 'credential or CA read before enrollment rejection'
        return read(path, **kwargs)
    monkeypatch.setattr(startup, 'read_protected_file', read_enrollment_only)
    monkeypatch.setattr(startup, 'PilotOwner', lambda *_a, **_kw: pytest.fail('owner opened'))
    monkeypatch.setattr(module, 'MAX_ACCEPTANCE_EVIDENCE_BYTES', 1)
    with pytest.raises(startup.UnifiedPilotStartupError):
        await startup.serve_configuration(installed['config'], installed['profile'],
                                          session_enrollment_path=selected)


async def test_installed_selector_uses_actual_async_composition_callbacks(installed, dependencies, monkeypatch):
    """Actual fixed selector/callback wiring; synthetic observer/command client."""
    from engineering_hub import unified_pilot_startup as startup
    from engineering_hub import unified_queued_pilot_service as serving
    with PilotOwner(installed['database'], allow_ephemeral_test_storage=True):
        pass
    original_owner = startup.PilotOwner
    monkeypatch.setattr(startup, 'PilotOwner', lambda path, **kw:
        original_owner(path, allow_ephemeral_test_storage=True, **kw))
    selected = installed['profile'].parent / 'actual-async-session.json'
    data = document()
    installed['document']['allowed_actors'] = ['codex', 'hermes']
    installed['document']['allowed_task_ids'].append('00000000-0000-4000-8000-000000000002')
    installed['profile'].write_bytes(encode(installed['document']))
    selected.write_bytes(encode(data))
    selected.chmod(0o600)
    seen = []
    async def serve(settings, manifest, client_factory, **kwargs):
        from engineering_hub.work_queue import WorkTarget
        veto = kwargs['target_verifier']
        allowed = WorkTarget(data['scope']['task_ids'][0], manifest.candidate.source_commit,
                             manifest.candidate.image_config_id)
        other = WorkTarget('00000000-0000-4000-8000-000000000002', allowed.source_commit,
                           allowed.artifact_digest)
        assert veto('codex', startup.ACTION, 'staging', allowed)
        assert not veto('hermes', startup.ACTION, 'staging', allowed)
        assert not veto('codex', startup.ACTION, 'staging', other)
        owner = kwargs['owner']
        target = ContainerPilotTarget.from_manifest(manifest)
        gate = kwargs['reconciliation_factory'](owner, target)
        assert gate.async_admission_required and dependencies.events.count('observe') == 0
        repo = HubRepository(settings.database, execution_principal='executor:container-pilot', pilot_owner=owner)
        client = DistinctContainerClient()
        workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
        queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project='engineering-hub')})
        bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
                                      reconciliation=gate)
        feed = NativeHubEventFeed(repo.path)
        runtime = QueuedPilotRuntime(bridge, feed)
        core = create_app(settings, repository=repo, event_feed=feed, work_queue=queue,
                          queued_pilot_runtime=runtime)
        broker = kwargs['session_acceptance_factory'](core, owner, repo, target, gate)
        assert broker._gate is gate and dependencies.events.count('observe') == 0
        snapshot = broker.snapshot(actor=data['operator_actor'])
        await broker.decide_async(actor=data['operator_actor'], request_id=snapshot['request_id'],
                                  expected_revision=snapshot['revision'], action='accept')
        assert gate.admission_state == 'accepted' and not client.submissions
        assert dependencies.events.count('observe') == 1
        seen.append(True)
    monkeypatch.setattr(serving, 'serve_owned_unified_queued_pilot', serve)
    await startup.serve_configuration(installed['config'], installed['profile'],
                                      session_enrollment_path=selected)
    assert seen == [True] and dependencies.observers[0].closed


@pytest.mark.parametrize('action,expected_calls', [('accept', 1), ('decline', 0)])
def test_installed_async_acceptance_keeps_shared_loop_health_responsive(action, expected_calls):
    """Supersedes the old 1/4/2 synchronous snapshot/accept/decline counts.

    Metadata and decline no longer observe; one owned preflight checks accept.
    This measures only isolated ASGI health while synthetic identity I/O waits,
    not real NAS/Engine latency or responsiveness during durable audit commits.
    """
    from tests.responsiveness_probe import run_isolated_probe
    report = run_isolated_probe('health', action=action)
    assert report['latency_accepted'] and report['functional_verified']
    assert report['observation_calls'] == expected_calls


def test_verified_proof_clipped_to_original_report_deadline(tmp_path, dependencies):
    composition = construct(tmp_path)
    try:
        composition._deadline = time.monotonic() + .5
        proof = composition._current_verified()
        assert proof.expires_monotonic == composition._deadline
        assert proof.started_monotonic < proof.expires_monotonic
    finally:
        composition.close()


def test_verified_absolute_wall_ceiling_clipped_to_original_report(tmp_path, dependencies):
    data = document()
    data['report']['expires_at_ms'] = int(time.time() * 1000) + 1000
    composition = construct(tmp_path, data)
    try:
        proof = composition._current_verified()
        assert proof.expires_wall_ms == data['report']['expires_at_ms']
        assert proof.verified_wall_ms < proof.expires_wall_ms
        assert proof.expires_monotonic <= composition._deadline
    finally:
        composition.close()
