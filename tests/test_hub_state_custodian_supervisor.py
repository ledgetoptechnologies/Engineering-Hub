"""Real credentialled socket against a synthetic supervisor/ledger fixture."""
import hashlib
import json
import os
import shutil
import socket
import tempfile
import time
from contextlib import nullcontext
from types import SimpleNamespace
from types import MethodType
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
from hub_container_executor.launch_supervisor import LaunchSupervisor, LaunchSupervisorError, _canonical, _boot_id
from hub_container_executor.ledger import SQLiteLedger
from hub_container_executor.state_custodian_admission import DOMAIN
from tests.test_executor_launch_profile import gate_profile
from tests.test_hub_container_executor_ledger import request
from tests.test_hub_container_pilot_manifest import make_manifest_data
import hub_container_executor.ledger as ledger_module


@pytest.mark.skipif(os.name != 'posix' or not hasattr(os, 'fork') or os.geteuid() != 0,
                    reason='requires isolated Linux root for distinct synthetic peer UID')
def test_supervisor_issues_peer_bound_snapshot_from_current_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    manifest = PilotManifest.model_validate(make_manifest_data())
    data = gate_profile()
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    data['generation_isolation'] = dict(kind='separate-provider-v1', app_uid=10002,
        app_gid=10002, provider_host_uid=200000, provider_host_gid=200000, provider_public_key='1' * 64)
    profile = ExecutorLaunchProfile.model_validate(data)
    key = Ed25519PrivateKey.generate()
    private = tmp_path / 'private'; private.mkdir(mode=0o700)
    ledger = SQLiteLedger(private / 'ledger.db', manifest_digest=manifest.digest(),
        candidate_commit=manifest.candidate.source_commit, policy_revision=manifest.policy_revision)
    command = request().model_copy(update={'manifest_digest': manifest.digest(),
        'candidate_commit': manifest.candidate.source_commit})
    ledger.admit(command)
    operation = ledger.begin_operation(command.command_id, 'state_copy')
    supervisor = SimpleNamespace(_pid=os.getpid(), profile=profile,
        release=SimpleNamespace(launch_profile_digest=profile.digest()), _signer=key,
        _handoff_manifest_digest=lambda: manifest.digest(),
        _observation_mutex=lambda deadline: nullcontext(), _validate_live=lambda deadline: None,
        pending_state_context=lambda **kwargs: ledger.pending_state_context(kwargs['operation_id']))
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    ready_read, ready_write = os.pipe(); release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            server.close(); os.close(ready_read); os.close(release_write)
            os.setgid(200000); os.setuid(200000)
            client.settimeout(5)
            client.send(_canonical({'protocol': 'engineering-hub.state-custodian-request.v1',
                'operation_id': operation, 'nonce': 'a' * 64}))
            response = client.recv(8192)
            os.write(ready_write, response)
            os.read(release_read, 1)
            os._exit(0)
        except BaseException:
            os._exit(1)
    client.close(); os.close(ready_write); os.close(release_read)
    try:
        assert LaunchSupervisor.serve_state_custodian_admission(supervisor, channel=server,
            manifest=manifest, timeout_seconds=4) == 'sent_unconfirmed'
        envelope = json.loads(os.read(ready_read, 8192))
        payload = envelope['payload']
        key.public_key().verify(bytes.fromhex(envelope['signature']), DOMAIN + _canonical(payload))
        assert payload['operation_id'] == operation and payload['nonce'] == 'a' * 64
        assert payload['manifest_digest'] == manifest.digest()
        assert payload['source_backup_digest'] == manifest.state_policy.baseline_backup_digest
        assert payload['provider_key_digest'] == 'sha256:' + hashlib.sha256(bytes.fromhex('1' * 64)).hexdigest()
        assert payload['role'] == 'candidate' and payload['command_id'] == command.command_id
        assert 0 < payload['expires_at_ms'] - payload['issued_at_ms'] <= 5000
    finally:
        try:
            os.write(release_write, b'x')
        except BrokenPipeError:
            pass
        os.close(release_write); os.close(ready_read); server.close()
        _, status = os.waitpid(pid, 0)
        ledger.close()
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


@pytest.mark.skipif(os.name != 'posix' or not hasattr(os, 'fork') or os.geteuid() != 0,
                    reason='requires isolated Linux root for distinct synthetic peer UID')
@pytest.mark.parametrize('drift', ['none', 'parent', 'policy'])
def test_protected_listener_binds_signed_policies_and_real_provider(monkeypatch, drift):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    root = Path(tempfile.mkdtemp(prefix='eh-custodian-listener-', dir='/tmp'))
    os.chmod(root, 0o711)
    private = root / 'private'; private.mkdir(mode=0o700)
    listening = root / 'listen'; listening.mkdir(mode=0o710); os.chown(listening, 0, 200000)
    manifest = PilotManifest.model_validate(make_manifest_data())
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    data = gate_profile()
    data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    data['gate_ledger_channel'] = 'supervisor-listener-v1'
    data['generation_isolation'] = dict(kind='separate-provider-v1', app_uid=10002,
        app_gid=10002, provider_host_uid=200000, provider_host_gid=200000, provider_public_key='1' * 64)
    uid_map = '0 10002 1\n1 100001 65536\n'
    enrollment = {'schema': 2, 'supervisor_public_key': public, 'manifest_digest': manifest.digest(),
        'source_backup_digest': manifest.state_policy.baseline_backup_digest,
        'provider_key_digest': 'sha256:' + hashlib.sha256(bytes.fromhex('1' * 64)).hexdigest(),
        'provider_uid': 200000, 'provider_gid': 200000, 'journal_path': str(private / 'journal')}
    copy = {'source_digest': manifest.state_policy.baseline_backup_digest}
    owner = {'schema': 1, 'boot_id': _boot_id(), 'daemon_pid': os.getpid(), 'daemon_start_ticks': 123,
             'uid_map': uid_map, 'gid_map': uid_map, 'app_uid': 10002, 'app_gid': 10002}
    paths = {}
    for name, value in (('journal_enrollment', enrollment), ('copy_policy', copy), ('owner_policy', owner)):
        path = private / name; raw = _canonical(value); path.write_bytes(raw); os.chmod(path, 0o600)
        paths[name + '_path'] = str(path); paths[name + '_digest'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
    data['custodian_control'] = dict(kind='protected-listener-v1', listener_path=str(listening / 'issuer.sock'),
        journal_path=str(private / 'journal'), **paths)
    profile = ExecutorLaunchProfile.model_validate(data)
    ledger = SQLiteLedger(private / 'ledger.db', manifest_digest=manifest.digest(),
        candidate_commit=manifest.candidate.source_commit, policy_revision=manifest.policy_revision)
    command = request().model_copy(update={'manifest_digest': manifest.digest(),
        'candidate_commit': manifest.candidate.source_commit})
    ledger.admit(command); operation = ledger.begin_operation(command.command_id, 'state_copy')
    calls = []
    def live(_deadline):
        calls.append(True)
        if len(calls) == 2:
            if drift == 'parent': os.chmod(listening, 0o711)
            if drift == 'policy': Path(paths['copy_policy_path']).write_bytes(b'{}')
    supervisor = SimpleNamespace(_pid=os.getpid(), profile=profile, _signer=key,
        release=SimpleNamespace(launch_profile_digest=profile.digest()),
        _daemon_pin=SimpleNamespace(pid=os.getpid(), start_ticks=123), _check_daemon_maps=lambda: None,
        _handoff_manifest_digest=lambda: manifest.digest(), _observation_mutex=lambda deadline: nullcontext(),
        _validate_live=live,
        pending_state_context=lambda **kwargs: ledger.pending_state_context(kwargs['operation_id']))
    supervisor.serve_state_custodian_admission = MethodType(LaunchSupervisor.serve_state_custodian_admission, supervisor)
    ready_read, ready_write = os.pipe(); release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(ready_read); os.close(release_write); os.setgid(200000); os.setuid(200000)
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET); channel.settimeout(4)
            deadline = time.monotonic() + 3
            while True:
                try: channel.connect(str(listening / 'issuer.sock')); break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() >= deadline: raise
                    time.sleep(.01)
            channel.send(_canonical({'protocol': 'engineering-hub.state-custodian-request.v1',
                                     'operation_id': operation, 'nonce': 'c' * 64}))
            response = channel.recv(8192)
            os.write(ready_write, response or b'EOF'); os.read(release_read, 1); os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(ready_write); os.close(release_read)
    try:
        if drift != 'none':
            with pytest.raises(LaunchSupervisorError):
                LaunchSupervisor.serve_protected_state_custodian(supervisor, manifest=manifest, timeout_seconds=4)
            assert os.read(ready_read, 8192) == b'EOF'
        else:
            assert LaunchSupervisor.serve_protected_state_custodian(supervisor, manifest=manifest,
                timeout_seconds=4) == 'sent_unconfirmed'
            response = json.loads(os.read(ready_read, 8192))
            key.public_key().verify(bytes.fromhex(response['signature']), DOMAIN + _canonical(response['payload']))
            assert response['payload']['operation_id'] == operation and response['payload']['nonce'] == 'c' * 64
        assert (listening / 'issuer.sock').exists()
    finally:
        try: os.write(release_write, b'x')
        except BrokenPipeError: pass
        os.close(release_write); os.close(ready_read)
        _, status = os.waitpid(pid, 0); ledger.close(); shutil.rmtree(root)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
