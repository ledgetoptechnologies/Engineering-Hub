"""Disposable Linux cold-ledger enrollment, never NAS or host activation."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import uuid

import pytest

from engineering_hub import pilot_enrollment as enrollment
from engineering_hub.auth import AuthConfig, SessionStore
from engineering_hub.cloudflare_access import CloudflareIdentity
from engineering_hub.outbox import HubOutboxPublisher
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError, _local_filesystem
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux offline ownership enrollment')


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    # Explicit disposable Linux overlay/tmpfs only; utility exposes no bypass.
    monkeypatch.setattr(enrollment, '_local_filesystem', lambda fd: _local_filesystem(fd, allow_ephemeral_test_storage=True))
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'hub.sqlite3'
    repo = HubRepository(path)
    repo.create_task('human:42', 'engineering-hub', 'Preserve task', 'preserved-request')
    repo.create_decision('codex', 'engineering-hub', 'Preserve question', ['Yes', 'No'])
    store = SessionStore(path, repository=repo)
    session = store.create_session(42)
    with repo._transaction() as db:
        db.execute('INSERT INTO auth_states VALUES(?,?,?)', ('statehash', 'PRIVATE-VERIFIER', 9999999999))
        db.execute('INSERT INTO hub_target_fences VALUES(?,?)', ('engineering-hub:staging', 51))
        db.execute('PRAGMA user_version=17')
        db.execute('PRAGMA application_id=12345')
    path.chmod(0o600)
    assert not any(root.glob('*-wal'))
    return path, repo, store, session


def snapshot(path):
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        return enrollment._snapshot(db)


def test_plan_is_read_only_fingerprints_no_secrets_and_apply_preserves_every_existing_row(ledger):
    path, legacy, store, session = ledger
    before = path.read_bytes()
    listing = sorted(p.name for p in path.parent.iterdir())
    plan = enrollment.inspect_plan(path)
    encoded = enrollment.canonical_json(plan)
    assert 'PRIVATE-VERIFIER' not in encoded and session['cookie_token'] not in encoded
    assert path.read_bytes() == before and sorted(p.name for p in path.parent.iterdir()) == listing
    assert plan['content_sha256'] == hashlib.sha256(before).hexdigest()
    result = enrollment.apply_plan(plan, enrollment.plan_digest(plan), writers_stopped=True, backup_verified=True)
    assert result['status'] == 'enrolled' and result['runtime_activated'] is False
    with sqlite3.connect(path) as db:
        assert enrollment._snapshot(db, enrolled=True) == plan['snapshot']
        assert db.execute('SELECT generation FROM hub_target_fences').fetchone()[0] == 51
        assert db.execute('SELECT version,database_id,epoch FROM hub_pilot_owner').fetchone() == (1, result['database_id'], result['epoch'])
    # Enrollment only fences cooperating repository writers, not arbitrary SQL.
    with pytest.raises(HubError, match='pilot_ownership_required'):
        legacy.create_task('human:42', 'engineering-hub', 'Forbidden late write', 'late')
    with pytest.raises(HubError, match='pilot_ownership_required'):
        store.create_session(42)
    with PilotOwner(path, require_existing=True, allow_ephemeral_test_storage=True) as owner:
        assert owner.previous_epoch == result['epoch']
        owned = HubRepository(path, pilot_owner=owner)
        assert len(owned.list_tasks()) == len(owned.list_decisions()) == 1
        assert owned.create_task('human:42', 'engineering-hub', 'Preserve task', 'preserved-request')['title'] == 'Preserve task'
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.apply_plan(plan, enrollment.plan_digest(plan), writers_stopped=True, backup_verified=True)


@pytest.mark.asyncio
async def test_cold_copy_preserves_real_auth_queue_and_delivery_history_after_owned_reopen(ledger):
    # All constructors and traffic below are local disposable fixtures. This
    # exercises enrollment compatibility, not live Access verification, bus
    # delivery, job execution, or a production state-migration procedure.
    source, repo, _, github_session = ledger
    task = repo.list_tasks()[0]
    config = AuthConfig(
        base_url='https://hub.example.com', human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://disposable-test.cloudflareaccess.com',
        cloudflare_audience='disposable-test-audience',
        cloudflare_allowed_emails=('beau@example.com',))
    identity = CloudflareIdentity('test-subject', 'beau@example.com', 'human:42', 99999)
    sessions = SessionStore(source, repository=repo, clock=lambda: 1000)
    access_session = sessions.create_cloudflare_session(config, identity)
    expected_access = sessions.cloudflare_session_for(access_session['cookie_token'], config, identity)
    assert expected_access['actor'] == identity.actor
    assert expected_access['csrf_token'] == access_session['csrf_token']
    actions = {'validate': WorkAction('disposable-machine', project='engineering-hub',
                                    requires_execution_proof=False)}
    queue = WorkQueue(repo, actions, clock=lambda: 1000, target_verifier=lambda *args: True)
    worker = WorkWorker('codex', str(uuid.uuid4()))
    target = WorkTarget(task['id'], 'a' * 40, 'sha256:' + 'b' * 64)
    terminal_jobs = []
    for outcome in ('succeeded', 'failed'):
        submitted = queue.submit(actor='codex', request_id='history-' + outcome,
                                 action_key='validate', environment='staging', target=target)
        claimed = queue.claim(worker=worker)
        assert claimed['id'] == submitted['id']
        terminal_jobs.append(queue.complete(job_id=claimed['id'], worker=worker,
                                            fence=claimed['fence'], outcome=outcome))
    expected_queue = queue.status()

    class DisposableBus:
        def __init__(self):
            self.sent = []

        async def bus_info(self, **kwargs):
            return {'native_push': {'inbox_uri': 'agentbus://inbox/codex'}}

        async def send_message(self, **payload):
            self.sent.append(payload)
            return {'id': str(uuid.uuid4())}

    bus = DisposableBus()
    publisher = HubOutboxPublisher(repo, bus, recipients=('hermes',))
    delivered = await publisher.drain_batch()
    assert delivered == {'accepted': len(bus.sent), 'failed': 0, 'pending': 0, 'held_reason': None}
    assert delivered['accepted'] > 0
    before = snapshot(source)
    source_bytes = source.read_bytes()
    assert not any(source.with_name(source.name + suffix).exists()
                   for suffix in ('-wal', '-shm', '-journal'))
    copied_root = source.parent.parent / 'cold-copy-private'
    copied_root.mkdir(mode=0o700)
    copied = copied_root / 'hub.sqlite3'
    shutil.copyfile(source, copied)
    copied.chmod(0o600)
    assert copied.read_bytes() == source_bytes
    plan = enrollment.inspect_plan(copied)
    assert plan['snapshot'] == before
    result = enrollment.apply_plan(plan, enrollment.plan_digest(plan),
                                   writers_stopped=True, backup_verified=True)
    with PilotOwner(copied, require_existing=True, allow_ephemeral_test_storage=True) as owner:
        owned = HubRepository(copied, pilot_owner=owner)
        owned_sessions = SessionStore(copied, repository=owned, clock=lambda: 1000)
        owned_queue = WorkQueue(owned, actions, clock=lambda: 1000,
                                target_verifier=lambda *args: True)
        owned_publisher = HubOutboxPublisher(owned, bus, recipients=('hermes',))
        assert owner.previous_epoch == result['epoch']
        assert owned.list_tasks() == repo.list_tasks()
        assert owned.list_decisions() == repo.list_decisions()
        assert owned.create_task('human:42', 'engineering-hub', 'Preserve task',
                                 'preserved-request') == task
        assert owned_sessions.session_for(github_session['cookie_token'])['github_id'] == 42
        assert owned_sessions.cloudflare_session_for(
            access_session['cookie_token'], config, identity) == expected_access
        changed_identity = CloudflareIdentity('different-subject', identity.email, identity.actor, 99999)
        assert owned_sessions.cloudflare_session_for(
            access_session['cookie_token'], config, changed_identity) is None
        assert owned_queue.status() == expected_queue
        for job in terminal_jobs:
            assert owned_queue.get_job(job['id'])['state'] == job['state']
            assert owned_queue.submit(actor='codex', request_id='history-' + job['state'],
                                      action_key='validate', environment='staging', target=target) == job
        sent_before = len(bus.sent)
        assert await owned_publisher.drain_batch() == {
            'accepted': 0, 'failed': 0, 'pending': 0, 'held_reason': None}
        assert len(bus.sent) == sent_before
        with owned._connection() as db:
            assert db.execute('SELECT generation FROM hub_target_fences').fetchone()[0] == 51
            assert db.execute('SELECT generation,job_id FROM hub_work_machines').fetchone()[:] == (2, None)
            assert db.execute('SELECT COUNT(*) FROM auth_access_identities').fetchone()[0] == 1
        with closing(sqlite3.connect(copied)) as db:
            assert enrollment._snapshot(db, enrolled=True) == before
    assert source.read_bytes() == source_bytes
    assert snapshot(source) == before


@pytest.mark.parametrize('changed', ['no-attestations', 'bad-digest', 'extra-key', 'content', 'replacement'])
def test_exact_plan_and_operator_attestations_required_without_creating_artifacts(ledger, changed):
    path, repo, *_ = ledger
    plan = enrollment.inspect_plan(path)
    digest = enrollment.plan_digest(plan)
    attest = dict(writers_stopped=True, backup_verified=True)
    if changed == 'no-attestations':
        attest = {}
    elif changed == 'bad-digest':
        digest = '0' * 64
    elif changed == 'extra-key':
        plan = {**plan, 'unexpected': 'refuse'}
        digest = enrollment.plan_digest(plan)
    elif changed == 'content':
        repo.create_decision('claude', 'engineering-hub', 'New question', ['Yes', 'No'])
    elif changed == 'replacement':
        original = path.read_bytes()
        path.rename(path.with_name('original-retained'))
        path.write_bytes(original)
        path.chmod(0o600)
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.apply_plan(plan, digest, **attest)
    assert not path.with_name(path.name + '.pilot-owner.lock').exists()


@pytest.mark.parametrize('hazard', ['wal', 'shm', 'journal', 'lock', 'hardlink', 'symlink', 'database-mode', 'parent-mode', 'unknown-table', 'trigger', 'partial-auth', 'owner-marker', 'active-run', 'queued-work', 'held-work', 'reserved-machine'])
def test_plan_rejects_unsafe_or_nonquiescent_ledgers(ledger, hazard):
    path, repo, *_ = ledger
    if hazard in {'wal', 'shm', 'journal'}:
        path.with_name(path.name + '-' + hazard).touch()
    elif hazard == 'lock':
        path.with_name(path.name + '.pilot-owner.lock').touch(mode=0o600)
    elif hazard == 'hardlink':
        os.link(path, path.with_name('second-link'))
    elif hazard == 'symlink':
        target = path.with_name('real-db')
        path.rename(target)
        path.symlink_to(target)
    elif hazard == 'database-mode':
        path.chmod(0o640)
    elif hazard == 'parent-mode':
        path.parent.chmod(0o750)
    elif hazard in {'unknown-table', 'trigger', 'partial-auth', 'owner-marker', 'active-run'}:
        with sqlite3.connect(path) as db:
            if hazard == 'unknown-table':
                db.execute('CREATE TABLE unknown (id TEXT)')
            elif hazard == 'trigger':
                db.execute('CREATE TRIGGER extra AFTER INSERT ON hub_requests BEGIN SELECT 1; END')
            elif hazard == 'partial-auth':
                db.execute(enrollment._DDL['auth_access_sessions'])
            elif hazard == 'owner-marker':
                db.execute('CREATE TABLE hub_pilot_owner (id INTEGER)')
            else:
                db.execute('INSERT INTO hub_entities VALUES(?,?,?)', ('uncertain', 'run', json.dumps({'status': 'held'})))
    else:
        WorkQueue(repo, {'validate': WorkAction('staging-pc', project='engineering-hub')})
        with repo._transaction() as db:
            if hazard == 'reserved-machine':
                db.execute('INSERT INTO hub_work_machines VALUES(?,?,?)', ('staging-pc', 9, 'unresolved-job'))
            else:
                db.execute('''INSERT INTO hub_work_jobs(id,actor,request_id,fingerprint,action_key,environment,machine_id,project,target,requires_execution_proof,state,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', ('job', 'codex', 'request', 'fingerprint', 'validate', 'staging', 'staging-pc', 'engineering-hub', '{}', 1, 'held' if hazard == 'held-work' else 'queued', 1))
    before = path.read_bytes()
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(path)
    assert path.read_bytes() == before


def test_interruption_rolls_back_only_owner_tables_and_preserves_lock_for_inspection(ledger, monkeypatch):
    path, *_ = ledger
    plan = enrollment.inspect_plan(path)
    original = enrollment._snapshot

    def interrupted(db, *, enrolled=False):
        if enrolled:
            raise RuntimeError('synthetic interruption after owner inserts')
        return original(db)

    monkeypatch.setattr(enrollment, '_snapshot', interrupted)
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.apply_plan(plan, enrollment.plan_digest(plan), writers_stopped=True, backup_verified=True)
    assert path.with_name(path.name + '.pilot-owner.lock').exists()
    assert snapshot(path) == plan['snapshot']
    with pytest.raises(PilotOwnershipError):
        PilotOwner(path, require_existing=True, allow_ephemeral_test_storage=True)
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(path)


def test_cli_plan_and_failure_output_do_not_expose_values(ledger, capsys, tmp_path):
    path, *_ = ledger
    assert enrollment.main(['inspect', '--database', str(path)]) == 0
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert set(document) == {'plan', 'digest'} and 'PRIVATE-VERIFIER' not in output.out
    plan_file = tmp_path / 'plan.json'
    plan_file.write_text(enrollment.canonical_json(document))
    assert enrollment.main(['apply', '--plan', str(plan_file), '--expected-digest', document['digest']]) == 1
    failure = capsys.readouterr()
    assert not failure.out and failure.err.strip() == str(enrollment.EnrollmentError())


@pytest.mark.parametrize('ancestor_mode', [0o777, 0o770])
def test_unsafe_nonsticky_ancestor_refused_even_with_private_leaf(tmp_path, monkeypatch, ancestor_mode):
    monkeypatch.setattr(enrollment, '_local_filesystem', lambda fd: None)
    unsafe = tmp_path / 'unsafe'
    unsafe.mkdir(mode=ancestor_mode)
    unsafe.chmod(ancestor_mode)
    parent = unsafe / 'private'
    parent.mkdir(mode=0o700)
    path = parent / 'hub.sqlite3'
    HubRepository(path)
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(path)
    assert path.read_bytes() == before and sorted(item.name for item in parent.iterdir()) == ['hub.sqlite3']


def test_no_follow_ancestor_and_changed_ancestor_mode_refused(ledger):
    path, *_ = ledger
    link = path.parent.parent / 'alias'
    link.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(link / path.name)
    plan = enrollment.inspect_plan(path)
    path.parent.parent.chmod(0o777)
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.apply_plan(plan, enrollment.plan_digest(plan), writers_stopped=True, backup_verified=True)
    assert not path.with_name(path.name + '.pilot-owner.lock').exists()


def test_pending_mail_acceptance_refused_but_confirmed_history_preserved(ledger):
    path, repo, *_ = ledger
    with repo._transaction() as db:
        for name in ('hub_delivery_namespace', 'hub_delivery_receipts', 'hub_delivery_sender'):
            db.execute(enrollment._DDL[name])
        db.execute('INSERT INTO hub_delivery_receipts VALUES(?,?,?,?)', (1, 'claude', '{"private":"never emit"}', None))
    before = path.read_bytes()
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(path)
    assert path.read_bytes() == before
    with repo._transaction() as db:
        db.execute('UPDATE hub_delivery_receipts SET message_id=?', ('12345678-1234-1234-1234-123456789abc',))
    plan = enrollment.inspect_plan(path)
    receipt = enrollment.apply_plan(plan, enrollment.plan_digest(plan), writers_stopped=True, backup_verified=True)
    assert receipt['prior_snapshot']['tables']['hub_delivery_receipts']['rows'] == 1


@pytest.mark.parametrize('budget', ['file', 'rows', 'cell', 'logical'])
def test_scan_budgets_refuse_without_creation_or_partial_enrollment(ledger, monkeypatch, budget):
    path, *_ = ledger
    limit = {'file': '_MAX_DB_BYTES', 'rows': '_MAX_ROWS', 'cell': '_MAX_CELL_BYTES', 'logical': '_MAX_LOGICAL_BYTES'}[budget]
    monkeypatch.setattr(enrollment, limit, 1)
    before = path.read_bytes()
    listing = sorted(item.name for item in path.parent.iterdir())
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(path)
    assert path.read_bytes() == before and sorted(item.name for item in path.parent.iterdir()) == listing


def test_inspection_connection_forces_memory_temp_store(ledger, monkeypatch):
    path, *_ = ledger
    original = enrollment._snapshot

    def observed(db, **kwargs):
        assert db.execute('PRAGMA temp_store').fetchone()[0] == 2
        return original(db, **kwargs)

    monkeypatch.setattr(enrollment, '_snapshot', observed)
    before = sorted(item.name for item in path.parent.iterdir())
    enrollment.inspect_plan(path)
    assert sorted(item.name for item in path.parent.iterdir()) == before


def test_actual_process_exit_after_owner_inserts_preserves_partial_artifacts(ledger):
    path, *_ = ledger
    plan = enrollment.inspect_plan(path)
    script = '''
import os, sys
from engineering_hub import pilot_enrollment as e
from engineering_hub.pilot_ownership import _local_filesystem
e._local_filesystem = lambda fd: _local_filesystem(fd, allow_ephemeral_test_storage=True)
original = e._snapshot
def abrupt(db, *, enrolled=False):
    if enrolled:
        os._exit(23)
    return original(db)
e._snapshot = abrupt
plan = e.inspect_plan(sys.argv[1])
e.apply_plan(plan, e.plan_digest(plan), writers_stopped=True, backup_verified=True)
'''
    result = subprocess.run([sys.executable, '-c', script, str(path)],
                            capture_output=True, timeout=15)
    assert result.returncode == 23 and not result.stdout
    assert path.with_name(path.name + '.pilot-owner.lock').exists()
    assert snapshot(path) == plan['snapshot']
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(path)


def test_cleanup_attempts_lock_close_even_if_database_close_raises(ledger, monkeypatch):
    path, *_ = ledger
    plan = enrollment.inspect_plan(path)
    connect, open_fd, close_fd = sqlite3.connect, os.open, os.close
    locks, closed = [], []

    class BrokenClose:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.connection.close()
            raise OSError('synthetic close failure')

    def connecting(value, *args, **kwargs):
        connection = connect(value, *args, **kwargs)
        return BrokenClose(connection) if '?mode=rw' in str(value) else connection

    def opening(value, *args, **kwargs):
        fd = open_fd(value, *args, **kwargs)
        if str(value).endswith('.pilot-owner.lock'):
            locks.append(fd)
        return fd

    def closing(fd):
        closed.append(fd)
        return close_fd(fd)

    monkeypatch.setattr(enrollment.sqlite3, 'connect', connecting)
    monkeypatch.setattr(enrollment.os, 'open', opening)
    monkeypatch.setattr(enrollment.os, 'close', closing)
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.apply_plan(plan, enrollment.plan_digest(plan), writers_stopped=True, backup_verified=True)
    assert len(locks) == 1 and locks[0] in closed
    with pytest.raises(OSError):
        os.fstat(locks[0])


def test_committed_but_response_lost_is_recognized_without_reenrollment(ledger):
    path, *_ = ledger
    script = '''
import os, sys
from engineering_hub import pilot_enrollment as e
from engineering_hub.pilot_ownership import _local_filesystem
e._local_filesystem = lambda fd: _local_filesystem(fd, allow_ephemeral_test_storage=True)
original, count = os.fsync, 0
def abrupt_after_commit(fd):
    global count
    count += 1
    if count == 3:
        os._exit(24)
    return original(fd)
e.os.fsync = abrupt_after_commit
plan = e.inspect_plan(sys.argv[1])
e.apply_plan(plan, e.plan_digest(plan), writers_stopped=True, backup_verified=True)
'''
    result = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, timeout=15)
    assert result.returncode == 24 and not result.stdout
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(path)
    with PilotOwner(path, require_existing=True, allow_ephemeral_test_storage=True) as owner:
        owner.assert_active(path)
        repo = HubRepository(path, pilot_owner=owner)
        assert len(repo.list_tasks()) == len(repo.list_decisions()) == 1
