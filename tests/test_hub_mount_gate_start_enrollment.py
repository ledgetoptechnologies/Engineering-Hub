"""Real seqpacket; synthetic owner/custodian authority, no launch enabled."""
import json
import os
import socket
import threading
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.mount_gate_start_enrollment import (
    MountGateStartEnrollmentController, StartEnrollmentError, REQUEST, verify_start_enrollment)
from tests.test_hub_application_mount_gate import pins


def setup(tmp_path, *, live=lambda _: True, reserve=lambda *_: True, hold=lambda _: None):
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    expected = pins(tmp_path); expected.update(host_pid=peer.pid, process_start_ticks=peer.start)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    controller = MountGateStartEnrollmentController(channel=server, peer=peer,
        expected_without_nonce={k:v for k,v in expected.items() if k != 'nonce'},
        bootstrap_digest='sha256:' + '9'*64, signing_key=key, enrolled_public_key=public,
        verify_live=live, reserve_start=reserve, hold_uncertain=hold, timeout_seconds=2)
    return client, server, peer, expected, key, controller


def request(client, nonce):
    client.send(json.dumps({'protocol': REQUEST, 'nonce': nonce}, sort_keys=True,
                           separators=(',', ':')).encode())


def test_actual_signed_enrollment_binds_nonce_digest_and_full_identity(tmp_path):
    events = []
    client, server, peer, expected, key, controller = setup(tmp_path,
        live=lambda _: events.append('live') or True,
        reserve=lambda payload, digest: events.append(('reserve', payload['identity']['fence'], len(digest))) or True)
    wall, tick = int(time.time()*1000), time.monotonic()
    try:
        request(client, expected['nonce'])
        assert controller.serve_once() == {'identity': expected, 'delivery': 'sent_unconfirmed'}
        client.settimeout(2)
        raw = client.recv(8192)
        args = dict(public_key=key.public_key(), nonce=expected['nonce'], measured_bootstrap_digest='sha256:'+'9'*64,
                    exchange_wall_ms=wall, exchange_monotonic=tick)
        assert verify_start_enrollment(raw, **args) == expected
        for change in ({'nonce':'f'*64}, {'measured_bootstrap_digest':'sha256:'+'8'*64},
                       {'public_key':Ed25519PrivateKey.generate().public_key()}):
            with pytest.raises(StartEnrollmentError): verify_start_enrollment(raw, **{**args, **change})
        tampered = json.loads(raw); tampered['payload']['identity']['fence'] += 1
        with pytest.raises(StartEnrollmentError):
            verify_start_enrollment(json.dumps(tampered, sort_keys=True, separators=(',', ':')).encode(), **args)
        with pytest.raises(StartEnrollmentError):
            verify_start_enrollment(raw, **{**args, 'wall_clock': lambda: (wall + 10000) / 1000})
        assert events == ['live', ('reserve', expected['fence'], 64), 'live']
        with pytest.raises(StartEnrollmentError): controller.serve_once()
    finally:
        client.close(); server.close(); peer.close()


@pytest.mark.parametrize('fault', ['bad_nonce', 'live_denied', 'reserve_uncertain', 'live_after_reserve'])
def test_failure_never_sends_and_uncertain_reservations_are_held(tmp_path, fault):
    calls = []
    def live(_):
        calls.append('live')
        return fault != 'live_denied' and not (fault == 'live_after_reserve' and calls.count('live') == 2)
    def reserve(*_):
        calls.append('reserve')
        if fault == 'reserve_uncertain': raise RuntimeError('private')
        return True
    client, server, peer, expected, _, controller = setup(tmp_path, live=live, reserve=reserve,
                                                           hold=lambda _: calls.append('hold'))
    try:
        request(client, 'BAD' if fault == 'bad_nonce' else expected['nonce'])
        with pytest.raises(StartEnrollmentError): controller.serve_once()
        client.settimeout(.05)
        with pytest.raises(TimeoutError): client.recv(8192)
        assert ('hold' in calls) == (fault in {'reserve_uncertain', 'live_after_reserve'})
    finally:
        client.close(); server.close(); peer.close()


def test_foreign_ancillary_is_denied(tmp_path):
    client, server, peer, expected, _, controller = setup(tmp_path)
    read_fd, write_fd = os.pipe()
    baseline = len(os.listdir('/proc/self/fd'))
    import array
    try:
        payload = json.dumps({'protocol':REQUEST,'nonce':expected['nonce']},sort_keys=True,separators=(',', ':')).encode()
        client.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [write_fd]))])
        with pytest.raises(StartEnrollmentError): controller.serve_once()
        os.fstat(read_fd)
        assert len(os.listdir('/proc/self/fd')) == baseline
    finally:
        os.close(read_fd); os.close(write_fd); client.close(); server.close(); peer.close()


def test_disconnected_peer_after_reservation_is_held(tmp_path):
    calls = []
    client, server, peer, expected, _, controller = setup(tmp_path,
        reserve=lambda *_: calls.append('reserve') or True,
        hold=lambda _: calls.append('hold'))
    try:
        request(client, expected['nonce']); client.close()
        with pytest.raises(StartEnrollmentError): controller.serve_once()
        assert calls == ['reserve', 'hold']
    finally:
        client.close(); server.close(); peer.close()
