"""Real app seqpacket and journals with synthetic daemon/ledger authority."""
import hashlib
import json
import os
import select
import signal
import socket
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub.container_pilot_client import ContainerPilotRequest
from hub_container_executor import ledger as ledger_module
from hub_container_executor.ledger import SQLiteLedger
from hub_container_executor.broker_seqpacket import PeerPin
from hub_container_executor.gate_ledger_channel import GateLedgerWorker, SupervisorLedgerClient
from tests.test_hub_gate_ledger_channel import verified_launch

from hub_container_executor import launch_supervisor as launch
from hub_container_executor.broker_seqpacket import _start
from tests.test_hub_gate_socket_supervisor import socket_rig, v4_policy
from tests.test_hub_container_executor_launch_supervisor import short_root
from tests.test_hub_application_gate_enrollment import raw
from tests.test_hub_application_mount_gate import pins


@pytest.fixture
def owned_resources():
    with ExitStack() as stack:
        yield stack


@pytest.mark.parametrize('fault', ['none', 'real_ledger', 'static_real_ledger', 'reserve', 'consume', 'revoked'])
def test_supervisor_owns_bootstrap_decision_and_consume(socket_rig, tmp_path, monkeypatch, owned_resources, fault):
    supervisor, manifest, image, path, directory, held = socket_rig
    binary = os.environ.get('HUB_STATIC_GATE_TEST_BINARY')
    if fault == 'static_real_ledger' and not binary: pytest.skip('requires isolated v3 test binary')
    data = json.loads(Path(path).read_bytes()); v4_policy(supervisor, data); Path(path).write_bytes(raw(data))
    if fault == 'static_real_ledger':
        key = Ed25519PrivateKey.from_private_bytes(b'\x01' * 32)
        data['controller_public_key'] = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        data['bootstrap_digest'] = 'sha256:' + hashlib.sha256(Path(binary).read_bytes()).hexdigest()
        Path(path).write_bytes(raw(data))
        key_path = Path(path).parent / 'controller-key'
        key_path.write_bytes(b'\x01' * 32); key_path.chmod(0o600)
    supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2)
    base = pins(tmp_path); base.pop('nonce')
    if fault == 'static_real_ledger':
        lock_path = '/tmp/hub-static-gate-fixture.lock'
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        owned_resources.callback(os.unlink, lock_path)
        owned_resources.callback(os.close, lock_fd)
        generations = Path('/tmp/hub-static-gate-fixture'); generations.mkdir(mode=0o755); generations.chmod(0o755)
        owned_resources.callback(generations.rmdir)
        base['generation_id'] = 'generation'
        generation = generations / base['generation_id']; generation.mkdir(mode=0o755); generation.chmod(0o755)
        owned_resources.callback(generation.rmdir)
        channel_alias = Path('/tmp/hub-static-gate-channel'); channel_alias.symlink_to(directory, target_is_directory=True)
        owned_resources.callback(channel_alias.unlink)
        bits = os.statvfs(generation).f_flag
        base['mount_flags'] = sorted(name for name, bit in (('nodev', os.ST_NODEV), ('noexec', os.ST_NOEXEC),
                                                             ('nosuid', os.ST_NOSUID)) if bits & bit)
        if not bits & os.ST_RDONLY: base['mount_flags'].append('rw')
    else:
        generations = tmp_path / 'generations'; generations.mkdir()
        generation = generations / base['generation_id']; generation.mkdir()
    info = generation.stat()
    base.update(expected_device=info.st_dev, expected_inode=info.st_ino, manifest_digest=manifest.digest(),
        image_config_id=image.image_config_id, runnable_digest=image.runnable_reference,
        platform_manifest_digest=image.platform_manifest_digest, implementation_commit=image.source_commit)
    ledger = verifier = None
    if fault in ('real_ledger', 'static_real_ledger'):
        monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
        private = tmp_path / 'ledger-private'; private.mkdir(mode=0o700)
        ledger = SQLiteLedger(private / 'ledger.sqlite3', manifest_digest=manifest.digest(),
                              candidate_commit=manifest.candidate.source_commit, policy_revision=manifest.policy_revision)
        owned_resources.callback(ledger.close)
        command = ContainerPilotRequest(schema_version='container_pilot_v1', command_id=str(uuid4()), run_id=str(uuid4()),
            target_id='engineering-hub:staging', action='deploy_container', manifest_digest=manifest.digest(),
            candidate_commit=manifest.candidate.source_commit, policy_revision=manifest.policy_revision, fencing_generation=1)
        assert ledger.admit(command).accepted
        context = {'schema': 2, 'role': image.role, 'run_id': command.run_id, 'fencing_generation': command.fencing_generation,
            'manifest_digest': manifest.digest(), 'image_config_id': image.image_config_id,
            'generation_id': base['generation_id'], 'runnable_reference': image.runnable_reference,
            'platform_manifest_digest': image.platform_manifest_digest}
        operation = ledger.begin_operation(command.command_id, 'create_candidate', create_context=context)
        ledger.complete_operation(operation, {'container_id': base['container_id']})
        ledger.record_target_instance(command.command_id, base['container_id'], image.image_config_id, base['generation_id'])
        ledger.require_gate_outcome(command.command_id, operation)
        base.update(operation_id=operation, run_id=command.run_id, fence=command.fencing_generation)
        issuer_key = Ed25519PrivateKey.generate()
        issuer_public = issuer_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        verifier = verified_launch(issuer_key, issuer_public)
        owned_resources.callback(verifier.close)
    supervisor._mounts['generations'] = SimpleNamespace(source=str(generations))
    supervisor._source_pins = {'generations': launch._identity(generations.stat())}
    ready_read, ready_write = os.pipe(); release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(ready_read); os.close(release_write); os.setgid(110001); os.setuid(110001)
            if fault == 'static_real_ledger':
                os.execve(binary, [binary], {'CODEX_TOKEN': 'test', 'HERMES_TOKEN': 'test', 'CLAUDE_TOKEN': 'test'})
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET); peer.settimeout(4)
            peer.connect(str(directory / 'controller.sock')); os.write(ready_write, b'C')
            peer.send(launch._canonical({'protocol': 'engineering-hub.application-gate-start-request.v1', 'nonce': 'a' * 64}))
            identity = json.loads(peer.recv(8192))['payload']['identity']
            peer.send(launch._canonical({'protocol': 'engineering-hub.application-mount-observation.v1',
                'identity': identity, 'observed_device': identity['expected_device'], 'observed_inode': identity['expected_inode']}))
            decision = peer.recv(8192)
            peer.send(launch._canonical({'protocol': 'engineering-hub.application-gate-consume-request.v1',
                'nonce': identity['nonce'], 'launch_id': identity['launch_id'], 'operation_id': identity['operation_id'],
                'fence': identity['fence'], 'decision_sha256': hashlib.sha256(decision).hexdigest()}))
            assert peer.recv(8192)
            os.write(ready_write, b'R'); os.read(release_read, 1)
        except BaseException:
            try: os.write(ready_write, b'E')
            except OSError: pass
        os._exit(0)
    os.close(ready_write); os.close(release_read)
    reaped = False
    try:
        if fault == 'static_real_ledger':
            assert select.select([supervisor._gate_listener], [], [], 4)[0]
        else:
            assert select.select([ready_read], [], [], 4)[0] and os.read(ready_read, 1) == b'C'
        base.update(host_pid=child, process_start_ticks=_start(child))
        original = Path.read_text
        def status(self, *args, **kwargs):
            value = original(self, *args, **kwargs)
            if str(self) == f'/proc/{child}/status':
                value = '\n'.join(('NSpid:\t' + str(child) + '\t1') if line.startswith('NSpid:') else line
                                  for line in value.splitlines()) + '\n'
            elif str(self) in {f'/proc/{child}/uid_map', f'/proc/{child}/gid_map'}:
                value = '0 10002 1\n1 100001 65536\n'
            return value
        monkeypatch.setattr(Path, 'read_text', status)
        supervisor.observe_pending_gate_runtime = lambda **_: dict(base)
        supervisor._engine = SimpleNamespace(json=lambda *_: (200, {'Id': base['container_id'],
            'Image': image.image_config_id, 'State': {'Running': True, 'Status': 'running', 'Paused': False,
            'Restarting': False, 'Dead': False, 'Pid': child}}))
        monkeypatch.setattr(launch, '_measure_gate_process', lambda *_, **__: {})
        operations = []
        @contextmanager
        def ledger_client(**_):
            if fault in ('real_ledger', 'static_real_ledger'):
                client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                for endpoint in (client, server): endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
                worker = GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=issuer_public,
                    launch_verifier=verifier, manifest_digest=manifest.digest(), hold_uncertain=ledger.hold_gate_uncertain,
                    timeout_seconds=3, require_sender_liveness=True)
                actual = SupervisorLedgerClient(channel=client, peer=peer, signing_key=issuer_key,
                    launch_binding=verifier.launch_binding(), manifest_digest=manifest.digest(), timeout_seconds=3)
                results = []
                def serve():
                    try: results.append(worker.serve_once())
                    except BaseException as error: results.append(error)
                thread = threading.Thread(target=serve); thread.start()
                try:
                    yield actual
                finally:
                    client.close(); server.close(); thread.join(4); peer.close()
                    assert not thread.is_alive() and results == ['sent_unconfirmed']
                return
            class Client:
                def invoke(self, action, value, **_kwargs):
                    operations.append((action, value))
                    if action == fault: raise RuntimeError('synthetic uncertain ledger response')
                    return True
            yield Client()
        supervisor.open_gate_ledger_client = ledger_client
        checks = []
        def authority_check():
            checks.append(True)
            if fault == 'revoked' and len(checks) == 5: raise RuntimeError('approval changed')
        args = dict(manifest=manifest, image=image, enrollment_file=path,
                    controller_key_file=str(Path(path).parent / 'controller-key'), timeout_seconds=4,
                    authority_check=authority_check)
        if fault in ('none', 'real_ledger', 'static_real_ledger'):
            try:
                result = supervisor.serve_pending_gate_bootstrap(**args)
            except launch.LaunchSupervisorError:
                with supervisor._journal.transaction() as db:
                    counts = (db.execute('SELECT count(*) FROM gate_start').fetchone()[0],
                              db.execute('SELECT count(*) FROM gate_issuance').fetchone()[0])
                state = []
                if ledger is not None:
                    with ledger._connection() as db:
                        state = [row[0] for row in db.execute('SELECT state FROM mount_gate_challenges')]
                pytest.fail(f'synthetic bootstrap held: journal={counts}, ledger={state}')
            assert len(result['nonce']) == 64
            if fault != 'static_real_ledger': assert result['nonce'] == 'a' * 64
            if fault in ('real_ledger', 'static_real_ledger'):
                with ledger._connection() as db:
                    assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == 'consumed'
            else: assert [item[0] for item in operations] == ['reserve', 'consume']
            if fault == 'static_real_ledger':
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    waited, status_code = os.waitpid(child, os.WNOHANG)
                    if waited: reaped = True; assert os.waitstatus_to_exitcode(status_code) == 0; break
                    time.sleep(.01)
                assert reaped
            else: assert select.select([ready_read], [], [], 3)[0] and os.read(ready_read, 1) == b'R'
            assert not held
        else:
            with pytest.raises(launch.LaunchSupervisorError): supervisor.serve_pending_gate_bootstrap(**args)
            wanted = [] if fault == 'revoked' else ['reserve'] if fault == 'reserve' else ['reserve', 'consume']
            assert held and [item[0] for item in operations] == wanted
            with supervisor._journal.transaction() as db:
                assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (1,)
                assert db.execute('SELECT count(*) FROM gate_issuance').fetchone() == (1 if fault == 'consume' else 0,)
    finally:
        os.close(ready_read); os.close(release_write)
        if not reaped:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if os.waitpid(child, os.WNOHANG)[0] == child: break
                time.sleep(.01)
            else:
                os.kill(child, signal.SIGKILL)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if os.waitpid(child, os.WNOHANG)[0] == child: break
                    time.sleep(.01)
                else: raise AssertionError('owned child did not exit')


@pytest.mark.parametrize('fault', ['none', 'handoff_delay', 'uncertain_acceptance', 'stop', 'timeout'])
def test_owned_sequence_only_retries_observational_waits(socket_rig, monkeypatch, fault):
    supervisor, manifest, image, path, _, held = socket_rig
    observed = []
    supervisor._gate_accepted_peer = SimpleNamespace(close=lambda: None)
    handoff_checks = []
    def handoff_ready(_):
        handoff_checks.append(True)
        return fault != 'handoff_delay' or len(handoff_checks) >= 3
    monkeypatch.setattr(supervisor, '_gate_ledger_handoff_ready', handoff_ready)
    def bootstrap(**_):
        observed.append('bootstrap')
        return None if fault == 'timeout' or observed.count('bootstrap') == 1 else {'operation_id': 'synthetic'}
    def finish(**_):
        observed.append('finish')
        if fault == 'uncertain_acceptance': raise RuntimeError('uncertain')
        return None if observed.count('finish') == 1 else {'accepted': True}
    monkeypatch.setattr(supervisor, 'serve_pending_gate_bootstrap', bootstrap)
    monkeypatch.setattr(supervisor, 'finish_pending_gate', finish)
    stopped = lambda: fault == 'stop'
    args = dict(manifest=manifest, image=image, enrollment_file=path,
                controller_key_file='synthetic', timeout_seconds=.15 if fault == 'timeout' else 2,
                stop_requested=stopped, authority_check=lambda: None)
    if fault in ('none', 'handoff_delay'):
        assert supervisor.run_gate_operation(**args)['acceptance'] == {'accepted': True}
        assert observed == ['bootstrap', 'bootstrap', 'finish', 'finish'] and not held
        if fault == 'handoff_delay': assert len(handoff_checks) == 3
    else:
        with pytest.raises(launch.LaunchSupervisorError): supervisor.run_gate_operation(**args)
        assert held
        if fault == 'uncertain_acceptance': assert observed.count('finish') == 1
        elif fault == 'timeout': assert observed and all(item == 'bootstrap' for item in observed)
        else: assert observed == []


@pytest.mark.parametrize('kind', ['async', 'async_call', 'returned_coroutine', 'false', 'other'])
def test_authority_check_must_complete_synchronously_with_none(socket_rig, monkeypatch, kind):
    supervisor, manifest, image, path, _, held = socket_rig
    effects = []
    monkeypatch.setattr(supervisor, '_gate_ledger_handoff_ready', lambda _: True)
    monkeypatch.setattr(supervisor, 'serve_pending_gate_bootstrap', lambda **_: effects.append('effect'))
    async def async_check(): return None
    class AsyncCall:
        async def __call__(self): return None
    callbacks = {'async': async_check, 'async_call': AsyncCall(), 'returned_coroutine': lambda: async_check(),
                 'false': lambda: False, 'other': lambda: 'approved'}
    with pytest.raises(launch.LaunchSupervisorError):
        supervisor.run_gate_operation(manifest=manifest, image=image, enrollment_file=path,
            controller_key_file='synthetic', timeout_seconds=2, stop_requested=lambda: False,
            authority_check=callbacks[kind])
    assert held and effects == []
