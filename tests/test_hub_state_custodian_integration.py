"""Synthetic supervisor and ledger, real signed transport and root journal."""
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
from hub_container_executor.launch_supervisor import LaunchSupervisor, LaunchSupervisorError, _canonical, _boot_id, _proc_start
from hub_container_executor.ledger import SQLiteLedger
from hub_container_executor.state_custodian_admission import CustodianAdmissionJournal, CustodianAdmissionError, DOMAIN
from tests.test_executor_launch_profile import gate_profile
from tests.test_hub_container_executor_ledger import request
from tests.test_hub_container_pilot_manifest import make_manifest_data
import hub_container_executor.ledger as ledger_module


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() != 0,
                    reason='requires synthetic root and distinct provider UID')
@pytest.mark.parametrize('mode', ['exchange', 'prepare', 'deadline', 'context_mismatch'])
def test_supervisor_through_nonroot_transport_to_root_journal(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    source_dir = tmp_path / 'source'; source_dir.mkdir(mode=0o700)
    stage = tmp_path / 'stage'; stage.mkdir(mode=0o700)
    source = source_dir / 'backup.sqlite'
    db = sqlite3.connect(source); db.execute('CREATE TABLE item(value TEXT)'); db.commit(); db.close(); source.chmod(0o600)
    backup = 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_data = make_manifest_data(); manifest_data['state_policy']['baseline_backup_digest'] = backup
    manifest = PilotManifest.model_validate(manifest_data)
    data = gate_profile()
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    data['generation_isolation'] = dict(kind='separate-provider-v1', app_uid=10002,
        app_gid=10002, provider_host_uid=200000, provider_host_gid=200000, provider_public_key='1' * 64)
    profile = ExecutorLaunchProfile.model_validate(data)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    provider_digest = 'sha256:' + hashlib.sha256(bytes.fromhex('1' * 64)).hexdigest()
    enrollment = tmp_path / 'enrollment.json'
    enrollment.write_bytes(_canonical({'schema': 2, 'journal_path': str(tmp_path / 'journal.sqlite'), 'supervisor_public_key': public,
        'manifest_digest': manifest.digest(), 'source_backup_digest': manifest.state_policy.baseline_backup_digest,
        'provider_key_digest': provider_digest, 'provider_uid': 200000, 'provider_gid': 200000}))
    enrollment.chmod(0o600)
    copy_policy = tmp_path / 'copy.json'
    copy_policy.write_bytes(_canonical({'schema': 1, 'source': str(source), 'source_digest': backup,
        'staging_directory': str(stage), 'maximum_bytes': 1024 * 1024,
        'maximum_total_bytes': 4 * 1024 * 1024, 'minimum_free_bytes': 1024})); copy_policy.chmod(0o600)
    owner_policy = tmp_path / 'owner.json'
    owner_policy.write_bytes(_canonical({'schema': 1, 'boot_id': _boot_id(), 'daemon_pid': os.getpid(),
        'daemon_start_ticks': _proc_start(os.getpid()), 'uid_map': open('/proc/self/uid_map').read(),
        'gid_map': open('/proc/self/gid_map').read(), 'app_uid': 10002, 'app_gid': 10002})); owner_policy.chmod(0o600)
    private = tmp_path / 'ledger'; private.mkdir(mode=0o700)
    with ExitStack() as cleanup:
        ledger = SQLiteLedger(private / 'ledger.db', manifest_digest=manifest.digest(),
            candidate_commit=manifest.candidate.source_commit, policy_revision=manifest.policy_revision)
        cleanup.callback(ledger.close)
        journal = CustodianAdmissionJournal(enrollment_path=enrollment, journal_path=tmp_path / 'journal.sqlite', create=True)
        cleanup.callback(journal.close)
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        cleanup.callback(server.close); cleanup.callback(client.close)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        results = []
        command = request().model_copy(update={'manifest_digest': manifest.digest(),
            'candidate_commit': manifest.candidate.source_commit})
        assert ledger.admit(command).accepted
        operation = ledger.begin_operation(command.command_id, 'state_copy')
        supervisor = SimpleNamespace(_pid=os.getpid(), profile=profile,
            release=SimpleNamespace(launch_profile_digest=profile.digest()), _signer=key,
            _handoff_manifest_digest=lambda: manifest.digest(),
            _observation_mutex=lambda deadline: nullcontext(), _validate_live=lambda deadline: None,
            pending_state_context=lambda **kwargs: ledger.pending_state_context(kwargs['operation_id']))
        def serve():
            try:
                results.append(LaunchSupervisor.serve_state_custodian_admission(supervisor,
                    channel=server, manifest=manifest, timeout_seconds=4))
            except Exception as exc:
                results.append(exc)
        worker = threading.Thread(target=serve)
        worker.start()
        try:
            if mode == 'exchange':
                assert len(journal.exchange(client, operation_id=operation, timeout_seconds=4,
                                            provider_uid=200000, provider_gid=200000)) == 64
            else:
                if mode == 'context_mismatch':
                    expected = {'command_id': command.command_id, 'run_id': command.run_id,
                        'target_id': command.target_id, 'fencing_generation': command.fencing_generation + 1,
                        'manifest_digest': manifest.digest(), 'source_backup_digest': backup, 'role': 'candidate'}
                    with pytest.raises(CustodianAdmissionError):
                        journal.execute_private_preparation(client, operation_id=operation, timeout_seconds=4,
                            provider_uid=200000, provider_gid=200000, copy_policy_path=copy_policy,
                            owner_policy_path=owner_policy, expected_context=expected)
                    assert not (stage / (operation + '.quarantine')).exists()
                    assert journal._db.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 0
                elif mode == 'deadline':
                    original_stage, original_check = journal.stage_quarantined_backup, journal._check
                    delayed = [False]
                    def mark_stage(*args, **kwargs):
                        delayed[0] = True
                        return original_stage(*args, **kwargs)
                    def slow_check():
                        if delayed[0]:
                            delayed[0] = False
                            time.sleep(1.2)
                        return original_check()
                    monkeypatch.setattr(journal, 'stage_quarantined_backup', mark_stage)
                    monkeypatch.setattr(journal, '_check', slow_check)
                    with pytest.raises(CustodianAdmissionError):
                        journal.execute_private_preparation(client, operation_id=operation, timeout_seconds=1,
                            provider_uid=200000, provider_gid=200000, copy_policy_path=copy_policy, owner_policy_path=owner_policy)
                    assert not (stage / (operation + '.quarantine')).exists()
                    assert journal._db.execute('SELECT COUNT(*) FROM custody_holds').fetchone()[0] == 0
                    assert journal._db.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 1
                else:
                    prepared = journal.execute_private_preparation(client, operation_id=operation, timeout_seconds=4,
                        provider_uid=200000, provider_gid=200000, copy_policy_path=copy_policy, owner_policy_path=owner_policy)
                    assert prepared['state'] == 'private_owner_prepared'
                    assert (stage / (operation + '.quarantine')).stat().st_uid == 10002
                    assert journal._db.execute('SELECT operation_id FROM custody_holds').fetchone() == (operation,)
        finally:
            worker.join(timeout=5)
            if worker.is_alive():
                server.close()
                worker.join(timeout=5)
        assert not worker.is_alive()
        # The child may exit after journal commit while the producer performs
        # its final peer-liveness check. That is a legitimate unconfirmed send.
        assert results == ['sent_unconfirmed'] or (len(results) == 1 and type(results[0]) is LaunchSupervisorError)
        row = journal._db.execute('SELECT operation_id,payload,signature FROM admissions').fetchone()
        if mode == 'context_mismatch':
            assert row is None
        else:
            assert row is not None and row[0] == operation
            key.public_key().verify(row[2], DOMAIN + row[1])
            recorded = json.loads(row[1])
            assert recorded['operation_id'] == operation and recorded['command_id'] == command.command_id
            assert recorded['manifest_digest'] == manifest.digest() and recorded['role'] == 'candidate'
