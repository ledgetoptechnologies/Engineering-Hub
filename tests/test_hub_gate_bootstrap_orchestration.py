"""Real app seqpacket and journals with synthetic daemon/ledger authority."""
import hashlib
import json
import os
import select
import signal
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import pytest

from hub_container_executor import launch_supervisor as launch
from hub_container_executor.broker_seqpacket import _start
from tests.test_hub_gate_socket_supervisor import socket_rig, v4_policy
from tests.test_hub_container_executor_launch_supervisor import short_root
from tests.test_hub_application_gate_enrollment import raw
from tests.test_hub_application_mount_gate import pins


@pytest.mark.parametrize('fault', ['none', 'reserve', 'consume'])
def test_supervisor_owns_bootstrap_decision_and_consume(socket_rig, tmp_path, monkeypatch, fault):
    supervisor, manifest, image, path, directory, held = socket_rig
    data = json.loads(Path(path).read_bytes()); v4_policy(supervisor, data); Path(path).write_bytes(raw(data))
    supervisor.prepare_gate_socket(manifest=manifest, image=image, enrollment_file=path, timeout_seconds=2)
    base = pins(tmp_path); base.pop('nonce')
    generations = tmp_path / 'generations'; generations.mkdir()
    generation = generations / base['generation_id']; generation.mkdir()
    info = generation.stat()
    base.update(expected_device=info.st_dev, expected_inode=info.st_ino, manifest_digest=manifest.digest(),
        image_config_id=image.image_config_id, runnable_digest=image.runnable_reference,
        platform_manifest_digest=image.platform_manifest_digest, implementation_commit=image.source_commit)
    supervisor._mounts['generations'] = SimpleNamespace(source=str(generations))
    supervisor._source_pins = {'generations': launch._identity(generations.stat())}
    ready_read, ready_write = os.pipe(); release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(ready_read); os.close(release_write); os.setgid(110001); os.setuid(110001)
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
    try:
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
            class Client:
                def invoke(self, action, value, **_kwargs):
                    operations.append((action, value))
                    if action == fault: raise RuntimeError('synthetic uncertain ledger response')
                    return True
            yield Client()
        supervisor.open_gate_ledger_client = ledger_client
        args = dict(manifest=manifest, image=image, enrollment_file=path,
                    controller_key_file=str(Path(path).parent / 'controller-key'), timeout_seconds=4)
        if fault == 'none':
            result = supervisor.serve_pending_gate_bootstrap(**args)
            assert result['nonce'] == 'a' * 64 and [item[0] for item in operations] == ['reserve', 'consume']
            assert select.select([ready_read], [], [], 3)[0] and os.read(ready_read, 1) == b'R'
            assert not held
        else:
            with pytest.raises(launch.LaunchSupervisorError): supervisor.serve_pending_gate_bootstrap(**args)
            assert held and [item[0] for item in operations] == (['reserve'] if fault == 'reserve' else ['reserve', 'consume'])
            with supervisor._journal.transaction() as db:
                assert db.execute('SELECT count(*) FROM gate_start').fetchone() == (1,)
                assert db.execute('SELECT count(*) FROM gate_issuance').fetchone() == (0 if fault == 'reserve' else 1,)
    finally:
        os.close(ready_read); os.close(release_write)
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


@pytest.mark.parametrize('fault', ['none', 'uncertain_acceptance', 'stop', 'timeout'])
def test_owned_sequence_only_retries_observational_waits(socket_rig, monkeypatch, fault):
    supervisor, manifest, image, path, _, held = socket_rig
    observed = []
    supervisor._gate_accepted_peer = SimpleNamespace(close=lambda: None)
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
                controller_key_file='synthetic', timeout_seconds=.15 if fault == 'timeout' else 2, stop_requested=stopped)
    if fault == 'none':
        assert supervisor.run_gate_operation(**args)['acceptance'] == {'accepted': True}
        assert observed == ['bootstrap', 'bootstrap', 'finish', 'finish'] and not held
    else:
        with pytest.raises(launch.LaunchSupervisorError): supervisor.run_gate_operation(**args)
        assert held
        if fault == 'uncertain_acceptance': assert observed.count('finish') == 1
        elif fault == 'timeout': assert observed and all(item == 'bootstrap' for item in observed)
        else: assert observed == []
