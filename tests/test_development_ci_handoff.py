"""Durable Hub<->CI-host handoff over the real owned queue; no host execution.

The host runner, channel and HMAC authenticator below are test doubles with a
key generated per test. No real transport, credential or Docker engine is used.
"""
import asyncio
import hmac
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
from types import SimpleNamespace
import uuid

import pytest

from engineering_hub.development_ci import DevelopmentCIManifest, QueuedDevelopmentCI, _canonical
from engineering_hub.development_ci_handoff import (
    HANDOFF_MARKER, CIHostHandoff, DurableDevelopmentCIHandoff, HandoffRefused,
    enroll_host, reconcile_restored_journal)
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


class HmacAuth:
    """Per-test HMAC stand-in for the not-yet-existing authenticated channel."""
    def __init__(self):
        import secrets
        self.key = secrets.token_bytes(32)

    def sign(self, operation, payload):
        return hmac.new(self.key, (operation + '\0' + _canonical(payload)).encode(), 'sha256').hexdigest()

    def verify(self, operation, payload, credential):
        return isinstance(credential, str) and hmac.compare_digest(credential, self.sign(operation, payload))


class Runner:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.exit_code = 0

    def __call__(self, request, guard):
        guard()
        self.calls.append(request)
        if self.fail:
            raise RuntimeError('host crashed before terminal')
        return {'container_id': 'f' * 64,
                'cgroup': {'path': '/sys/fs/cgroup/eh-ci.scope', 'device': 7, 'inode': 100 + len(self.calls)},
                'descendants_empty': True, 'terminal_verified': True, 'exit_code': self.exit_code,
                'engine_id': 'engine-one', 'image_id': IMAGE, 'timed_out': False, 'failed': False}


class Channel:
    """In-process stand-in for the intended (disabled) supervisor transport."""
    def __init__(self, host, auth):
        self.host, self.auth = host, auth
        self.lose, self.block, self.tamper = set(), set(), None
        self.starts, self.finalizes = [], []

    async def start(self, payload, credential):
        if 'start' in self.block:
            raise ConnectionError('blocked')
        self.starts.append((payload, credential))
        response, signature = self.host.start(payload, credential)
        if self.tamper is not None:
            response = self.tamper(response)
            signature = self.auth.sign('start_result', response)
        if 'start' in self.lose:
            self.lose.discard('start')
            raise ConnectionError('lost start response')
        return response, signature

    async def inspect(self, payload, credential):
        return self.host.inspect(payload, credential)

    async def finalize(self, payload, credential):
        if 'finalize' in self.block:
            raise ConnectionError('blocked')
        self.finalizes.append(payload)
        result = self.host.finalize(payload, credential)
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


def make_host(rig, *, authenticator='rig', **kwargs):
    return CIHostHandoff(rig.journal, rig.anchor, rig.engine, manifest_digest=rig.manifest.digest,
        runner=rig.runner, authenticator=rig.auth if authenticator == 'rig' else authenticator,
        enable_durable_handoff=kwargs.pop('enable', True), **kwargs)


def make_bridge(rig, queue, owner, *, authenticator='rig', enable=True):
    return DurableDevelopmentCIHandoff(queue, owner, rig.manifest, rig.channel, action_key=ACTION,
        host_generation=rig.generation, authenticator=rig.auth if authenticator == 'rig' else authenticator,
        enable_durable_handoff=enable, renew_interval=.01)


def restart_hub(rig, *, compose=True, **kwargs):
    """Simulated Hub crash/restart: new owner epoch, repository and queue."""
    rig.owner.close()
    rig.owner = PilotOwner(rig.path, allow_ephemeral_test_storage=True, require_existing=True)
    rig.repo = HubRepository(rig.path, execution_principal='executor:software-validation', pilot_owner=rig.owner)
    rig.queue = _queue(rig.repo, rig.clock)
    rig.bridge = make_bridge(rig, rig.queue, rig.owner, **kwargs) if compose else None
    return rig.bridge


@pytest.fixture
def rig(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    engine = tmp_path / 'engine'
    engine.mkdir(mode=0o700)
    hostdir = tmp_path / 'host'
    hostdir.mkdir(mode=0o700)
    anchors = tmp_path / 'anchor-store'
    anchors.mkdir(mode=0o700)
    journal, anchor = hostdir / 'journal.sqlite3', anchors / 'anchor.json'
    generation = enroll_host(journal, anchor, engine, enable_durable_handoff=True)
    shutil.copy(journal, tmp_path / 'journal-backup.sqlite3')
    path = private / 'hub.sqlite3'
    owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    repo = HubRepository(path, execution_principal='executor:software-validation', pilot_owner=owner)
    clock = [1000.0]
    manifest = DevelopmentCIManifest(MACHINE, 'engine-one', IMAGE, 'sha256:' + 'e' * 64, ('sha256:' + 'a' * 64,) * 4)
    value = SimpleNamespace(tmp=tmp_path, path=path, owner=owner, repo=repo, clock=clock, manifest=manifest,
        engine=engine, journal=journal, anchor=anchor, generation=generation, auth=HmacAuth(), runner=Runner())
    value.host = make_host(value)
    value.channel = Channel(value.host, value.auth)
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
    try:
        return db.execute('SELECT * FROM handoff_attempts ORDER BY fence').fetchall()
    finally:
        db.close()


def host_state(rig):
    return [row[6] for row in host_rows(rig)]


def start_request(rig, fence, **changes):
    request = {'attempt_id': str(uuid.uuid4()), 'job_id': 'manual-job', 'fence': fence,
               'generation': rig.generation, 'manifest_digest': rig.manifest.digest, 'source_commit': 'c' * 40}
    request.update(changes)
    return request, rig.auth.sign('start', request)


@pytest.mark.asyncio
async def test_handoff_completes_once_and_host_slot_stays_reserved(rig):
    job = submit(rig)
    result = await rig.bridge.run_once()
    assert result['id'] == job['id'] and result['state'] == 'succeeded'
    assert reservation(rig) is None and releases(rig, job['id']) == 1
    assert handoff(rig, job['id'])['state'] == 'finalized'
    assert host_state(rig) == ['finalized_reserved'] and len(rig.runner.calls) == 1
    with rig.repo._connection() as db:
        assert db.execute('SELECT state FROM hub_development_ci_attempts WHERE job_id=?',
                          (job['id'],)).fetchone()[0] == 'handoff'
    assert await rig.bridge.run_once() is None and len(rig.runner.calls) == 1


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
        host.start(*start_request(rig, 99))
    assert host_rows(rig) == before and len(rig.runner.calls) == 1
    # The Hub re-inspects read-only and keeps the job/machine held.
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert reservation(rig) == job['id'] and len(rig.runner.calls) == 1


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
async def test_crash_between_host_ack_and_hub_record_resumes_once_without_new_start(rig, monkeypatch):
    first = submit(rig, 'first')
    second = submit(rig, 'second')
    complete = rig.queue.complete
    def crash(**kwargs):
        if kwargs['outcome'] != 'held':
            raise RuntimeError('hub crashed after host ack')
        return complete(**kwargs)
    monkeypatch.setattr(rig.queue, 'complete', crash)
    assert (await rig.bridge.run_once())['state'] == 'held'
    assert host_state(rig) == ['finalized_reserved']
    assert handoff(rig, first['id'])['state'] == 'receipt_committed'
    stored_ack = handoff(rig, first['id'])['hub_ack']
    # Restarted Hub whose host channel is temporarily unavailable: no new start.
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
    # Only after release can the next job start, with a strictly higher fence.
    result = await rig.bridge.run_once()
    assert result['id'] == second['id'] and result['state'] == 'succeeded'
    assert [call['fence'] for call in rig.runner.calls] == [1, 2]
    assert releases(rig, first['id']) == 1


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
            rig.host.start(*start_request(rig, 50, **change))
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
    from engineering_hub.development_ci_handoff import receipt_digest
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


def test_legacy_controller_and_legacy_queue_composition_refuse_while_enrolled(rig, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'local_development_ci.py'
    spec = importlib.util.spec_from_file_location('local_development_ci', path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert (rig.engine / HANDOFF_MARKER).exists()
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
    assert (rig.engine / HANDOFF_MARKER).exists()
    restart_hub(rig, compose=False)
    class Provider:
        async def invoke(self, attempt, guard): pytest.fail('legacy provider invoked')
        async def verify_terminal(self, attempt, receipt): pytest.fail('legacy provider invoked')
    with pytest.raises(HubError, match='development_ci_handoff_enrolled'):
        QueuedDevelopmentCI(rig.queue, rig.owner, rig.manifest, Provider(), action_key=ACTION)


@pytest.mark.asyncio
async def test_direct_finalize_or_release_writes_are_aborted_by_triggers(rig):
    job = submit(rig)
    rig.runner.fail = True
    await rig.bridge.run_once()
    assert host_state(rig) == ['running']
    db = sqlite3.connect(rig.journal)
    try:
        for statement, values in (
                ("UPDATE handoff_attempts SET state='finalized_reserved'", ()),
                ("UPDATE handoff_attempts SET state='finalized_reserved',hub_ack=?", ('b' * 64,)),
                ("UPDATE handoff_attempts SET state='terminal_held'", ()),
                ("UPDATE handoff_attempts SET fence=fence+10", ()),
                ('DELETE FROM handoff_attempts', ()),
                ("UPDATE handoff_meta SET floor_fence=-1", ())):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement, values)
        db.rollback()
    finally:
        db.close()
    assert host_state(rig) == ['running']
    # Hub ledger: the queue cannot release a handoff job without a committed receipt.
    with pytest.raises(sqlite3.IntegrityError, match='development_ci_handoff_unfinalized'):
        with rig.repo._transaction() as hub:
            hub.execute("UPDATE hub_work_jobs SET state='succeeded' WHERE id=?", (job['id'],))
    assert reservation(rig) == job['id']


@pytest.mark.asyncio
async def test_finalized_reserved_without_prior_terminal_hub_ack_is_aborted(rig):
    submit(rig)
    rig.channel.block.add('finalize')
    await rig.bridge.run_once()
    assert host_state(rig) == ['terminal_held']
    db = sqlite3.connect(rig.journal)
    try:
        with pytest.raises(sqlite3.IntegrityError, match='handoff_transition_refused'):
            db.execute("UPDATE handoff_attempts SET state='finalized_reserved'")
        with pytest.raises(sqlite3.IntegrityError, match='handoff_transition_refused'):
            db.execute("UPDATE handoff_attempts SET state='finalized_reserved',hub_ack='short'")
        db.rollback()
    finally:
        db.close()
    assert host_state(rig) == ['terminal_held']


@pytest.mark.asyncio
async def test_restored_journal_below_anchor_refuses_until_operator_reconcile(rig):
    submit(rig)
    await rig.bridge.run_once()
    assert json.loads(rig.anchor.read_text())['highest_fence'] == 1
    shutil.copy(rig.tmp / 'journal-backup.sqlite3', rig.journal)  # restore older journal
    with pytest.raises(HandoffRefused, match='handoff_journal_restored'):
        rig.host.start(*start_request(rig, 5))
    with pytest.raises(HandoffRefused, match='requires_operator'):
        reconcile_restored_journal(rig.journal, rig.anchor)
    assert len(rig.runner.calls) == 1
    reconcile_restored_journal(rig.journal, rig.anchor, operator_confirmed=True)
    with pytest.raises(HandoffRefused, match='handoff_fence_stale'):
        rig.host.start(*start_request(rig, 1))
    assert rig.host.start(*start_request(rig, 5))[0]['state'] == 'terminal_held'
    assert len(rig.runner.calls) == 2


@pytest.mark.parametrize('missing', ['anchor', 'marker'])
def test_unknown_anchor_or_missing_writer_exclusion_refuses_admission(rig, missing):
    (rig.anchor if missing == 'anchor' else rig.engine / HANDOFF_MARKER).unlink()
    with pytest.raises(HandoffRefused, match='anchor_unknown|writer_exclusion_missing'):
        rig.host.start(*start_request(rig, 1))
    assert host_rows(rig) == [] and not rig.runner.calls


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
async def test_disabled_by_default(rig):
    with pytest.raises(HandoffRefused, match='handoff_disabled'):
        make_host(rig, enable=False)
    with pytest.raises(HandoffRefused, match='handoff_disabled'):
        enroll_host(rig.tmp / 'j2', rig.tmp / 'a2', rig.engine)
    restart_hub(rig, compose=False)
    with pytest.raises(HubError, match='development_ci_handoff_disabled'):
        make_bridge(rig, rig.queue, rig.owner, enable=False)
    assert getattr(rig.repo, '_development_ci', None) is None
    assert rig.queue.completion_verifier is None and rig.queue.reconciliation_verifier is None
