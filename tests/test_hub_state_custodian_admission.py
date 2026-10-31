import os
import json
import hashlib
import fcntl
import multiprocessing
import socket
import sqlite3
import struct
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub_container_executor.state_custodian_admission import (
    CustodianAdmissionError, CustodianAdmissionJournal, DOMAIN, HOLD_DOMAIN, PROTOCOL, _canonical,
)
import hub_container_executor.state_custodian_admission as admission_module
from hub_container_executor.launch_supervisor import _boot_id, _proc_start
from hub_container_executor.state_custodian_observation import verify_held_observation, HeldObservationError

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason='requires synthetic root-owned enrollment')


def test_signed_admission_is_durable_and_one_shot(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    digest = 'sha256:' + 'a' * 64
    policy = {'schema': 2, 'journal_path': str(tmp_path / 'journal.sqlite'), 'supervisor_public_key': public, 'manifest_digest': digest,
              'source_backup_digest': digest, 'provider_key_digest': digest, 'provider_uid': 10001, 'provider_gid': 10001}
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical(policy)); enrollment.chmod(0o600)
    path = tmp_path / 'journal.sqlite'
    with pytest.raises(CustodianAdmissionError):
        CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'alternate.sqlite', create=True)
    assert not (tmp_path / 'alternate.sqlite').exists()
    now = time.time_ns() // 1_000_000
    payload = {'protocol': PROTOCOL, 'operation_id': str(uuid4()), 'command_id': str(uuid4()),
               'run_id': str(uuid4()), 'target_id': 'engineering-hub:staging', 'fencing_generation': 1,
               'manifest_digest': digest, 'role': 'candidate', 'source_backup_digest': digest,
               'provider_key_digest': digest, 'nonce': 'a' * 64, 'issued_at_ms': now, 'expires_at_ms': now + 5000}
    signature = key.sign(DOMAIN + _canonical(payload))
    journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=path, create=True)
    try:
        with pytest.raises(CustodianAdmissionError): journal.reserve(dict(payload, fencing_generation=2), signature)
        expired = dict(payload, issued_at_ms=now - 6000, expires_at_ms=now - 1000)
        with pytest.raises(CustodianAdmissionError): journal.reserve(expired, key.sign(DOMAIN + _canonical(expired)))
        assert len(journal.reserve(payload, signature)) == 64
        with pytest.raises(CustodianAdmissionError): journal.reserve(payload, signature)
    finally:
        journal.close()
    journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=path, create=False)
    try:
        with pytest.raises(CustodianAdmissionError): journal.reserve(payload, signature)
        other = dict(payload, operation_id=str(uuid4()))
        with pytest.raises(CustodianAdmissionError): journal.reserve(other, key.sign(DOMAIN + _canonical(other)))
    finally:
        journal.close()


@pytest.mark.parametrize('late', [False, True])
def test_exchange_uses_fresh_nonce_and_reserves(tmp_path, monkeypatch, late):
    monkeypatch.setenv('CUSTODIAN_TEST_SECRET', 'must-not-reach-child')
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    digest = 'sha256:' + 'a' * 64
    policy = {'schema': 2, 'journal_path': str(tmp_path / 'journal.sqlite'), 'supervisor_public_key': public, 'manifest_digest': digest,
              'source_backup_digest': digest, 'provider_key_digest': digest, 'provider_uid': 10001, 'provider_gid': 10001}
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical(policy)); enrollment.chmod(0o600)
    journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'journal.sqlite', create=True)
    if late:
        real_reserve = journal.reserve
        def slow_reserve(*args):
            result = real_reserve(*args)
            time.sleep(1.1)
            return result
        monkeypatch.setattr(journal, 'reserve', slow_reserve)
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    operation = str(uuid4())
    observed = []
    child_environments = []
    real_popen = admission_module.subprocess.Popen
    def capture_popen(*args, **kwargs):
        child_environments.append(kwargs['env'].copy())
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(admission_module.subprocess, 'Popen', capture_popen)
    def issuer():
        server.settimeout(3)
        raw, controls, _, _ = server.recvmsg(4096, socket.CMSG_SPACE(12))
        observed.extend(struct.unpack('3i', data) for level, kind, data in controls
                        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS)
        req = json.loads(raw)
        now = time.time_ns() // 1_000_000
        payload = {'protocol': PROTOCOL, 'operation_id': req['operation_id'], 'command_id': str(uuid4()),
                   'run_id': str(uuid4()), 'target_id': 'engineering-hub:staging', 'fencing_generation': 1,
                   'manifest_digest': digest, 'role': 'candidate', 'source_backup_digest': digest,
                   'provider_key_digest': digest, 'nonce': req['nonce'], 'issued_at_ms': now, 'expires_at_ms': now + 5000}
        server.send(_canonical({'payload': payload, 'signature': key.sign(DOMAIN + _canonical(payload)).hex()}))
    worker = threading.Thread(target=issuer)
    worker.start()
    try:
        if late:
            with pytest.raises(CustodianAdmissionError):
                journal.exchange(client, operation_id=operation, timeout_seconds=1, provider_uid=10001, provider_gid=10001)
            assert journal._db.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 1
            assert operation not in journal._fresh
            with pytest.raises(CustodianAdmissionError):
                journal.stage_quarantined_backup(operation_id=operation, admission_digest='a' * 64,
                    copy_policy_path=tmp_path / 'unused', timeout_seconds=1)
        else:
            assert len(journal.exchange(client, operation_id=operation, timeout_seconds=3, provider_uid=10001, provider_gid=10001)) == 64
    finally:
        client.close(); server.close(); worker.join(timeout=3); journal.close()
    assert not worker.is_alive()
    assert len(observed) == 1 and observed[0][1:] == (10001, 10001)
    assert child_environments == [{'LANG': 'C.UTF-8'}]


def test_reopen_rejects_incomplete_journal_schema(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    digest = 'sha256:' + 'a' * 64
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical({'schema': 2, 'journal_path': str(tmp_path / 'journal.sqlite'), 'supervisor_public_key': public, 'manifest_digest': digest,
        'source_backup_digest': digest, 'provider_key_digest': digest, 'provider_uid': 10001, 'provider_gid': 10001})); enrollment.chmod(0o600)
    path = tmp_path / 'journal.sqlite'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE admissions (operation_id TEXT, nonce TEXT, payload BLOB, signature BLOB)')
    db.commit(); db.close(); path.chmod(0o600)
    with pytest.raises(CustodianAdmissionError):
        CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=path, create=False)


def test_legacy_enrollment_never_enables_ownership_on_reopen(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    digest = 'sha256:' + 'a' * 64
    enrollment = tmp_path / 'legacy.json'
    enrollment.write_bytes(_canonical({'schema': 1, 'supervisor_public_key': public,
        'manifest_digest': digest, 'source_backup_digest': digest, 'provider_key_digest': digest,
        'provider_uid': 10001, 'provider_gid': 10001})); enrollment.chmod(0o600)
    path = tmp_path / 'journal.sqlite'
    journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=path, create=True)
    try:
        assert journal._holds_supported is False
    finally:
        journal.close()
    journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=path, create=False)
    try:
        assert journal._holds_supported is False
        with pytest.raises(CustodianAdmissionError):
            journal.prepare_private_owner(operation_id=str(uuid4()), admission_digest='a' * 64,
                                          owner_policy_path=tmp_path / 'unused')
    finally:
        journal.close()


@pytest.mark.parametrize('condition', ['wal_header', 'dangling_sidecar'])
def test_read_only_reopen_rejects_wal_and_sidecars_without_mutation(tmp_path, condition):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    digest = 'sha256:' + 'a' * 64
    path = tmp_path / 'journal.sqlite'; enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical({'schema': 2, 'journal_path': str(path), 'supervisor_public_key': public,
        'manifest_digest': digest, 'source_backup_digest': digest, 'provider_key_digest': digest,
        'provider_uid': 10001, 'provider_gid': 10001})); enrollment.chmod(0o600)
    journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=path, create=True); journal.close()
    if condition == 'wal_header':
        db = sqlite3.connect(path); assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',); db.close()
    else:
        os.symlink('missing', str(path) + '-wal')
    before = hashlib.sha256(path.read_bytes()).digest(); names = set(os.listdir(tmp_path))
    with pytest.raises(CustodianAdmissionError):
        CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=path, create=False, read_only=True)
    assert hashlib.sha256(path.read_bytes()).digest() == before and set(os.listdir(tmp_path)) == names


def test_post_commit_clock_regression_keeps_reservation(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    digest = 'sha256:' + 'a' * 64
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical({'schema': 2, 'journal_path': str(tmp_path / 'journal.sqlite'), 'supervisor_public_key': public, 'manifest_digest': digest,
        'source_backup_digest': digest, 'provider_key_digest': digest, 'provider_uid': 10001, 'provider_gid': 10001})); enrollment.chmod(0o600)
    now = time.time_ns() // 1_000_000
    payload = {'protocol': PROTOCOL, 'operation_id': str(uuid4()), 'command_id': str(uuid4()),
        'run_id': str(uuid4()), 'target_id': 'engineering-hub:staging', 'fencing_generation': 1,
        'manifest_digest': digest, 'role': 'candidate', 'source_backup_digest': digest,
        'provider_key_digest': digest, 'nonce': 'b' * 64, 'issued_at_ms': now - 10, 'expires_at_ms': now + 4000}
    signature = key.sign(DOMAIN + _canonical(payload))
    journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'journal.sqlite', create=True)
    try:
        samples = iter((now, now, now - 1))
        monkeypatch.setattr(admission_module.time, 'time_ns', lambda: next(samples) * 1_000_000)
        with pytest.raises(CustodianAdmissionError): journal.reserve(payload, signature)
        assert journal._db.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 1
    finally:
        journal.close()


@pytest.mark.parametrize('uncertain_chown', [False, True, 'late', 'record_write', 'record_fsync'])
def test_quarantined_copy_is_bound_and_never_replayed(tmp_path, monkeypatch, uncertain_chown):
    source_dir = tmp_path / 'source'; source_dir.mkdir(mode=0o700)
    stage = tmp_path / 'stage'; stage.mkdir(mode=0o700)
    source = source_dir / 'backup.sqlite'
    db = sqlite3.connect(source); db.execute('CREATE TABLE item (value TEXT)'); db.commit(); db.close()
    source.chmod(0o600)
    backup_digest = 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()
    digest = 'sha256:' + 'a' * 64
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    provider_key = Ed25519PrivateKey.generate()
    provider_raw = provider_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    provider_public = provider_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    provider_digest = 'sha256:' + hashlib.sha256(provider_public).hexdigest()
    provider_path = tmp_path / 'provider.key'; provider_path.write_bytes(provider_raw); provider_path.chmod(0o600)
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical({'schema': 2, 'journal_path': str(tmp_path / 'journal.sqlite'), 'supervisor_public_key': public, 'manifest_digest': digest,
        'source_backup_digest': backup_digest, 'provider_key_digest': provider_digest, 'provider_uid': 10001, 'provider_gid': 10001}))
    enrollment.chmod(0o600)
    copy_policy = tmp_path / 'copy.json'
    copy_policy.write_bytes(_canonical({'schema': 1, 'source': str(source), 'source_digest': backup_digest,
        'staging_directory': str(stage), 'maximum_bytes': 1024 * 1024,
        'maximum_total_bytes': 4 * 1024 * 1024, 'minimum_free_bytes': 1024})); copy_policy.chmod(0o600)
    now = time.time_ns() // 1_000_000
    payload = {'protocol': PROTOCOL, 'operation_id': str(uuid4()), 'command_id': str(uuid4()),
        'run_id': str(uuid4()), 'target_id': 'engineering-hub:staging', 'fencing_generation': 1,
        'manifest_digest': digest, 'role': 'candidate', 'source_backup_digest': backup_digest,
        'provider_key_digest': provider_digest, 'nonce': 'c' * 64, 'issued_at_ms': now, 'expires_at_ms': now + 5000}
    journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'journal.sqlite', create=True)
    try:
        token = journal.reserve(payload, key.sign(DOMAIN + _canonical(payload)))
        receipt = journal.stage_quarantined_backup(operation_id=payload['operation_id'], admission_digest=token,
                                                   copy_policy_path=copy_policy, timeout_seconds=3)
        output = stage / (payload['operation_id'] + '.quarantine')
        assert output.read_bytes() == source.read_bytes() and receipt['digest'] == backup_digest
        assert output.stat().st_mode & 0o777 == 0o600
        with pytest.raises(CustodianAdmissionError):
            journal.stage_quarantined_backup(operation_id=payload['operation_id'], admission_digest=token,
                                             copy_policy_path=copy_policy, timeout_seconds=3)
        clock_op = dict(payload, operation_id=str(uuid4()), nonce='f' * 64)
        clock_token = journal.reserve(clock_op, key.sign(DOMAIN + _canonical(clock_op)))
        with monkeypatch.context() as clock:
            clock.setattr(admission_module.time, 'time_ns', lambda: (now - 1) * 1_000_000)
            with pytest.raises(CustodianAdmissionError):
                journal.stage_quarantined_backup(operation_id=clock_op['operation_id'], admission_digest=clock_token,
                                                 copy_policy_path=copy_policy, timeout_seconds=3)
        assert not (stage / (clock_op['operation_id'] + '.quarantine')).exists()
        assert journal._db.execute('SELECT COUNT(*) FROM admissions WHERE operation_id=?', (clock_op['operation_id'],)).fetchone()[0] == 1
        second = dict(payload, operation_id=str(uuid4()), nonce='d' * 64)
        second_token = journal.reserve(second, key.sign(DOMAIN + _canonical(second)))
        with source.open('ab') as file: file.write(b'changed')
        with pytest.raises(CustodianAdmissionError):
            journal.stage_quarantined_backup(operation_id=second['operation_id'], admission_digest=second_token,
                                             copy_policy_path=copy_policy, timeout_seconds=3)
        assert (stage / (second['operation_id'] + '.quarantine')).exists()
        third = dict(payload, operation_id=str(uuid4()), nonce='e' * 64)
        third_token = journal.reserve(third, key.sign(DOMAIN + _canonical(third)))
        policy = json.loads(copy_policy.read_bytes()); policy['maximum_total_bytes'] = policy['maximum_bytes']
        copy_policy.write_bytes(_canonical(policy)); copy_policy.chmod(0o600)
        filler = stage / (str(uuid4()) + '.quarantine')
        with filler.open('wb') as file: file.truncate(1024 * 1024)
        filler.chmod(0o600)
        with pytest.raises(CustodianAdmissionError):
            journal.stage_quarantined_backup(operation_id=third['operation_id'], admission_digest=third_token,
                                             copy_policy_path=copy_policy, timeout_seconds=3)
        assert not (stage / (third['operation_id'] + '.quarantine')).exists()
        owner_policy = tmp_path / 'owner.json'
        owner_policy.write_bytes(_canonical({'schema': 1, 'boot_id': _boot_id(), 'daemon_pid': os.getpid(),
            'daemon_start_ticks': _proc_start(os.getpid()), 'uid_map': open('/proc/self/uid_map').read(),
            'gid_map': open('/proc/self/gid_map').read(), 'app_uid': 10002, 'app_gid': 10002}))
        owner_policy.chmod(0o600)
        with monkeypatch.context() as hooks:
            if uncertain_chown in ('record_write', 'record_fsync'):
                original_open, original_write, original_fsync = os.open, os.write, os.fsync
                record_fds = set()
                def track_record(path, *args, **kwargs):
                    fd = original_open(path, *args, **kwargs)
                    if str(path).endswith('.ownership-observed'): record_fds.add(fd)
                    return fd
                def fail_record(fd, data):
                    if fd in record_fds and uncertain_chown == 'record_write': raise OSError('synthetic incomplete preparation record')
                    return original_write(fd, data)
                def fail_record_sync(fd):
                    original_fsync(fd)
                    if fd in record_fds and uncertain_chown == 'record_fsync': raise OSError('synthetic uncertain record sync')
                hooks.setattr(admission_module.os, 'open', track_record)
                hooks.setattr(admission_module.os, 'write', fail_record)
                hooks.setattr(admission_module.os, 'fsync', fail_record_sync)
                with pytest.raises(CustodianAdmissionError):
                    journal.prepare_private_owner(operation_id=payload['operation_id'], admission_digest=token,
                                                  owner_policy_path=owner_policy)
            elif uncertain_chown == 'late':
                root_file = admission_module._root_file
                monotonic = time.monotonic
                def delayed_policy(path, maximum):
                    result = root_file(path, maximum)
                    if (stage / (payload['operation_id'] + '.ownership-intent')).exists():
                        hooks.setattr(admission_module.time, 'monotonic', lambda: monotonic() + 10)
                    return result
                hooks.setattr(admission_module, '_root_file', delayed_policy)
                with pytest.raises(CustodianAdmissionError):
                    journal.prepare_private_owner(operation_id=payload['operation_id'], admission_digest=token,
                                                  owner_policy_path=owner_policy)
            elif uncertain_chown:
                real_fchown = os.fchown
                def changed_then_failed(fd, uid, gid):
                    real_fchown(fd, uid, gid)
                    raise OSError('synthetic uncertain ownership')
                hooks.setattr(admission_module.os, 'fchown', changed_then_failed)
                with pytest.raises(CustodianAdmissionError):
                    journal.prepare_private_owner(operation_id=payload['operation_id'], admission_digest=token,
                                                  owner_policy_path=owner_policy)
            else:
                prepared = journal.prepare_private_owner(operation_id=payload['operation_id'], admission_digest=token,
                                                          owner_policy_path=owner_policy)
                assert prepared['state'] == 'private_owner_prepared'
        assert output.stat().st_uid == (0 if uncertain_chown == 'late' else 10002)
        assert (stage / (payload['operation_id'] + '.ownership-intent')).exists()
        assert (stage / (payload['operation_id'] + '.ownership-observed')).exists() is (uncertain_chown in (False, 'record_write', 'record_fsync'))
        start_wall = time.time_ns() // 1_000_000; start_mono = time.monotonic()
        observed = journal.observe_held(target_id=payload['target_id'], nonce='4' * 64,
            owner_policy_path=owner_policy, copy_policy_path=copy_policy, provider_key_path=provider_path, timeout_seconds=3)
        provider_key.public_key().verify(bytes.fromhex(observed['signature']), HOLD_DOMAIN + _canonical(observed['payload']))
        assert observed['payload']['operation_id'] == payload['operation_id']
        assert observed['payload']['quiescence'] == 'unknown' and observed['payload']['marker_present'] is True
        assert observed['payload']['run_id'] == payload['run_id'] and observed['payload']['manifest_digest'] == digest
        record_path = stage / (payload['operation_id'] + '.ownership-observed')
        if uncertain_chown in (False, 'record_fsync'):
            v2 = journal.observe_held(target_id=payload['target_id'], nonce='6' * 64,
                owner_policy_path=owner_policy, copy_policy_path=copy_policy, provider_key_path=provider_path,
                timeout_seconds=3, include_preparation=True)
            assert v2['payload']['private_preparation_recorded'] is True
            old_record = record_path.read_bytes(); record_path.write_bytes(b'corrupt'); record_path.chmod(0o600)
            with pytest.raises(CustodianAdmissionError):
                journal.observe_held(target_id=payload['target_id'], nonce='6' * 64,
                    owner_policy_path=owner_policy, copy_policy_path=copy_policy, provider_key_path=provider_path,
                    timeout_seconds=3, include_preparation=True)
            assert journal._db.execute('SELECT operation_id FROM custody_holds').fetchone() == (payload['operation_id'],)
            record_path.write_bytes(old_record); record_path.chmod(0o600)
        info = output.stat()
        expected = {'target_id': payload['target_id'], 'operation_id': payload['operation_id'], 'run_id': payload['run_id'],
                    'manifest_digest': digest, 'admission_digest': token, 'fencing_generation': payload['fencing_generation'],
                    'device': info.st_dev, 'inode': info.st_ino, 'host_uid': 10002, 'host_gid': 10002,
                    'owner_policy_digest': hashlib.sha256(owner_policy.read_bytes()).hexdigest()}
        assert verify_held_observation(_canonical(observed), provider_public_key=provider_public, nonce='4' * 64,
            expected=expected, start_wall_ms=start_wall, start_monotonic=start_mono)['quiescence'] == 'unknown'
        with pytest.raises(HeldObservationError):
            verify_held_observation(_canonical(observed), provider_public_key=provider_public, nonce='5' * 64,
                expected=expected, start_wall_ms=start_wall, start_monotonic=start_mono)
        with pytest.raises(HeldObservationError):
            verify_held_observation(_canonical(observed), provider_public_key=provider_public, nonce='4' * 64,
                expected=dict(expected, fencing_generation=True), start_wall_ms=start_wall, start_monotonic=start_mono)
        installed = os.environ.get('EH_INSTALLED_EXECUTOR_PATH')
        if installed:
            cli_enrollment = tmp_path / 'inspect.json'
            cli_enrollment.write_bytes(_canonical({'schema': 1, 'journal_enrollment': str(enrollment),
                'journal_path': str(tmp_path / 'journal.sqlite'), 'owner_policy': str(owner_policy),
                'copy_policy': str(copy_policy), 'provider_key_path': str(provider_path),
                'provider_public_key': provider_public.hex(), 'expected': expected})); cli_enrollment.chmod(0o600)
            journal.close()
            before = hashlib.sha256((tmp_path / 'journal.sqlite').read_bytes()).digest()
            before_names = set(os.listdir(tmp_path))
            invoke = ("import runpy,sys; sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)]; "
                "import hub_container_executor; assert hub_container_executor.__file__.startswith(sys.path[0]); "
                "runpy.run_module('hub_container_executor.state_custodian_inspect',run_name='__main__')")
            result = subprocess.run([sys.executable, '-I', '-c', invoke, installed, '/src/src',
                '--enrollment', str(cli_enrollment)], capture_output=True, text=True, timeout=8, cwd='/',
                env={'LANG': 'C.UTF-8'})
            assert result.returncode == 0 and result.stderr == ''
            assert json.loads(result.stdout) == {'status': 'held', 'quiescence': 'unknown',
                'operation_id': payload['operation_id'], 'fencing_generation': 1, 'marker_present': True,
                'private_preparation_recorded': uncertain_chown in (False, 'record_fsync'), 'intent_state': 'valid',
                'preparation_state': 'incomplete' if uncertain_chown == 'record_write' else
                                     'valid' if uncertain_chown in (False, 'record_fsync') else 'absent'}
            assert provider_raw.hex() not in result.stdout and provider_raw.hex() not in result.stderr
            assert hashlib.sha256((tmp_path / 'journal.sqlite').read_bytes()).digest() == before
            assert set(os.listdir(tmp_path)) == before_names
            invalid = json.loads(cli_enrollment.read_bytes())
            invalid['expected']['fencing_generation'] += 1
            bad_enrollment = tmp_path / 'inspect-invalid.json'
            bad_enrollment.write_bytes(_canonical(invalid)); bad_enrollment.chmod(0o600)
            rejected = subprocess.run([sys.executable, '-I', '-c', invoke, installed, '/src/src',
                '--enrollment', str(bad_enrollment)], capture_output=True, text=True, timeout=8, cwd='/',
                env={'LANG': 'C.UTF-8'})
            assert rejected.returncode == 2 and rejected.stdout == ''
            assert rejected.stderr == 'Custodian hold inspection unavailable\n'
            assert hashlib.sha256((tmp_path / 'journal.sqlite').read_bytes()).digest() == before
            read_only = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'journal.sqlite',
                                                  create=False, read_only=True)
            try:
                assert fcntl.fcntl(read_only._fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
                assert read_only._db.execute('PRAGMA query_only').fetchone() == (1,)
                with pytest.raises(sqlite3.OperationalError):
                    read_only._db.execute('DELETE FROM custody_holds')
                with pytest.raises(CustodianAdmissionError):
                    read_only.reserve(payload, key.sign(DOMAIN + _canonical(payload)))
            finally:
                read_only.close()
            journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'journal.sqlite', create=False)
        other_key = tmp_path / 'other.key'; other_key.write_bytes(os.urandom(32)); other_key.chmod(0o600)
        with pytest.raises(CustodianAdmissionError):
            journal.observe_held(target_id=payload['target_id'], nonce='4' * 64, owner_policy_path=owner_policy,
                copy_policy_path=copy_policy, provider_key_path=other_key, timeout_seconds=3)
        with pytest.raises(CustodianAdmissionError):
            journal.observe_held(target_id=payload['target_id'], nonce='bad', owner_policy_path=owner_policy,
                copy_policy_path=copy_policy, provider_key_path=provider_path, timeout_seconds=3)
        journal._db.execute('UPDATE custody_holds SET fence=fence+1')
        with pytest.raises(CustodianAdmissionError):
            journal.observe_held(target_id=payload['target_id'], nonce='4' * 64, owner_policy_path=owner_policy,
                copy_policy_path=copy_policy, provider_key_path=provider_path, timeout_seconds=3)
        journal._db.execute('UPDATE custody_holds SET fence=fence-1')
        denied = subprocess.run([sys.executable, '-c', 'import sys; open(sys.argv[1], "rb")', str(output)],
            user=10002, group=10002, extra_groups=(), env={'LANG': 'C.UTF-8'}, cwd='/',
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        assert denied.returncode != 0
        with pytest.raises(CustodianAdmissionError):
            journal.prepare_private_owner(operation_id=payload['operation_id'], admission_digest=token,
                                          owner_policy_path=owner_policy)
        # Restore unrelated capacity and source conditions; the prior prepared
        # file and its marker must be accounted without being adopted or reused.
        filler.unlink()
        source.write_bytes(output.read_bytes()); source.chmod(0o600)
        policy['maximum_total_bytes'] = 4 * 1024 * 1024
        copy_policy.write_bytes(_canonical(policy)); copy_policy.chmod(0o600)
        fourth = dict(payload, operation_id=str(uuid4()), nonce='1' * 64)
        fourth_token = journal.reserve(fourth, key.sign(DOMAIN + _canonical(fourth)))
        if uncertain_chown == 'record_write':
            with pytest.raises(CustodianAdmissionError):
                journal.stage_quarantined_backup(operation_id=fourth['operation_id'], admission_digest=fourth_token,
                                                 copy_policy_path=copy_policy, timeout_seconds=3)
        else:
            fourth_result = journal.stage_quarantined_backup(operation_id=fourth['operation_id'], admission_digest=fourth_token,
                                                             copy_policy_path=copy_policy, timeout_seconds=3)
            assert fourth_result['digest'] == backup_digest
            assert (stage / (fourth['operation_id'] + '.quarantine')).exists()
        assert output.stat().st_uid == (0 if uncertain_chown == 'late' else 10002)
        with pytest.raises(CustodianAdmissionError):
            journal.prepare_private_owner(operation_id=fourth['operation_id'], admission_digest=fourth_token,
                                          owner_policy_path=owner_policy)
        assert not (stage / (fourth['operation_id'] + '.ownership-intent')).exists()
        assert journal._db.execute('SELECT operation_id FROM custody_holds').fetchone() == (payload['operation_id'],)
    finally:
        journal.close()
    reopened = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'journal.sqlite', create=False)
    try:
        assert reopened._db.execute('SELECT operation_id FROM custody_holds').fetchone() == (payload['operation_id'],)
        fifth = dict(payload, operation_id=str(uuid4()), nonce='2' * 64)
        fifth_token = reopened.reserve(fifth, key.sign(DOMAIN + _canonical(fifth)))
        if uncertain_chown == 'record_write':
            with pytest.raises(CustodianAdmissionError):
                reopened.stage_quarantined_backup(operation_id=fifth['operation_id'], admission_digest=fifth_token,
                                                  copy_policy_path=copy_policy, timeout_seconds=3)
        else:
            reopened.stage_quarantined_backup(operation_id=fifth['operation_id'], admission_digest=fifth_token,
                                              copy_policy_path=copy_policy, timeout_seconds=3)
        with pytest.raises(CustodianAdmissionError):
            reopened.prepare_private_owner(operation_id=fifth['operation_id'], admission_digest=fifth_token,
                                            owner_policy_path=owner_policy)
        assert not (stage / (fifth['operation_id'] + '.ownership-intent')).exists()
        with pytest.raises(CustodianAdmissionError):
            reopened.stage_quarantined_backup(operation_id=second['operation_id'], admission_digest=second_token,
                                              copy_policy_path=copy_policy, timeout_seconds=3)
    finally:
        reopened.close()


@pytest.mark.parametrize('crashpoint', ['before_marker', 'intent_write', 'intent_partial', 'record_write', 'record_partial', 'record_fsync'])
def test_owner_crash_after_durable_hold_keeps_target_reserved(tmp_path, crashpoint):
    source_dir = tmp_path / 'source'; source_dir.mkdir(mode=0o700)
    stage = tmp_path / 'stage'; stage.mkdir(mode=0o700)
    source = source_dir / 'backup.sqlite'
    db = sqlite3.connect(source); db.execute('CREATE TABLE item (value TEXT)'); db.commit(); db.close(); source.chmod(0o600)
    backup = 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()
    digest = 'sha256:' + 'a' * 64
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    provider_key = Ed25519PrivateKey.generate()
    provider_raw = provider_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    provider_public = provider_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    provider_digest = 'sha256:' + hashlib.sha256(provider_public).hexdigest()
    provider_path = tmp_path / 'provider.key'; provider_path.write_bytes(provider_raw); provider_path.chmod(0o600)
    enrollment = tmp_path / 'enrollment.json'; journal_path = tmp_path / 'journal.sqlite'
    enrollment.write_bytes(_canonical({'schema': 2, 'journal_path': str(journal_path), 'supervisor_public_key': public,
        'manifest_digest': digest, 'source_backup_digest': backup, 'provider_key_digest': provider_digest,
        'provider_uid': 10001, 'provider_gid': 10001})); enrollment.chmod(0o600)
    copy_policy = tmp_path / 'copy.json'
    copy_policy.write_bytes(_canonical({'schema': 1, 'source': str(source), 'source_digest': backup,
        'staging_directory': str(stage), 'maximum_bytes': 1024 * 1024,
        'maximum_total_bytes': 4 * 1024 * 1024, 'minimum_free_bytes': 1024})); copy_policy.chmod(0o600)
    operation = str(uuid4())
    def child():
        now = time.time_ns() // 1_000_000
        payload = {'protocol': PROTOCOL, 'operation_id': operation, 'command_id': str(uuid4()),
            'run_id': str(uuid4()), 'target_id': 'engineering-hub:staging', 'fencing_generation': 1,
            'manifest_digest': digest, 'role': 'candidate', 'source_backup_digest': backup,
            'provider_key_digest': provider_digest, 'nonce': '9' * 64, 'issued_at_ms': now, 'expires_at_ms': now + 5000}
        journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=journal_path, create=True)
        token = journal.reserve(payload, key.sign(DOMAIN + _canonical(payload)))
        journal.stage_quarantined_backup(operation_id=operation, admission_digest=token, copy_policy_path=copy_policy, timeout_seconds=3)
        owner = tmp_path / 'owner.json'
        owner.write_bytes(_canonical({'schema': 1, 'boot_id': _boot_id(), 'daemon_pid': os.getpid(),
            'daemon_start_ticks': _proc_start(os.getpid()), 'uid_map': open('/proc/self/uid_map').read(),
            'gid_map': open('/proc/self/gid_map').read(), 'app_uid': 10002, 'app_gid': 10002})); owner.chmod(0o600)
        original_open, original_write, original_fsync = os.open, os.write, os.fsync
        record_fds, intent_fds = set(), set()
        def crash_on_marker(path, *args, **kwargs):
            if crashpoint == 'before_marker' and str(path).endswith('.ownership-intent'): os.kill(os.getpid(), signal.SIGKILL)
            fd = original_open(path, *args, **kwargs)
            if str(path).endswith('.ownership-observed'): record_fds.add(fd)
            if str(path).endswith('.ownership-intent'): intent_fds.add(fd)
            return fd
        def crash_on_write(fd, value):
            if (crashpoint in ('intent_write', 'intent_partial') and fd in intent_fds) or (crashpoint in ('record_write', 'record_partial') and fd in record_fds):
                if crashpoint.endswith('_partial'): original_write(fd, value[:7])
                os.kill(os.getpid(), signal.SIGKILL)
            return original_write(fd, value)
        def crash_on_record_sync(fd):
            if crashpoint == 'record_fsync' and fd in record_fds: os.kill(os.getpid(), signal.SIGKILL)
            return original_fsync(fd)
        admission_module.os.open = crash_on_marker
        admission_module.os.write = crash_on_write
        admission_module.os.fsync = crash_on_record_sync
        journal.prepare_private_owner(operation_id=operation, admission_digest=token, owner_policy_path=owner)
    process = multiprocessing.get_context('fork').Process(target=child)
    process.start()
    try:
        process.join(timeout=8)
        if process.is_alive():
            process.kill(); process.join(timeout=3)
            pytest.fail('owned synthetic child exceeded deadline')
        assert process.exitcode == -signal.SIGKILL
        journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=journal_path, create=False)
        try:
            assert journal._db.execute('SELECT operation_id FROM custody_holds').fetchone() == (operation,)
            assert (stage / (operation + '.ownership-intent')).exists() is (crashpoint != 'before_marker')
            hold = journal._db.execute('SELECT * FROM custody_holds').fetchone()
            admission = json.loads(journal._db.execute('SELECT payload FROM admissions WHERE operation_id=?', (operation,)).fetchone()[0])
        finally:
            journal.close()
        installed = os.environ.get('EH_INSTALLED_EXECUTOR_PATH')
        if installed:
            expected = {'target_id': hold[0], 'operation_id': hold[1], 'run_id': admission['run_id'],
                'manifest_digest': admission['manifest_digest'], 'admission_digest': hold[2], 'fencing_generation': hold[3],
                'device': hold[4], 'inode': hold[5], 'host_uid': hold[6], 'host_gid': hold[7], 'owner_policy_digest': hold[8]}
            inspect = tmp_path / 'inspect.json'
            inspect.write_bytes(_canonical({'schema': 1, 'journal_enrollment': str(enrollment), 'journal_path': str(journal_path),
                'owner_policy': str(tmp_path / 'owner.json'), 'copy_policy': str(copy_policy), 'provider_key_path': str(provider_path),
                'provider_public_key': provider_public.hex(), 'expected': expected})); inspect.chmod(0o600)
            invoke = ("import runpy,sys; sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)]; "
                "import hub_container_executor; assert hub_container_executor.__file__.startswith(sys.path[0]); "
                "runpy.run_module('hub_container_executor.state_custodian_inspect',run_name='__main__')")
            before = hashlib.sha256(journal_path.read_bytes()).digest()
            result = subprocess.run([sys.executable, '-I', '-c', invoke, installed, '/src/src', '--enrollment', str(inspect)],
                capture_output=True, text=True, timeout=8, cwd='/', env={'LANG': 'C.UTF-8'})
            assert result.returncode == 0 and result.stderr == ''
            output = json.loads(result.stdout)
            assert output['status'] == 'held' and output['quiescence'] == 'unknown'
            assert output['intent_state'] == ('absent' if crashpoint == 'before_marker' else 'incomplete' if crashpoint in ('intent_write', 'intent_partial') else 'valid')
            assert output['preparation_state'] == ('incomplete' if crashpoint in ('record_write', 'record_partial') else 'valid' if crashpoint == 'record_fsync' else 'absent')
            assert hashlib.sha256(journal_path.read_bytes()).digest() == before
            if crashpoint == 'intent_partial':
                marker = stage / (operation + '.ownership-intent')
                saved = marker.read_bytes(); marker.write_bytes(b'not-a-prefix'); marker.chmod(0o600)
                bad = subprocess.run([sys.executable, '-I', '-c', invoke, installed, '/src/src', '--enrollment', str(inspect)],
                    capture_output=True, text=True, timeout=8, cwd='/', env={'LANG': 'C.UTF-8'})
                assert bad.returncode == 2 and bad.stdout == '' and bad.stderr == 'Custodian hold inspection unavailable\n'
                marker.write_bytes(saved); marker.chmod(0o644)
                bad_mode = subprocess.run([sys.executable, '-I', '-c', invoke, installed, '/src/src', '--enrollment', str(inspect)],
                    capture_output=True, text=True, timeout=8, cwd='/', env={'LANG': 'C.UTF-8'})
                assert bad_mode.returncode == 2 and bad_mode.stdout == '' and bad_mode.stderr == 'Custodian hold inspection unavailable\n'
                assert hashlib.sha256(journal_path.read_bytes()).digest() == before
    finally:
        if process.is_alive(): process.kill(); process.join(timeout=3)
        process.close()
