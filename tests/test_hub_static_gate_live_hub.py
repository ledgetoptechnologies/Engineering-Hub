"""Opt-in local test: synthetic custody, real gate, controllers, ledger and Hub.

The container entrypoint is overridden to run this parent process; it passes FD 3
to the exact image gate executable. This does not test Docker FD provisioning.
"""
from contextlib import ExitStack
import hashlib
import json
import os
import signal
import socket
import time
import urllib.error
import urllib.request

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hub_container_executor import ledger as ledger_module
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.mount_gate_start_enrollment import MountGateStartEnrollmentController
from hub_container_executor.mount_gate_controller import MountGateController, MountGateControllerError
from hub_container_executor.mount_gate_consume_protocol import MountGateConsumeController
from hub_container_executor.mount_gate_protocol import issue_decision
from tests.test_hub_container_executor_ledger import gate_fixture, open_ledger


def _reap(child):
    if child[0] is None: return
    try: os.kill(child[0], signal.SIGKILL)
    except ProcessLookupError: pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try: pid, _ = os.waitpid(child[0], os.WNOHANG)
        except ChildProcessError: child[0] = None; return
        if pid: child[0] = None; return
        time.sleep(.01)
    raise RuntimeError('test child did not exit')


@pytest.mark.skipif(os.environ.get('HUB_GATE_LIVE_TEST') != '1', reason='requires isolated test image')
@pytest.mark.parametrize('allow', [True, False])
def test_gate_starts_actual_hub_or_denies(tmp_path, monkeypatch, allow):
    with ExitStack() as owned:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.URLError):
            opener.open('http://127.0.0.1:8770/health', timeout=1)
        monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
        ledger = open_ledger(tmp_path); owned.callback(ledger.close)
        _, _, pins = gate_fixture(ledger, require_gate=True)
        target = '/var/lib/hub'; stat = os.stat(target); flags = os.statvfs(target).f_flag
        mount_flags = sorted(name for name, bit in [('nodev', os.ST_NODEV), ('noexec', os.ST_NOEXEC),
                                                    ('nosuid', os.ST_NOSUID)] if flags & bit)
        if not flags & os.ST_RDONLY: mount_flags.append('rw')
        pins.update(expected_device=stat.st_dev, expected_inode=stat.st_ino, mount_flags=mount_flags)
        key = Ed25519PrivateKey.from_private_bytes(b'\x01' * 32)
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        owned.callback(client.close); owned.callback(server.close)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        binary = '/usr/local/libexec/hub-application-gate'
        env = {'CODEX_TOKEN': 'synthetic-codex', 'HERMES_TOKEN': 'synthetic-hermes',
               'CLAUDE_TOKEN': 'synthetic-claude'}
        pid = os.posix_spawn(binary, [binary], env, file_actions=[(os.POSIX_SPAWN_DUP2, client.fileno(), 3),
                                                                  (os.POSIX_SPAWN_CLOSE, server.fileno())])
        child = [pid]; owned.callback(_reap, child); client.close()
        peer = PeerPin(pid=pid, uid=os.getuid(), gid=os.getgid(), start_ticks=_start(pid)); owned.callback(peer.close)
        pins.update(host_pid=pid, process_start_ticks=peer.start)
        held = []; decisions = []
        def reserve_start(payload, _digest):
            ledger.reserve_mount_gate_challenge(payload['identity']); return True
        def reserve_decision(payload, _digest):
            decisions.append(issue_decision(payload, key)); return True
        with open(binary, 'rb') as stream: executable_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        enrollment = MountGateStartEnrollmentController(channel=server, peer=peer,
            expected_without_nonce={k: v for k, v in pins.items() if k != 'nonce'},
            bootstrap_digest='sha256:' + executable_digest, signing_key=key, enrolled_public_key=pub,
            verify_live=lambda _: True, reserve_start=reserve_start,
            hold_uncertain=lambda _: held.append(True), timeout_seconds=5)
        pins = enrollment.serve_once()['identity']
        wall, tick = int(time.time() * 1000), time.monotonic()
        controller = MountGateController(channel=server, peer=peer, expected=pins, signing_key=key,
            verify_live=lambda _: allow, reserve_issuance=reserve_decision,
            hold_uncertain=lambda _: held.append(True), timeout_seconds=5)
        if not allow:
            with pytest.raises(MountGateControllerError): controller.serve_once()
            server.close()
            with ledger._connection() as db:
                assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == 'reserved'
            deadline = time.monotonic() + 5
            while True:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    child[0] = None
                    break
                assert time.monotonic() < deadline, 'denied gate did not exit'
                time.sleep(.01)
            assert os.waitstatus_to_exitcode(status) != 0
            with pytest.raises(urllib.error.URLError): opener.open('http://127.0.0.1:8770/health', timeout=1)
            return
        assert controller.serve_once() == 'sent_unconfirmed'
        consumer = MountGateConsumeController(channel=server, peer=peer, expected=pins,
            decision_wire=decisions[0], signing_key=key, enrolled_public_key=pub,
            consume_challenge=ledger.consume_mount_gate_challenge, verify_live=lambda _: True,
            hold_uncertain=lambda _: held.append(True), exchange_wall_ms=wall, exchange_monotonic=tick,
            timeout_seconds=5)
        assert consumer.serve_once() == 'sent_unconfirmed'
        with ledger._connection() as db:
            assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == 'consumed'
        deadline = time.monotonic() + 20
        while True:
            try:
                with opener.open('http://127.0.0.1:8770/health', timeout=1) as response:
                    body = json.load(response)
                break
            except urllib.error.URLError:
                assert time.monotonic() < deadline, 'Hub did not become healthy'
                time.sleep(.1)
        assert body['ok'] is True and body['service'] == 'engineering-hub-control'
        assert body['build_commit'] == os.environ['HUB_GATE_EXPECTED_COMMIT']
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            child[0] = None  # waitpid reaped it; never signal a potentially reused PID.
            pytest.fail(f'original gate child exited with status {status}')
        with open(f'/proc/{pid}/cmdline', 'rb') as stream:
            cmdline = stream.read().split(b'\0')
        assert cmdline[:5] == [b'/opt/venv/bin/python', b'-I', b'-B', b'-m', b'engineering_hub']
        assert not held
