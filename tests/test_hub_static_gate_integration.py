"""Opt-in test binary only; synthetic host/custody with real controllers and ledger."""
import hashlib
import os
import signal
import socket
import subprocess
import time
from contextlib import ExitStack

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub_container_executor import ledger as ledger_module
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.mount_gate_start_enrollment import MountGateStartEnrollmentController
from hub_container_executor.mount_gate_controller import MountGateController
from hub_container_executor.mount_gate_consume_protocol import MountGateConsumeController
from hub_container_executor.mount_gate_protocol import issue_decision
from tests.test_hub_container_executor_ledger import gate_fixture, open_ledger


@pytest.mark.skipif(not os.environ.get('HUB_STATIC_GATE_TEST_BINARY'), reason='requires explicitly built test binary')
@pytest.mark.parametrize('hostile_config', [False, True])
def test_static_receiver_with_real_controllers_and_ledger(tmp_path, monkeypatch, hostile_config):
    with ExitStack() as owned:
        _run_receiver_test(tmp_path, monkeypatch, hostile_config, owned)


def _reap_owned_child(child):
    if child[0] is None:
        return
    try:
        os.kill(child[0], signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            waited, _ = os.waitpid(child[0], os.WNOHANG)
        except ChildProcessError:
            child[0] = None
            return
        if waited:
            child[0] = None
            return
        time.sleep(.01)
    raise RuntimeError('owned static gate child did not exit after SIGKILL')


def _unlink_if_present(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _run_receiver_test(tmp_path, monkeypatch, hostile_config, owned):
    binary = os.environ['HUB_STATIC_GATE_TEST_BINARY']
    target = '/tmp/hub-static-gate-fixture'
    lock_path = target + '.lock'
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    owned.callback(os.unlink, lock_path)
    owned.callback(os.close, lock_fd)
    os.mkdir(target, 0o700)
    owned.callback(os.rmdir, target)
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    owned.callback(ledger.close)
    _, _, pins = gate_fixture(ledger)
    stat = os.stat(target); vf = os.statvfs(target).f_flag
    flags = sorted(name for name, bit in [('nodev', os.ST_NODEV), ('noexec', os.ST_NOEXEC),
                                           ('nosuid', os.ST_NOSUID)] if vf & bit)
    if not vf & os.ST_RDONLY: flags.append('rw')
    pins.update(expected_device=stat.st_dev, expected_inode=stat.st_ino, mount_flags=flags)
    key = Ed25519PrivateKey.from_private_bytes(b'\x01' * 32)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    path_mode = os.environ.get('HUB_STATIC_GATE_TEST_CHANNEL_MODE') == 'path'
    if path_mode:
        channel_dir = '/tmp/hub-static-gate-channel'
        channel_path = channel_dir + '/controller.sock'
        os.mkdir(channel_dir, 0o700); owned.callback(os.rmdir, channel_dir)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        owned.callback(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        listener.bind(channel_path); owned.callback(_unlink_if_present, channel_path)
        listener.listen(1); listener.settimeout(5)
        client = server = None
    else:
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        owned.callback(client.close); owned.callback(server.close)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    env = {'CODEX_TOKEN': 'test', 'HERMES_TOKEN': 'test', 'CLAUDE_TOKEN': 'test'}
    marker = '/tmp/hub-gate-hostile-provider-loaded'
    assert not os.path.lexists(marker)
    owned.callback(_unlink_if_present, marker)
    if hostile_config:
        provider = os.environ.get('HUB_STATIC_GATE_TEST_PROVIDER')
        if not provider: pytest.skip('requires explicitly built hostile test provider')
        config = tmp_path / 'hostile.cnf'
        config.write_text('openssl_conf = init\n[init]\nproviders = providers\n[providers]\n'
                          'hostile = hostile_section\n[hostile_section]\nactivate = 1\nmodule = ' + provider + '\n')
        env.update(OPENSSL_CONF=str(config), OPENSSL_MODULES=os.path.dirname(provider))
        subprocess.run(['/usr/bin/openssl', 'list', '-providers'], env=env,
                       capture_output=True, timeout=5, check=False)
        assert os.path.exists(marker), 'hostile OpenSSL configuration did not load the test provider'
        os.unlink(marker)
    actions = [] if path_mode else [(os.POSIX_SPAWN_DUP2, client.fileno(), 3),
                                   (os.POSIX_SPAWN_CLOSE, server.fileno())]
    pid = os.posix_spawn(binary, [binary], env, file_actions=actions)
    child = [pid]
    owned.callback(_reap_owned_child, child)
    if path_mode:
        server, _ = listener.accept(); owned.callback(server.close)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    else:
        client.close()
    peer = PeerPin(pid=pid, uid=os.getuid(), gid=os.getgid(), start_ticks=_start(pid))
    owned.callback(peer.close)
    pins.update(host_pid=pid, process_start_ticks=peer.start)
    held = []; captured = []
    def reserve_start(payload, _digest):
        ledger.reserve_mount_gate_challenge(payload['identity'])
        return True
    def reserve_decision(payload, _digest):
        captured.append(issue_decision(payload, key))
        return True
    try:
        start = MountGateStartEnrollmentController(channel=server, peer=peer,
            expected_without_nonce={k: v for k, v in pins.items() if k != 'nonce'},
            bootstrap_digest='sha256:' + hashlib.sha256(open(binary, 'rb').read()).hexdigest(),
            signing_key=key, enrolled_public_key=pub, verify_live=lambda _: True,
            reserve_start=reserve_start, hold_uncertain=lambda _: held.append(True), timeout_seconds=5)
        pins = start.serve_once()['identity']
        wall, tick = int(time.time() * 1000), time.monotonic()
        controller = MountGateController(channel=server, peer=peer, expected=pins, signing_key=key,
            verify_live=lambda _: True, reserve_issuance=reserve_decision,
            hold_uncertain=lambda _: held.append(True), timeout_seconds=5)
        assert controller.serve_once() == 'sent_unconfirmed'
        consumer = MountGateConsumeController(channel=server, peer=peer, expected=pins,
            decision_wire=captured[0], signing_key=key, enrolled_public_key=pub,
            consume_challenge=ledger.consume_mount_gate_challenge, verify_live=lambda _: True,
            hold_uncertain=lambda _: held.append(True), exchange_wall_ms=wall, exchange_monotonic=tick,
            timeout_seconds=5)
        assert consumer.serve_once() == 'sent_unconfirmed'
        with ledger._connection() as db:
            assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == 'consumed'
        deadline = time.monotonic() + 5
        while True:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited: child[0] = None; break
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert os.waitstatus_to_exitcode(status) == 0
        assert not held
        assert not os.path.exists(marker)
    finally:
        pass  # ExitStack owns resources even if setup or assertions fail.
