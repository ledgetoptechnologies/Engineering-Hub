"""Synthetic root-enrolled worker; no host daemon or installed activation."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from types import MethodType, SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile, canonical
from hub_container_executor.launch_supervisor import LaunchSupervisor, _boot_id, _proc_start
from hub_container_executor.ledger import SQLiteLedger
from hub_container_executor.state_custodian_admission import CustodianAdmissionJournal
from hub_container_executor.state_custodian_worker import SCHEMA, CustodianWorkerError, load_custodian_operation, _policy_pins
from tests.test_executor_launch_profile import gate_profile
from tests.test_hub_container_executor_ledger import request
from tests.test_hub_container_pilot_manifest import make_manifest_data
import hub_container_executor.ledger as ledger_module

pytestmark = pytest.mark.skipif(os.name != 'posix' or os.geteuid() != 0, reason='synthetic Linux root test')


@pytest.mark.parametrize('synthetic_maps', [False, True])
def test_worker_cli_binds_signed_operation_and_holds_private_uncertainty(monkeypatch, synthetic_maps):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    with tempfile.TemporaryDirectory(prefix='ehcw-', dir='/tmp') as temporary:
        root = Path(temporary); root.chmod(0o711)
        private = root / 'private'; private.mkdir(mode=0o700)
        source_dir = root / 'source'; source_dir.mkdir(mode=0o700)
        stage = root / 'stage'; stage.mkdir(mode=0o700)
        listening = root / 'listen'; listening.mkdir(mode=0o710); os.chown(listening, 0, 200000)
        source = source_dir / 'backup.sqlite'
        db = sqlite3.connect(source); db.execute('CREATE TABLE item(value TEXT)'); db.commit(); db.close(); source.chmod(0o600)
        backup = 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()
        data = make_manifest_data(); data['state_policy']['baseline_backup_digest'] = backup
        manifest = PilotManifest.model_validate(data)
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        profile_data = gate_profile()
        profile_data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
            'rollback_executable_digest': 'sha256:' + 'b' * 64, 'health_contract': 'engineering-hub.health-json-build-commit.v1'}
        profile_data['gate_ledger_channel'] = 'supervisor-listener-v1'
        profile_data['generation_isolation'] = {'kind': 'separate-provider-v1', 'app_uid': 10002, 'app_gid': 10002,
            'provider_host_uid': 200000, 'provider_host_gid': 200000, 'provider_public_key': '1' * 64}
        fake_map = '0 10002 1\n1 100001 65536\n'
        policies = {'journal_enrollment': {'schema': 2, 'supervisor_public_key': public, 'manifest_digest': manifest.digest(),
            'source_backup_digest': backup, 'provider_key_digest': 'sha256:' + hashlib.sha256(bytes.fromhex('1' * 64)).hexdigest(),
            'provider_uid': 200000, 'provider_gid': 200000, 'journal_path': str(private / 'journal')},
            'copy_policy': {'schema': 1, 'source': str(source), 'source_digest': backup, 'staging_directory': str(stage),
                'maximum_bytes': 1024 * 1024, 'maximum_total_bytes': 4 * 1024 * 1024, 'minimum_free_bytes': 1024},
            'owner_policy': {'schema': 1, 'boot_id': _boot_id(), 'daemon_pid': os.getpid(),
                'daemon_start_ticks': _proc_start(os.getpid()), 'uid_map': fake_map, 'gid_map': fake_map,
                'app_uid': 10002, 'app_gid': 10002}}
        pins = {}
        for name, value in policies.items():
            path = private / name; raw = canonical(value); path.write_bytes(raw); path.chmod(0o600)
            pins[name + '_path'] = str(path); pins[name + '_digest'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
        profile_data['custodian_control'] = dict(kind='protected-listener-v1', listener_path=str(listening / 'issuer.sock'),
                                                 journal_path=str(private / 'journal'), **pins)
        profile = ExecutorLaunchProfile.model_validate(profile_data)
        ledger = SQLiteLedger(private / 'ledger.db', manifest_digest=manifest.digest(),
            candidate_commit=manifest.candidate.source_commit, policy_revision=manifest.policy_revision)
        journal = CustodianAdmissionJournal(enrollment_path=private / 'journal_enrollment', journal_path=private / 'journal', create=True)
        journal.close()
        try:
            command = request().model_copy(update={'manifest_digest': manifest.digest(), 'candidate_commit': manifest.candidate.source_commit})
            assert ledger.admit(command).accepted
            operation_id = ledger.begin_operation(command.command_id, 'state_copy')
            operation_value = {'schema': SCHEMA, 'profile_json': canonical(profile_data).decode(), 'profile_digest': profile.digest(),
                'supervisor_public_key': public, 'operation_id': operation_id, 'command_id': command.command_id,
                'run_id': command.run_id, 'target_id': command.target_id, 'fencing_generation': command.fencing_generation,
                'manifest_digest': manifest.digest(), 'source_backup_digest': backup, 'role': 'candidate', 'timeout_ms': 5000}
            operation_path = private / 'operation.json'; operation_path.write_bytes(canonical(operation_value)); operation_path.chmod(0o600)
            operation = load_custodian_operation(str(operation_path)); _policy_pins(operation)
            assert operation.operation_id == operation_id
            bad = dict(operation_value, fencing_generation=True)
            operation_path.write_bytes(canonical(bad)); operation_path.chmod(0o600)
            with pytest.raises(CustodianWorkerError): load_custodian_operation(str(operation_path))
            operation_path.write_bytes(canonical(operation_value)); operation_path.chmod(0o600)
            supervisor = SimpleNamespace(_pid=os.getpid(), profile=profile, _signer=key,
                release=SimpleNamespace(launch_profile_digest=profile.digest()),
                _daemon_pin=SimpleNamespace(pid=os.getpid(), start_ticks=_proc_start(os.getpid())),
                _check_daemon_maps=lambda: None, _handoff_manifest_digest=lambda: manifest.digest(),
                _observation_mutex=lambda deadline: nullcontext(), _validate_live=lambda deadline: None,
                pending_state_context=lambda **kwargs: ledger.pending_state_context(kwargs['operation_id']))
            supervisor.serve_state_custodian_admission = MethodType(LaunchSupervisor.serve_state_custodian_admission, supervisor)
            results = []
            def serve():
                try: results.append(LaunchSupervisor.serve_protected_state_custodian(supervisor, manifest=manifest, timeout_seconds=5))
                except Exception as exc: results.append(exc)
            thread = threading.Thread(target=serve); thread.start()
            try:
                deadline = time.monotonic() + 3
                while True:
                    try:
                        ready = (listening / 'issuer.sock').lstat()
                        if stat.S_ISSOCK(ready.st_mode) and ready.st_uid == 0 and ready.st_gid == 200000 and stat.S_IMODE(ready.st_mode) == 0o660:
                            break
                    except FileNotFoundError:
                        pass
                    if time.monotonic() >= deadline: pytest.fail('owned listener not ready')
                    time.sleep(.01)
                # Explicitly synthetic PID/maps; the false branch uses real
                # maps and must deny. The true branch exercises private effects
                # with test-only map reads and is not host isolation evidence.
                prefix = ''
                if synthetic_maps:
                    script = ("def fake(p,*a,**k):\n if str(p) in %r: return %r\n return old(p,*a,**k)\n"
                              % ([f'/proc/{os.getpid()}/uid_map', f'/proc/{os.getpid()}/gid_map'], fake_map))
                    prefix = "from pathlib import Path; old=Path.read_text; exec(%r); Path.read_text=fake; " % script
                source_root = Path(__file__).resolve().parents[1]
                invoke = "import runpy,sys; sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)]; " + prefix + "runpy.run_module('hub_container_executor.state_custodian_worker',run_name='__main__')"
                result = subprocess.run([sys.executable, '-I', '-c', invoke, str(source_root), str(source_root / 'src'), '--operation-file', str(operation_path)],
                    capture_output=True, text=True, timeout=8, cwd='/', env={'LANG': 'C.UTF-8'})
                if synthetic_maps:
                    assert result.returncode == 0 and result.stdout == 'private_prepared\n' and result.stderr == ''
                else:
                    assert result.returncode == 2 and result.stdout == ''
                    assert result.stderr == 'custodian_unavailable_preserve_evidence\n'
            finally:
                thread.join(timeout=7)
            assert not thread.is_alive()
            journal = CustodianAdmissionJournal(enrollment_path=private / 'journal_enrollment', journal_path=private / 'journal', create=False)
            try:
                assert journal._db.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 1
                assert journal._db.execute('SELECT COUNT(*) FROM custody_holds').fetchone()[0] == (1 if synthetic_maps else 0)
                assert (stage / (operation_id + '.quarantine')).exists()
                if synthetic_maps:
                    assert (stage / (operation_id + '.quarantine')).stat().st_uid == 110002
            finally: journal.close()
        finally:
            ledger.close()
