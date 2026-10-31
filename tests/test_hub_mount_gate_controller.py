"""Real local seqpacket exchange; all authority callbacks are synthetic."""
import json
import array
import os
import socket
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub_container_executor import application_mount_gate as bootstrap
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.mount_gate_controller import MountGateController, MountGateControllerError, _read_observation
from tests.test_hub_application_mount_gate import pins, TOKENS


def resources(tmp_path):
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    expected = pins(tmp_path)
    expected.update(host_pid=peer.pid, process_start_ticks=peer.start)
    return client, server, peer, expected


def observation(expected, **changes):
    value = dict(protocol='engineering-hub.application-mount-observation.v1', identity=expected,
        observed_device=expected['expected_device'], observed_inode=expected['expected_inode'])
    value.update(changes)
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def test_received_rights_are_cloexec_quarantined_and_closed():
    read_fd, write_fd = os.pipe()
    received = os.dup(read_fd)
    seen = []
    class FakeChannel:
        def recvmsg(self, size, ancillary_size, flags):
            seen.append(flags)
            rights = array.array('i', [received]).tobytes()
            return b'{}', [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)], 0, None
    try:
        with pytest.raises(MountGateControllerError): _read_observation(FakeChannel())
        assert seen == [socket.MSG_CMSG_CLOEXEC]
        with pytest.raises(OSError): os.fstat(received)
        os.fstat(read_fd)  # The sender's original descriptor remains owned.
    finally:
        os.close(read_fd); os.close(write_fd)


def test_controller_and_actual_bootstrap_exchange_before_exec(tmp_path, monkeypatch):
    client, server, peer, expected = resources(tmp_path)
    monkeypatch.setattr(bootstrap, '_TARGET', str(tmp_path))
    key, events, errors = Ed25519PrivateKey.generate(), [], []
    controller = MountGateController(channel=server, peer=peer, expected=expected, signing_key=key,
        verify_live=lambda _: events.append('live') or True,
        reserve_issuance=lambda decision, digest: events.append(('reserve', decision['fence'], len(digest))) or True,
        hold_uncertain=lambda _: events.append('hold'), timeout_seconds=5)
    class Executed(BaseException): pass
    monkeypatch.setattr(bootstrap.os, 'execve', lambda *_: (_ for _ in ()).throw(Executed()))
    def serve():
        try: events.append(controller.serve_once())
        except BaseException as error: errors.append(error)
    worker = threading.Thread(target=serve); worker.start()
    try:
        with pytest.raises(Executed):
            bootstrap.run_gate(channel=client, public_key=key.public_key(), expected=expected,
                consume_challenge=lambda _: events.append('consume') or True,
                application_tokens=TOKENS, timeout_seconds=5)
        worker.join(5)
        assert not worker.is_alive() and not errors
        assert events[:3] == ['live', ('reserve', expected['fence'], 64), 'live']
        assert 'consume' in events and 'sent_unconfirmed' in events and 'hold' not in events
        with pytest.raises(MountGateControllerError): controller.serve_once()
    finally:
        client.close(); server.close(); peer.close()


@pytest.mark.parametrize('fault', ['wrong_inode', 'wrong_identity', 'live_denied', 'reservation_unknown', 'live_after_reserve'])
def test_rejects_wrong_evidence_and_holds_possible_reservation(tmp_path, fault):
    client, server, peer, expected = resources(tmp_path)
    calls = []
    def live(_):
        calls.append('live')
        return fault != 'live_denied' and not (fault == 'live_after_reserve' and calls.count('live') == 2)
    def reserve(*_):
        calls.append('reserve')
        if fault == 'reservation_unknown': raise RuntimeError('private error')
        return True
    controller = MountGateController(channel=server, peer=peer, expected=expected,
        signing_key=Ed25519PrivateKey.generate(), verify_live=live, reserve_issuance=reserve,
        hold_uncertain=lambda _: calls.append('hold'), timeout_seconds=1)
    wire = observation(expected, observed_inode=expected['expected_inode'] + 1) if fault == 'wrong_inode' else observation(expected)
    if fault == 'wrong_identity':
        changed = dict(expected, fence=expected['fence'] + 1); wire = observation(changed)
    try:
        client.send(wire)
        with pytest.raises(MountGateControllerError, match='mount gate controller unavailable'):
            controller.serve_once()
        client.settimeout(.05)
        with pytest.raises(TimeoutError): client.recv(8192)
        assert ('hold' in calls) == (fault in {'reservation_unknown', 'live_after_reserve'})
        assert ('reserve' in calls) == (fault in {'reservation_unknown', 'live_after_reserve'})
    finally:
        client.close(); server.close(); peer.close()


def test_late_committed_reservation_is_held_and_never_sent(tmp_path):
    client, server, peer, expected = resources(tmp_path)
    calls = []
    def reserve(*_):
        calls.append('committed')
        time.sleep(.12)
        return True
    controller = MountGateController(channel=server, peer=peer, expected=expected,
        signing_key=Ed25519PrivateKey.generate(), verify_live=lambda _: True,
        reserve_issuance=reserve, hold_uncertain=lambda _: calls.append('hold'), timeout_seconds=.08)
    try:
        client.send(observation(expected))
        with pytest.raises(MountGateControllerError): controller.serve_once()
        assert calls == ['committed', 'hold']
        client.settimeout(.05)
        with pytest.raises(TimeoutError): client.recv(8192)
    finally:
        client.close(); server.close(); peer.close()


def test_delegated_writer_has_wrong_kernel_pid(tmp_path):
    client, server, peer, expected = resources(tmp_path)
    calls = []
    controller = MountGateController(channel=server, peer=peer, expected=expected,
        signing_key=Ed25519PrivateKey.generate(), verify_live=lambda _: calls.append('live') or True,
        reserve_issuance=lambda *_: calls.append('reserve') or True,
        hold_uncertain=lambda _: calls.append('hold'), timeout_seconds=1)
    child = os.fork()
    if child == 0:
        try:
            server.close(); client.send(observation(expected)); os._exit(0)
        except BaseException:
            os._exit(1)
    try:
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        with pytest.raises(MountGateControllerError): controller.serve_once()
        assert not calls
    finally:
        client.close(); server.close(); peer.close()
