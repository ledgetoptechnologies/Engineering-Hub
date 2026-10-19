"""Real SQLite/queue, mocked host observations: NOT real Docker acceptance."""
from dataclasses import replace
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker
from hub_validation_worker.core import (ValidationEnrollment, ValidationError,
    ValidationJob, ValidationLedger, ValidationObservation, ValidationProfile, ValidationWorker)


COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
IMAGE = 'sha256:' + 'c' * 64


class FakeBackend:
    """Mock only: makes no host effect and proves no container isolation."""
    def __init__(self):
        self.calls = []
        self.hook = None
        self.observation = None

    def execute(self, enrollment, job, intent_id, *, deadline_monotonic):
        self.calls.append((enrollment, job, intent_id, deadline_monotonic))
        if self.hook:
            self.hook(enrollment, job, intent_id)
        if isinstance(self.observation, Exception):
            raise self.observation
        return self.observation or ValidationObservation('d' * 64, IMAGE, enrollment.profile.digest(), 0, True)


@pytest.fixture
def rig(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    clock = [1000.0]
    monotonic = [2000.0]
    queue = WorkQueue(repo, {'engineering-hub.validate': WorkAction('staging-pc', project='engineering-hub')},
                      target_verifier=lambda *args: True, clock=lambda: clock[0])
    private = tmp_path / 'receipts'
    private.mkdir(mode=0o700)
    ledger = ValidationLedger(private / 'validation.sqlite3', create=True,
                               allow_windows_test_storage=os.name != 'posix')
    queue.completion_verifier = ledger.verify_completion
    enrollment = ValidationEnrollment('engineering-hub.validate', 'engineering-hub', 'staging-pc',
                                     COMMIT, DIGEST, IMAGE, 'linux/amd64',
                                     ('python', '-m', 'pytest', '-q'), ValidationProfile(), 10)
    backend = FakeBackend()
    worker = ValidationWorker(queue, ledger, backend, (enrollment,), monotonic=lambda: monotonic[0])
    instance = WorkWorker('codex', str(uuid4()))
    job = queue.submit(actor='codex', request_id=str(uuid4()), action_key=enrollment.action_key,
                       environment='staging', target=WorkTarget(str(uuid4()), COMMIT, DIGEST))
    claim = queue.claim(worker=instance, lease_seconds=60)
    return repo, queue, ledger, enrollment, backend, worker, instance, claim, clock, monotonic


def run(rig):
    _, _, _, _, _, worker, instance, claim, *_ = rig
    return worker.run_once(job_id=claim['id'], worker=instance, fence=claim['fence'])


def test_durable_intent_before_backend_and_verified_completion(rig):
    _, queue, ledger, enrollment, backend, _, _, claim, *_ = rig
    def check_intent(enrolled, job, intent_id):
        reopened = ValidationLedger(ledger.path, create=False, allow_windows_test_storage=os.name != 'posix')
        receipt = reopened.inspect(job_id=job.job_id, fence=job.fence)
        assert receipt['state'] == 'intent' and receipt['intent_id'] == intent_id
        # Proves SQLite writer transaction is not retained across host execution.
        queue.set_paused(False)
    backend.hook = check_intent
    result = run(rig)
    assert result['state'] == 'succeeded'
    assert len(backend.calls) == 1
    assert ledger.verify_completion(claim, result['evidence_id'], 'succeeded')
    assert queue.status()['jobs'][0]['state'] == 'succeeded'
    assert backend.calls[0][3] == 2010


def test_nonzero_exit_is_verified_failure_not_success(rig):
    _, queue, ledger, enrollment, backend, *_ = rig
    backend.observation = ValidationObservation('d' * 64, IMAGE, enrollment.profile.digest(), 3, True)
    result = run(rig)
    claim = rig[7]
    assert result['state'] == 'failed'
    assert ledger.verify_completion(claim, result['evidence_id'], 'failed')
    assert not ledger.verify_completion(claim, result['evidence_id'], 'succeeded')
    assert queue.status()['jobs'][0]['state'] == 'failed'


@pytest.mark.parametrize('case', ['exception', 'running', 'wrong_image', 'wrong_profile', 'deadline', 'lease_expired', 'pause', 'review_changed'])
def test_uncertainty_holds_machine_no_replay(rig, case):
    _, queue, ledger, enrollment, backend, _, _, claim, clock, mono = rig
    if case == 'exception':
        backend.observation = RuntimeError('secret exception must not escape')
    elif case == 'running':
        backend.observation = ValidationObservation('d' * 64, IMAGE, enrollment.profile.digest(), 0, False)
    elif case == 'wrong_image':
        backend.observation = ValidationObservation('d' * 64, DIGEST, enrollment.profile.digest(), 0, True)
    elif case == 'wrong_profile':
        backend.observation = ValidationObservation('d' * 64, IMAGE, DIGEST, 0, True)
    elif case == 'deadline':
        backend.hook = lambda *args: mono.__setitem__(0, 2010)
    elif case == 'lease_expired':
        backend.hook = lambda *args: clock.__setitem__(0, 1060)
    elif case == 'pause':
        backend.hook = lambda *args: queue.set_paused(True)
    else:
        backend.hook = lambda *args: setattr(queue, 'target_verifier', lambda *args: False)
    assert run(rig)['state'] == 'held'
    assert ledger.inspect(job_id=claim['id'], fence=claim['fence'])['state'] == 'held'
    with pytest.raises((ValidationError, HubError)):
        run(rig)
    assert len(backend.calls) == 1
    assert queue.claim(worker=WorkWorker('hermes', str(uuid4()))) is None


def test_preexisting_intent_reopen_never_replays(rig):
    _, queue, ledger, enrollment, backend, _, instance, claim, *_ = rig
    job = ValidationJob.from_claim(claim)
    ledger._begin(job, enrollment)  # Simulates controller crash after durable admission.
    reopened = ValidationLedger(ledger.path, create=False, allow_windows_test_storage=os.name != 'posix')
    queue.completion_verifier = reopened.verify_completion
    worker = ValidationWorker(queue, reopened, backend, (enrollment,))
    with pytest.raises(ValidationError, match='validation_intent_already_exists'):
        worker.run_once(job_id=job.job_id, worker=instance, fence=job.fence)
    assert backend.calls == []
    assert reopened.inspect(job_id=job.job_id, fence=job.fence)['state'] == 'intent'


def test_live_claim_checked_again_after_intent_before_backend(rig, monkeypatch):
    _, queue, ledger, _, backend, *_ = rig
    begin = ledger._begin
    def pause_after_intent(*args):
        identity = begin(*args)
        queue.set_paused(True)
        return identity
    monkeypatch.setattr(ledger, '_begin', pause_after_intent)
    assert run(rig)['state'] == 'held'
    assert backend.calls == []


def test_bad_instance_denied_before_intent(rig):
    _, _, ledger, _, backend, worker, _, claim, *_ = rig
    with pytest.raises(HubError, match='work_ownership_lost'):
        worker.run_once(job_id=claim['id'], worker=WorkWorker('codex', str(uuid4())), fence=claim['fence'])
    assert ledger.inspect(job_id=claim['id'], fence=claim['fence']) is None
    assert backend.calls == []


def test_insufficient_lease_and_wrong_enrollment_denied_before_effect(rig):
    _, queue, ledger, enrollment, backend, worker, instance, claim, clock, _ = rig
    clock[0] = 1045
    with pytest.raises(ValidationError, match='validation_lease_budget_required'):
        run(rig)
    clock[0] = 1000
    worker.enrollments = {enrollment.action_key: replace(enrollment, source_commit='e' * 40)}
    with pytest.raises(ValidationError, match='validation_enrollment_mismatch'):
        run(rig)
    assert backend.calls == []
    assert ledger.inspect(job_id=claim['id'], fence=claim['fence']) is None


def test_completion_receipt_exact_target_instance_fence_evidence(rig):
    _, _, ledger, _, _, _, _, claim, *_ = rig
    result = run(rig)
    for changes in ({'fence': claim['fence'] + 1}, {'id': str(uuid4())},
                    {'worker': 'codex:' + str(uuid4())}, {'action_key': 'other.validate'},
                    {'machine_id': 'other'}, {'environment': 'production'},
                    {'target': {**claim['target'], 'source_commit': 'e' * 40}}):
        assert not ledger.verify_completion({**claim, **changes}, result['evidence_id'], 'succeeded')
    assert not ledger.verify_completion(claim, str(uuid4()), 'succeeded')


def test_receipt_verifier_binding_cannot_be_worker_assertion(rig):
    _, queue, ledger, enrollment, backend, *_ = rig
    queue.completion_verifier = lambda *args: True
    with pytest.raises(ValidationError, match='validation_receipt_verifier_required'):
        ValidationWorker(queue, ledger, backend, (enrollment,))


@pytest.mark.parametrize('changes', [{'uid': 0}, {'uid': 1000}, {'gid': 0}, {'memory_bytes': True}, {'cpu_millicores': 99999}, {'tmpfs_bytes': 9999999999}])
def test_strict_validation_profile(changes):
    with pytest.raises(ValidationError, match='invalid_validation_profile'):
        ValidationProfile(**changes)


@pytest.mark.parametrize('changes', [{'timeout_seconds': 301}, {'timeout_seconds': True}, {'argv': ['python']}, {'argv': ('bad\ncommand',)}, {'platform': 'windows/amd64'}, {'source_commit': 'A' * 40}])
def test_strict_trusted_enrollment(rig, changes):
    with pytest.raises(ValidationError, match='invalid_validation_enrollment'):
        replace(rig[3], **changes)


def test_arbitrary_existing_database_not_adopted(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'other.sqlite3'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE unrelated (value TEXT)')
    db.commit()
    db.close()
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises((ValidationError, sqlite3.DatabaseError)):
        ValidationLedger(path, create=False, allow_windows_test_storage=os.name != 'posix')
    assert path.read_bytes() == before


@pytest.mark.skipif(os.name != 'posix', reason='real POSIX private permissions')
def test_private_permissions_and_replacement_denied(rig):
    ledger = rig[2]
    ledger.path.chmod(0o644)
    with pytest.raises(ValidationError, match='validation_storage_changed'):
        ledger.inspect(job_id=rig[7]['id'], fence=rig[7]['fence'])
    ledger.path.chmod(0o600)
    old = ledger.path.with_suffix('.previous')
    ledger.path.rename(old)
    ledger.path.write_bytes(old.read_bytes())
    ledger.path.chmod(0o600)
    with pytest.raises(ValidationError, match='validation_storage_changed'):
        ledger.inspect(job_id=rig[7]['id'], fence=rig[7]['fence'])


@pytest.mark.parametrize('terminal', [False, True])
def test_committed_intent_and_receipt_survive_process_exit(rig, terminal):
    """Actual abrupt process exit; observation remains synthetic, not Docker."""
    _, _, ledger, enrollment, _, _, _, claim, *_ = rig
    job = ValidationJob.from_claim(claim)
    script = '''
import json, os, sys
from engineering_hub.work_queue import WorkTarget, WorkWorker
from hub_validation_worker.core import ValidationLedger, ValidationEnrollment, ValidationProfile, ValidationJob, ValidationObservation
data = json.load(sys.stdin)
raw = data['job']
raw['target'] = WorkTarget(**raw['target'])
raw['worker'] = WorkWorker(**raw['worker'])
job = ValidationJob(**raw)
raw = data['enrollment']
raw['argv'] = tuple(raw['argv'])
raw['profile'] = ValidationProfile(**raw['profile'])
enrollment = ValidationEnrollment(**raw)
ledger = ValidationLedger(data['path'], create=False, allow_windows_test_storage=os.name != 'posix')
intent = ledger._begin(job, enrollment)
if data['terminal']:
    ledger._record(job, enrollment, intent, ValidationObservation('d'*64, enrollment.image_config_id, enrollment.profile.digest(), 0, True))
os._exit(17)
'''
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps({
        'path': str(ledger.path), 'job': asdict(job), 'enrollment': asdict(enrollment),
        'terminal': terminal}), text=True, capture_output=True, timeout=10)
    assert result.returncode == 17
    assert result.stdout == '' and result.stderr == ''
    reopened = ValidationLedger(ledger.path, create=False, allow_windows_test_storage=os.name != 'posix')
    receipt = reopened.inspect(job_id=job.job_id, fence=job.fence)
    assert receipt['state'] == ('terminal' if terminal else 'intent')
    if terminal:
        assert reopened.verify_completion(claim, receipt['evidence_id'], 'succeeded')
    else:
        assert not reopened.verify_completion(claim, str(uuid4()), 'succeeded')
    with pytest.raises(ValidationError, match='validation_intent_already_exists'):
        reopened._begin(job, enrollment)
