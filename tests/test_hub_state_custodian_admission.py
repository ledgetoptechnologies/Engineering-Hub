import os
import json
import hashlib
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub_container_executor.state_custodian_admission import (
    CustodianAdmissionError, CustodianAdmissionJournal, DOMAIN, PROTOCOL, _canonical,
)
import hub_container_executor.state_custodian_admission as admission_module
from hub_container_executor.launch_supervisor import _boot_id, _proc_start

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason='requires synthetic root-owned enrollment')


def test_signed_admission_is_durable_and_one_shot(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    digest = 'sha256:' + 'a' * 64
    policy = {'schema': 1, 'supervisor_public_key': public, 'manifest_digest': digest,
              'source_backup_digest': digest, 'provider_key_digest': digest, 'provider_uid': 10001, 'provider_gid': 10001}
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical(policy)); enrollment.chmod(0o600)
    path = tmp_path / 'journal.sqlite'
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
    policy = {'schema': 1, 'supervisor_public_key': public, 'manifest_digest': digest,
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
    enrollment.write_bytes(_canonical({'schema': 1, 'supervisor_public_key': public, 'manifest_digest': digest,
        'source_backup_digest': digest, 'provider_key_digest': digest, 'provider_uid': 10001, 'provider_gid': 10001})); enrollment.chmod(0o600)
    path = tmp_path / 'journal.sqlite'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE admissions (operation_id TEXT, nonce TEXT, payload BLOB, signature BLOB)')
    db.commit(); db.close(); path.chmod(0o600)
    with pytest.raises(CustodianAdmissionError):
        CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=path, create=False)


def test_post_commit_clock_regression_keeps_reservation(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    digest = 'sha256:' + 'a' * 64
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical({'schema': 1, 'supervisor_public_key': public, 'manifest_digest': digest,
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


@pytest.mark.parametrize('uncertain_chown', [False, True, 'late'])
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
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical({'schema': 1, 'supervisor_public_key': public, 'manifest_digest': digest,
        'source_backup_digest': backup_digest, 'provider_key_digest': digest, 'provider_uid': 10001, 'provider_gid': 10001}))
    enrollment.chmod(0o600)
    copy_policy = tmp_path / 'copy.json'
    copy_policy.write_bytes(_canonical({'schema': 1, 'source': str(source), 'source_digest': backup_digest,
        'staging_directory': str(stage), 'maximum_bytes': 1024 * 1024,
        'maximum_total_bytes': 4 * 1024 * 1024, 'minimum_free_bytes': 1024})); copy_policy.chmod(0o600)
    now = time.time_ns() // 1_000_000
    payload = {'protocol': PROTOCOL, 'operation_id': str(uuid4()), 'command_id': str(uuid4()),
        'run_id': str(uuid4()), 'target_id': 'engineering-hub:staging', 'fencing_generation': 1,
        'manifest_digest': digest, 'role': 'candidate', 'source_backup_digest': backup_digest,
        'provider_key_digest': digest, 'nonce': 'c' * 64, 'issued_at_ms': now, 'expires_at_ms': now + 5000}
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
            if uncertain_chown == 'late':
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
        fourth_result = journal.stage_quarantined_backup(operation_id=fourth['operation_id'], admission_digest=fourth_token,
                                                         copy_policy_path=copy_policy, timeout_seconds=3)
        assert fourth_result['digest'] == backup_digest
        assert (stage / (fourth['operation_id'] + '.quarantine')).exists()
        assert output.stat().st_uid == (0 if uncertain_chown == 'late' else 10002)
    finally:
        journal.close()
    reopened = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'journal.sqlite', create=False)
    try:
        with pytest.raises(CustodianAdmissionError):
            reopened.stage_quarantined_backup(operation_id=second['operation_id'], admission_digest=second_token,
                                              copy_policy_path=copy_policy, timeout_seconds=3)
    finally:
        reopened.close()
