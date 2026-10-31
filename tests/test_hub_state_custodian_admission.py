import os
import json
import socket
import sqlite3
import struct
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


def test_exchange_uses_fresh_nonce_and_reserves(tmp_path, monkeypatch):
    monkeypatch.setenv('CUSTODIAN_TEST_SECRET', 'must-not-reach-child')
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    digest = 'sha256:' + 'a' * 64
    policy = {'schema': 1, 'supervisor_public_key': public, 'manifest_digest': digest,
              'source_backup_digest': digest, 'provider_key_digest': digest, 'provider_uid': 10001, 'provider_gid': 10001}
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical(policy)); enrollment.chmod(0o600)
    journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'journal.sqlite', create=True)
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
