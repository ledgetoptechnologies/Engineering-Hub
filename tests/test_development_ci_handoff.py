"""Durable Hub<->CI-host handoff over the real owned queue; no host execution.

The host runner, channel, HMAC authenticator, in-memory external anchor and
terminal verifier below are TEST-ONLY doubles (key generated per test). No real
transport, credential, external anchor or Docker engine is used.
"""
import hmac
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import sys
from types import SimpleNamespace
import uuid

import pytest

from engineering_hub.development_ci import DevelopmentCIManifest, QueuedDevelopmentCI, _canonical
from engineering_hub.development_ci_handoff import (
    HANDOFF_MARKER, CIHostHandoff, DurableDevelopmentCIHandoff, HandoffRefused, LegacyControllersRemoved,
    OperatorReconciliation, engine_admission_lock, enroll_host, receipt_digest, reconcile_restored_journal)
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import ClosedClaimPolicy, WorkAction, WorkQueue, WorkTarget
from tests.test_hub_container_pilot import reviewed_task

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux owned ledger and flock')
ACTION = 'engineering-hub.development-ci'
MACHINE = 'staging-pc'
IMAGE = 'sha256:' + 'd' * 64
ACTIONS = {ACTION: WorkAction(MACHINE, project='engineering-hub'),
           'engineering-hub.production': WorkAction(MACHINE, frozenset({'production'}), project='engineering-hub')}


def load_script(name):
    path = Path(__file__).resolve().parents[1] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name + '_under_test_' + uuid.uuid4().hex, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HmacAuth:
    """Test-only stand-in for the not-yet-existing authenticated channel."""
    def __init__(self):
        self.key = secrets.token_bytes(32)

    def sign(self, operation, payload):
        return hmac.new(self.key, (operation + '\0' + _canonical(payload)).encode(), 'sha256').hexdigest()

    def verify(self, operation, payload, credential):
        return isinstance(credential, str) and hmac.compare_digest(credential, self.sign(operation, payload))


class MemoryExternalAnchor:
    """Test-only external non-rollback anchor (survives journal/anchor restore)."""
    def __init__(self):
        self.fences = {}

    def highest_fence(self, generation):
        return self.fences.get(generation, 0)

    def advance(self, generation, fence):
        self.fences[generation] = max(self.fences.get(generation, 0), fence)
        return True


class UnsafeTestOnlyTerminalVerifier:
    """Accepts the runner's observation. NEVER a production verifier."""
    def verify(self, start_request, terminal):
        return terminal['descendants_empty'] is True


class Runner:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.during = None

    def __call__(self, request, guard):
        guard()
        self.calls.append(request)
        if self.during is not None:
            self.during()
        if self.fail:
            raise RuntimeError('host crashed before terminal')
        return {'container_id': 'f' * 64,
                'cgroup': {'path': '/sys/fs/cgroup/eh-ci.scope', 'device': 7, 'inode': 100 + len(self.calls)},
                'descendants_empty': True, 'terminal_verified': True, 'exit_code': 0,
                'engine_id': 'engine-one', 'image_id': IMAGE, 'timed_out': False, 'failed': False}


class Channel:
    """In-process stand-in for the intended (disabled) supervisor transport."""
    def __init__(self, rig):
        self.rig = rig
        self.lose, self.block, self.tamper = set(), set(), None
        self.starts, self.finalizes = [], []

    async def start(self, payload, credential):
        if 'start' in self.block:
            raise ConnectionError('blocked')
        self.starts.append((payload, credential))
        response, signature = self.rig.host.start(payload, credential)
        if self.tamper is not None:
            response = self.tamper(response)
            signature = self.rig.auth.sign('start_result', response)
        if 'start' in self.lose:
            self.lose.discard('start')
            raise ConnectionError('lost start response')
        return response, signature

    async def inspect(self, payload, credential):
        return self.rig.host.inspect(payload, credential)

    async def finalize(self, payload, credential):
        if 'finalize' in self.block:
            raise ConnectionError('blocked')
        self.finalizes.append(payload)
        result = self.rig.host.finalize(payload, credential)
        if 'finalize' in self.lose:
            self.lose.discard('finalize')
            raise ConnectionError('lost finalize response')
        return result


def _verifier(repo):
    def verified(actor, action, environment, target):
        task = repo.get_task(target.task_id)
        return (task['owner'] == actor and task['status'] == 'verified'
                and task['candidate_commit'] == target.source_commit)
    return verified


def _queue(repo, clock):
    return WorkQueue(repo, dict(ACTIONS), claim_verifier=ClosedClaimPolicy(), target_verifier=_verifier(repo),
                     approval_verifier=lambda *args: True, clock=lambda: clock[0])


RIG = object()


def make_host(rig, *, authenticator=RIG, external_anchor=RIG, terminal_verifier=RIG, enable=True):
    pick = lambda value, default: default if value is RIG else value
    return CIHostHandoff(rig.journal, rig.anchor, rig.engine, manifest_digest=rig.manifest.digest,
        runner=rig.runner, authenticator=pick(authenticator, rig.auth),
        external_anchor=pick(external_anchor, rig.external),
        terminal_verifier=pick(terminal_verifier, UnsafeTestOnlyTerminalVerifier()),
        enable_durable_handoff=enable)


def make_bridge(rig, queue, owner, *, authenticator=RIG, enable=True):
    return DurableDevelopmentCIHandoff(queue, owner, rig.manifest, rig.channel, action_key=ACTION,
        host_generation=rig.generation, authenticator=rig.auth if authenticator is RIG else authenticator,
        enable_durable_handoff=enable, renew_interval=.01)


def restart_hub(rig, *, compose=True, **kwargs):
    """Simulated Hub crash/restart: new owner epoch, repository and queue."""
    rig.owner.close()
    rig.owner = PilotOwner(rig.path, allow_ephemeral_test_storage=True, require_existing=True)
    rig.repo = HubRepository(rig.path, execution_principal='executor:software-validation', pilot_owner=rig.owner)
    rig.queue = _queue(rig.repo, rig.clock)
    rig.bridge = make_bridge(rig, rig.queue, rig.owner, **kwargs) if compose else None
    return rig.bridge


def attestation(engine):
    return LegacyControllersRemoved(str(Path(engine).absolute()), ('local_development_ci f099 build',),
                                    'operator:test')


@pytest.fixture
def rig(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    engine = tmp_path / 'engine'
    engine.mkdir(mode=0o700)
    hostdir = tmp_path / 'host'
    hostdir.mkdir(mode=0o700)
    journal, anchor = hostdir / 'journal.sqlite3', hostdir / 'anchor.json'
    generation = enroll_host(journal, anchor, engine, attestation=attestation(engine), enable_durable_handoff=True)
    shutil.copy(journal, tmp_path / 'journal-backup.sqlite3')
    shutil.copy(anchor, tmp_path / 'anchor-backup.json')
    path = private / 'hub.sqlite3'
    owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    repo = HubRepository(path, execution_principal='executor:software-validation', pilot_owner=owner)
    clock = [1000.0]
    manifest = DevelopmentCIManifest(MACHINE, 'engine-one', IMAGE, 'sha256:' + 'e' * 64, ('sha256:' + 'a' * 64,) * 4)
    value = SimpleNamespace(tmp=tmp_path, path=path, owner=owner, repo=repo, clock=clock, manifest=manifest,
        engine=engine, journal=journal, anchor=anchor, generation=generation, auth=HmacAuth(),
        external=MemoryExternalAnchor(), runner=Runner())
    value.host = make_host(value)
    value.channel = Channel(value)
    value.queue = _queue(repo, clock)
    value.bridge = make_bridge(value, value.queue, owner)
    try:
        yield value
    finally:
        value.owner.close()


def submit(rig, key='one'):
    task = reviewed_task(rig.repo, key)
    return rig.queue.submit(actor='codex', request_id=key, action_key=ACTION, environment='staging',
        target=WorkTarget(task['id'], task['candidate_commit'], rig.manifest.digest))


def reservation(rig):
    with rig.repo._connection() as db:
        row = db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', (MACHINE,)).fetchone()
        return row['job_id'] if row else None


def releases(rig, job_id):
    with rig.repo._connection() as db:
        return db.execute("SELECT COUNT(*) FROM hub_audit WHERE entity_id=? AND action IN "
                          "('work.succeeded','work.failed','work.reconciled')", (job_id,)).fetchone()[0]


def handoff(rig, job_id):
    with rig.repo._connection() as db:
        return db.execute('SELECT * FROM hub_development_ci_handoff WHERE job_id=?', (job_id,)).fetchone()


def host_rows(rig):
    db = sqlite3.connect(rig.journal)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute('SELECT * FROM handoff_attempts ORDER BY fence')]
    finally:
        db.close()


def host_state(rig):
    return [row['state'] for row in host_rows(rig)]


def release_of(rig, row):
    release = {'attempt_id': row['attempt_id'], 'generation': row['generation'], 'fence': row['fence'],
               'receipt_digest': row['receipt_digest'], 'hub_ack': row['hub_ack'], 'state': 'released'}
    return {'release': release, 'credential': rig.auth.sign('release', release)}


def start_request(rig, fence, *, prior='latest', **changes):
    if prior == 'latest':
        rows = host_rows(rig)
        prior = release_of(rig, rows[-1]) if rows else None
    request = {'attempt_id': str(uuid.uuid4()), 'job_id': 'manual-job', 'fence': fence,
               'generation': rig.generation, 'manifest_digest': rig.manifest.digest, 'source_commit': 'c' * 40,
               'prior_release': prior}
    request.update(changes)
    return request, rig.auth.sign('start', request)


# --- protocol -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handoff_completes_once_and_host_slot_stays_reserved(rig):
    job = submit(rig)
    result = await rig.bridge.run_once()
    assert result['id'] == job['id'] and result['state'] == 'succeeded'
    assert reservation(rig) is None and releases(rig, job['id']) == 1
    row = handoff(rig, job['id'])
    assert row['state'] == 'finalized' and json.loads(row['host_ack'])['ack']['state'] == 'finalized_reserved'
    assert host_state(rig) == ['finalized_reserved'] and len(rig.runner.calls) == 1
    assert rig.external.highest_fence(rig.generation) == 1
    second = submit(rig, 'two')
    assert (await rig.bridge.run_once())['id'] == second['id']
    rows = host_rows(rig)
    assert [r['fence'] for r in rows] == [1, 2] and rows[0]['hub_release'] is not None


@pytest.mark.asyncio
async def test_finalized_reserved_admits_nothing_without_hub_start_even_after_restart(rig):
    submit(rig)
    await rig.bridge.run_once()
    before = host_rows(rig)
    host = make_host(rig)  # host process restart: nothing runs by itself
    assert host_rows(rig) == before and len(rig.runner.calls) == 1
    payload, credential = rig.channel.starts[0]
    with pytest.raises(HandoffRefused, match='handoff_fence_stale'):
        host.start(payload, credential)  # exact replay of the original start
    assert host_rows(rig) == before and len(rig.runner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fence', [1, 0])
async def test_start_with_fence_not_above_highest_is_refused(rig, fence):
    submit(rig)
    await rig.bridge.run_once()
    before = host_rows(rig)
    with pytest.raises(HandoffRefused, match='handoff_fence_stale|handoff_request_invalid'):
        rig.host.start(*start_request(rig, fence))
    assert host_rows(rig) == before and len(rig.runner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['running', 'terminal_held'])
async def test_higher_fence_refused_while_earlier_attempt_unfinalized(rig, stage):
    job = submit(rig)
    if stage == 'running':
        rig.runner.fail = True  # host restart before terminal
    else:
        rig.channel.block.add('finalize')
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert host_state(rig) == [stage] and reservation(rig) == job['id']
    host = make_host(rig)  # restart: still held, no replay
    before = host_rows(rig)
    with pytest.raises(HandoffRefused, match='handoff_slot_held'):
        host.start(*start_request(rig, 99, prior=None))
    assert host_rows(rig) == before and len(rig.runner.calls) == 1
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert reservation(rig) == job['id'] and len(rig.runner.calls) == 1


def _crash_release(rig, monkeypatch):
    complete = rig.queue.complete
    def crash(**kwargs):
        if kwargs['outcome'] != 'held':
            raise RuntimeError('lost Hub release')
        return complete(**kwargs)
    monkeypatch.setattr(rig.queue, 'complete', crash)


@pytest.mark.asyncio
async def test_greater_fence_before_hub_released_previous_job_is_refused(rig, monkeypatch):
    first = submit(rig, 'first')
    _crash_release(rig, monkeypatch)
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert host_state(rig) == ['finalized_reserved'] and handoff(rig, first['id'])['state'] == 'host_acked'
    row = host_rows(rig)[0]
    before = host_rows(rig)
    forged = release_of(rig, row)
    forged['credential'] = 'not-the-hub'
    wrong = release_of(rig, {**row, 'hub_ack': '9' * 64})
    for prior, code in ((None, 'prior_release_required'), (forged, 'prior_release_required'),
                        (wrong, 'prior_release_mismatch')):
        with pytest.raises(HandoffRefused, match=code):
            rig.host.start(*start_request(rig, 2, prior=prior))
        assert host_rows(rig) == before
    second = submit(rig, 'second')
    monkeypatch.undo()
    rig.channel.block.add('finalize')
    # Pending release is resumed first; the second job is never started.
    assert (await rig.bridge.run_once())['id'] == first['id']
    assert rig.queue.get_job(second['id'])['state'] == 'queued' and len(rig.runner.calls) == 1


@pytest.mark.asyncio
async def test_hub_crash_before_receipt_committed_reinspects_and_commits(rig):
    job = submit(rig)
    rig.channel.lose.add('start')
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert handoff(rig, job['id'])['state'] == 'intent' and host_state(rig) == ['terminal_held']
    restart_hub(rig)
    result = await rig.bridge.run_once()
    assert result['state'] == 'succeeded' and reservation(rig) is None
    assert releases(rig, job['id']) == 1 and len(rig.runner.calls) == 1
    assert host_state(rig) == ['finalized_reserved']


@pytest.mark.asyncio
async def test_crash_between_host_ack_and_hub_record_resumes_once_without_new_start(rig):
    first = submit(rig, 'first')
    second = submit(rig, 'second')
    rig.channel.lose.add('finalize')  # host acked, Hub never recorded it
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert host_state(rig) == ['finalized_reserved']
    assert handoff(rig, first['id'])['state'] == 'receipt_committed'
    stored_ack = handoff(rig, first['id'])['hub_ack']
    restart_hub(rig)
    rig.channel.block.add('finalize')
    assert (await rig.bridge.run_once())['id'] == first['id']
    assert reservation(rig) == first['id'] and len(rig.runner.calls) == 1
    assert rig.queue.get_job(second['id'])['state'] == 'queued'
    rig.channel.block.clear()
    result = await rig.bridge.run_once()
    assert result['id'] == first['id'] and result['state'] == 'succeeded'
    assert [item['hub_ack'] for item in rig.channel.finalizes] == [stored_ack, stored_ack]
    assert releases(rig, first['id']) == 1 and handoff(rig, first['id'])['state'] == 'finalized'
    result = await rig.bridge.run_once()
    assert result['id'] == second['id'] and result['state'] == 'succeeded'
    assert [call['fence'] for call in rig.runner.calls] == [1, 2]
    assert releases(rig, first['id']) == 1


@pytest.mark.asyncio
async def test_crash_after_recorded_host_ack_completes_once_without_new_host_call(rig, monkeypatch):
    job = submit(rig)
    _crash_release(rig, monkeypatch)
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert handoff(rig, job['id'])['state'] == 'host_acked'
    restart_hub(rig)
    assert (await rig.bridge.run_once())['state'] == 'succeeded'
    assert len(rig.channel.finalizes) == 1 and releases(rig, job['id']) == 1


@pytest.mark.asyncio
async def test_finalize_lost_response_identical_retry_completes_once(rig):
    job = submit(rig)
    rig.channel.lose.add('finalize')
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert host_state(rig) == ['finalized_reserved'] and reservation(rig) == job['id']
    result = await rig.bridge.run_once()
    assert result['state'] == 'succeeded' and reservation(rig) is None
    assert len(rig.channel.finalizes) == 2 and rig.channel.finalizes[0] == rig.channel.finalizes[1]
    assert releases(rig, job['id']) == 1 and len(rig.runner.calls) == 1


@pytest.mark.asyncio
async def test_finalize_or_start_with_different_tuple_is_refused_rows_unchanged(rig):
    job = submit(rig)
    rig.channel.block.add('finalize')
    await rig.bridge.run_once()
    row = handoff(rig, job['id'])
    good = {'attempt_id': row['attempt_id'], 'generation': rig.generation, 'fence': row['fence'],
            'receipt_digest': row['receipt_digest'], 'hub_ack': row['hub_ack']}
    before = host_rows(rig)
    for change in ({'fence': row['fence'] + 1}, {'receipt_digest': 'sha256:' + '0' * 64},
                   {'generation': '0' * 32}, {'attempt_id': 'other-attempt'}):
        request = {**good, **change}
        with pytest.raises(HandoffRefused, match='handoff_finalize_mismatch'):
            rig.host.finalize(request, rig.auth.sign('finalize', request))
        assert host_rows(rig) == before
    for change in ({'generation': '0' * 32}, {'manifest_digest': 'sha256:' + '1' * 64}, {}):
        with pytest.raises(HandoffRefused):
            rig.host.start(*start_request(rig, 50, prior=None, **change))
        assert host_rows(rig) == before
    rig.host.finalize(good, rig.auth.sign('finalize', good))
    after = host_rows(rig)
    other = {**good, 'hub_ack': '1' * 64}
    with pytest.raises(HandoffRefused, match='handoff_finalize_mismatch'):
        rig.host.finalize(other, rig.auth.sign('finalize', other))
    assert host_rows(rig) == after
    assert rig.host.finalize(good, rig.auth.sign('finalize', good))[0]['state'] == 'finalized_reserved'


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['container', 'cgroup'])
async def test_receipt_with_different_container_or_cgroup_is_never_committed(rig, field):
    job = submit(rig)
    def tamper(response):
        response = json.loads(json.dumps(response))
        if field == 'container':
            response['terminal']['container_id'] = 'a' * 64
        else:
            response['terminal']['cgroup']['inode'] += 1
        record = {key: response[key] for key in ('attempt_id', 'job_id', 'fence', 'generation',
                                                 'manifest_digest', 'source_commit', 'terminal')}
        response['receipt_digest'] = receipt_digest(record)
        return response
    rig.channel.tamper = tamper
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert handoff(rig, job['id'])['state'] == 'observed' and handoff(rig, job['id'])['hub_ack'] is None
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert not rig.channel.finalizes and host_state(rig) == ['terminal_held']
    assert reservation(rig) == job['id'] and releases(rig, job['id']) == 0


@pytest.mark.asyncio
async def test_injected_terminal_without_trusted_verifier_stays_held(rig):
    job = submit(rig)
    rig.host = make_host(rig, terminal_verifier=None)  # production default
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert len(rig.runner.calls) == 1 and host_state(rig) == ['running']
    assert handoff(rig, job['id'])['state'] == 'intent' and reservation(rig) == job['id']


# --- writer exclusion, triggers and schema -------------------------------------

def test_legacy_controller_and_legacy_queue_composition_refuse_while_enrolled(rig, monkeypatch):
    runner = load_script('local_development_ci')
    output = rig.tmp / 'output'
    output.mkdir(mode=0o700)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(rig.engine / 'engine.sock'))
    monkeypatch.setattr(runner, '_run_admitted', lambda *a, **k: pytest.fail('standalone CI admitted'))
    args = SimpleNamespace(image=IMAGE, ref='refs/heads/codex/example', timeout=10, checkout=str(rig.tmp),
                           output_root=str(output), engine='unix://' + str(rig.engine / 'engine.sock'))
    try:
        with pytest.raises(runner.CIError, match='admission denied'):
            runner.run(args)
    finally:
        sock.close()
    restart_hub(rig, compose=False)
    class Provider:
        async def invoke(self, attempt, guard): pytest.fail('legacy provider invoked')
        async def verify_terminal(self, attempt, receipt): pytest.fail('legacy provider invoked')
    with pytest.raises(HubError, match='development_ci_handoff_enrolled'):
        QueuedDevelopmentCI(rig.queue, rig.owner, rig.manifest, Provider(), action_key=ACTION)


def test_old_controller_and_enrollment_or_start_never_both_proceed(rig, tmp_path):
    runner = load_script('local_development_ci')
    fresh = tmp_path / 'fresh-engine'
    fresh.mkdir(mode=0o700)
    # An old controller holding the shared lock (it does not read the marker).
    fd = runner.private_lock(fresh / 'admission.lock')
    try:
        with pytest.raises(HandoffRefused, match='handoff_host_busy'):
            enroll_host(tmp_path / 'j', tmp_path / 'a', fresh, attestation=attestation(fresh),
                        enable_durable_handoff=True)
    finally:
        os.close(fd)
    assert not (fresh / HANDOFF_MARKER).exists() and not (tmp_path / 'j').exists()
    fd = runner.private_lock(rig.engine / 'admission.lock')
    try:
        with pytest.raises(HandoffRefused, match='handoff_host_busy'):
            rig.host.start(*start_request(rig, 1))
    finally:
        os.close(fd)
    assert host_rows(rig) == [] and not rig.runner.calls
    # Conversely, the handoff holding the lock excludes the old controller's lock.
    with engine_admission_lock(rig.engine):
        with pytest.raises(runner.CIError, match='Another operation owns'):
            runner.private_lock(rig.engine / 'admission.lock')


@pytest.mark.parametrize('mutation', ['symlink', 'mode'])
def test_admission_lock_identity_is_validated(rig, mutation):
    lock = rig.engine / 'admission.lock'
    lock.unlink()
    if mutation == 'symlink':
        target = rig.tmp / 'elsewhere.lock'
        target.write_text('')
        target.chmod(0o600)
        lock.symlink_to(target)
    else:
        lock.write_text('')
        lock.chmod(0o644)
    with pytest.raises(HandoffRefused, match='handoff_admission_lock_invalid'):
        rig.host.start(*start_request(rig, 1))
    assert host_rows(rig) == [] and not rig.runner.calls


@pytest.mark.asyncio
async def test_direct_host_writes_and_early_hub_release_are_aborted(rig):
    job = submit(rig)
    rig.channel.block.add('finalize')
    await rig.bridge.run_once()
    assert host_state(rig) == ['terminal_held']
    assert handoff(rig, job['id'])['state'] == 'receipt_committed'
    db = sqlite3.connect(rig.journal)
    try:
        for statement in ("UPDATE handoff_attempts SET state='finalized_reserved'",
                          "UPDATE handoff_attempts SET state='finalized_reserved',hub_ack='short'",
                          "UPDATE handoff_attempts SET state='running'",
                          "UPDATE handoff_attempts SET fence=fence+10",
                          'DELETE FROM handoff_attempts',
                          "UPDATE handoff_meta SET floor_fence=-1"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
        db.rollback()
    finally:
        db.close()
    assert host_state(rig) == ['terminal_held']
    # Old queue writer / raw SQL after receipt_committed but before a recorded
    # verified host ack: the ledger trigger refuses the release.
    with pytest.raises(sqlite3.IntegrityError, match='development_ci_handoff_unfinalized'):
        with rig.repo._transaction() as hub:
            hub.execute("UPDATE hub_work_jobs SET state='succeeded' WHERE id=?", (job['id'],))
    with pytest.raises(sqlite3.IntegrityError, match='development_ci_handoff_transition'):
        with rig.repo._transaction() as hub:
            hub.execute("UPDATE hub_development_ci_handoff SET state='host_acked' WHERE job_id=?", (job['id'],))
    assert reservation(rig) == job['id'] and releases(rig, job['id']) == 0


@pytest.mark.asyncio
async def test_forged_recorded_host_ack_is_not_accepted_as_finalize_proof(rig):
    job = submit(rig)
    rig.channel.block.add('finalize')
    await rig.bridge.run_once()
    row = handoff(rig, job['id'])
    ack = {'attempt_id': row['attempt_id'], 'generation': rig.generation, 'fence': row['fence'],
           'receipt_digest': row['receipt_digest'], 'hub_ack': row['hub_ack'], 'state': 'finalized_reserved'}
    with rig.repo._transaction() as hub:  # raw writer forging the record
        hub.execute("UPDATE hub_development_ci_handoff SET state='host_acked',host_ack=? WHERE job_id=?",
                    (_canonical({'ack': ack, 'credential': 'forged'}), job['id']))
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert reservation(rig) == job['id'] and releases(rig, job['id']) == 0


def test_host_journal_schema_tampering_refuses(rig):
    for statement in ('DROP TRIGGER handoff_transition_guard',
                      'CREATE TRIGGER sneaky AFTER UPDATE ON handoff_attempts BEGIN SELECT 1; END'):
        shutil.copy(rig.tmp / 'journal-backup.sqlite3', rig.journal)
        db = sqlite3.connect(rig.journal)
        db.execute(statement)
        db.commit()
        db.close()
        with pytest.raises(HandoffRefused, match='handoff_journal_schema_invalid'):
            make_host(rig)


@pytest.mark.asyncio
async def test_hub_schema_tampering_refuses(rig):
    submit(rig)
    with rig.repo._transaction() as hub:
        hub.execute('DROP TRIGGER hub_ci_handoff_release_guard')
    with pytest.raises(HubError, match='schema_invalid'):
        await rig.bridge.run_once()
    assert not rig.runner.calls
    with pytest.raises(HubError, match='development_ci_handoff_schema_invalid'):
        restart_hub(rig)


# --- anchors, restore and reconciliation ----------------------------------------

@pytest.mark.asyncio
async def test_coordinated_rollback_of_anchor_and_journal_refused_until_reconcile(rig):
    submit(rig)
    await rig.bridge.run_once()
    finalized = host_rows(rig)[0]
    shutil.copy(rig.tmp / 'journal-backup.sqlite3', rig.journal)  # coordinated local rollback
    shutil.copy(rig.tmp / 'anchor-backup.json', rig.anchor)
    with pytest.raises(HandoffRefused, match='handoff_external_anchor_unavailable'):
        make_host(rig, external_anchor=None).start(*start_request(rig, 5, prior=None))
    with pytest.raises(HandoffRefused, match='handoff_journal_restored'):
        rig.host.start(*start_request(rig, 5, prior=None))
    info = os.stat(rig.journal)
    good = OperatorReconciliation(rig.generation, info.st_dev, info.st_ino, 1)
    for confirmation, external in ((True, rig.external), (good, None),
                                   (OperatorReconciliation(rig.generation, info.st_dev, info.st_ino + 1, 1), rig.external),
                                   (OperatorReconciliation(rig.generation, info.st_dev, info.st_ino, 7), rig.external)):
        with pytest.raises(HandoffRefused):
            reconcile_restored_journal(rig.journal, rig.anchor, rig.engine, confirmation, external_anchor=external)
    assert len(rig.runner.calls) == 1
    reconcile_restored_journal(rig.journal, rig.anchor, rig.engine, good, external_anchor=rig.external)
    with pytest.raises(HandoffRefused, match='handoff_fence_stale'):
        rig.host.start(*start_request(rig, 1, prior=None))
    with pytest.raises(HandoffRefused, match='prior_release_required'):
        rig.host.start(*start_request(rig, 5, prior=None))
    assert rig.host.start(*start_request(rig, 5, prior=release_of(rig, finalized)))[0]['state'] == 'terminal_held'
    assert len(rig.runner.calls) == 2


def test_default_external_anchor_refuses_admission(rig):
    with pytest.raises(HandoffRefused, match='handoff_external_anchor_unavailable'):
        make_host(rig, external_anchor=None).start(*start_request(rig, 1))
    assert host_rows(rig) == [] and not rig.runner.calls


@pytest.mark.parametrize('missing', ['anchor', 'marker'])
def test_unknown_anchor_or_marker_refuses_admission(rig, missing):
    (rig.anchor if missing == 'anchor' else rig.engine / HANDOFF_MARKER).unlink()
    with pytest.raises(HandoffRefused, match='anchor_unknown|enrollment_marker_invalid'):
        rig.host.start(*start_request(rig, 1))
    assert host_rows(rig) == [] and not rig.runner.calls


# --- defaults --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_authenticator_refuses_on_host_and_hub(rig):
    host = make_host(rig, authenticator=None)
    request, credential = start_request(rig, 1)
    for value in (credential, None, 'anything'):
        with pytest.raises(HandoffRefused, match='handoff_unauthenticated'):
            host.start(request, value)
    with pytest.raises(HandoffRefused, match='handoff_unauthenticated'):
        host.finalize({'attempt_id': 'a', 'generation': rig.generation, 'fence': 1,
                       'receipt_digest': 'sha256:' + '0' * 64, 'hub_ack': '0' * 64}, credential)
    assert host_rows(rig) == [] and not rig.runner.calls
    job = submit(rig)
    restart_hub(rig, authenticator=None)
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert not rig.channel.starts and not rig.runner.calls and reservation(rig) == job['id']


@pytest.mark.asyncio
async def test_disabled_by_default_and_enrollment_requires_attestation(rig, tmp_path):
    with pytest.raises(HandoffRefused, match='handoff_disabled'):
        make_host(rig, enable=False)
    with pytest.raises(HandoffRefused, match='handoff_disabled'):
        enroll_host(tmp_path / 'j2', tmp_path / 'a2', rig.engine)
    fresh = tmp_path / 'fresh'
    fresh.mkdir(mode=0o700)
    for value in (None, True, LegacyControllersRemoved(str(fresh), (), 'operator:test'),
                  LegacyControllersRemoved(str(rig.engine), ('old',), 'operator:test')):
        with pytest.raises(HandoffRefused, match='handoff_legacy_attestation_required'):
            enroll_host(tmp_path / 'j3', tmp_path / 'a3', fresh, attestation=value, enable_durable_handoff=True)
    assert not (fresh / HANDOFF_MARKER).exists()
    restart_hub(rig, compose=False)
    with pytest.raises(HubError, match='development_ci_handoff_disabled'):
        make_bridge(rig, rig.queue, rig.owner, enable=False)
    assert getattr(rig.repo, '_development_ci', None) is None
    assert rig.queue.completion_verifier is None and rig.queue.reconciliation_verifier is None
