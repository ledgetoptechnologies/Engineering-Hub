"""Real seqpacket and SQLite ledger; host/peer custody are synthetic here."""
import hashlib
import json
import os
import socket
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import hub_container_executor.ledger as ledger_module
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.mount_gate_consume_protocol import (
    GateConsumeError, MountGateConsumeController, REQUEST, verify_consume_ack)
from hub_container_executor.mount_gate_protocol import issue_decision
from tests.test_hub_container_executor_ledger import gate_fixture, open_ledger


def setup(tmp_path, monkeypatch, *, hold=lambda _: None, live=lambda _: True):
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    ledger = open_ledger(tmp_path)
    _, decision, pins = gate_fixture(ledger, require_gate=True)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    pins.update(host_pid=peer.pid, process_start_ticks=peer.start)
    decision.update(host_pid=peer.pid, process_start_ticks=peer.start)
    now = int(time.time() * 1000)
    decision.update(issued_at_ms=now, expires_at_ms=now+4000)
    ledger.reserve_mount_gate_challenge(pins)
    key = Ed25519PrivateKey.generate(); wire = issue_decision(decision, key)
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    tick = time.monotonic()
    controller = MountGateConsumeController(channel=server, peer=peer, expected=pins, decision_wire=wire,
        signing_key=key, enrolled_public_key=key.public_key().public_bytes(serialization.Encoding.Raw,
            serialization.PublicFormat.Raw), consume_challenge=ledger.consume_mount_gate_challenge,
        verify_live=live, hold_uncertain=hold, exchange_wall_ms=now, exchange_monotonic=tick, timeout_seconds=3)
    return ledger, client, server, peer, pins, key, wire, now, tick, controller


def send(client, pins, wire, **changes):
    value = dict(protocol=REQUEST, nonce=pins['nonce'], launch_id=pins['launch_id'],
        operation_id=pins['operation_id'], fence=pins['fence'], decision_sha256=hashlib.sha256(wire).hexdigest())
    value.update(changes)
    client.send(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def test_real_ledger_consumes_before_signed_ack_and_refuses_replay(tmp_path, monkeypatch):
    ledger, client, server, peer, pins, key, wire, wall, tick, controller = setup(tmp_path, monkeypatch)
    try:
        send(client, pins, wire)
        assert controller.serve_once() == 'sent_unconfirmed'
        client.settimeout(2); ack = client.recv(8192)
        assert verify_consume_ack(ack, public_key=key.public_key(), expected=pins, decision_wire=wire,
                                  exchange_wall_ms=wall, exchange_monotonic=tick) is True
        with ledger._connection() as db:
            assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == 'consumed'
        with pytest.raises(GateConsumeError): controller.serve_once()
        with pytest.raises(GateConsumeError):
            verify_consume_ack(ack, public_key=Ed25519PrivateKey.generate().public_key(), expected=pins,
                               decision_wire=wire, exchange_wall_ms=wall, exchange_monotonic=tick)
        with pytest.raises(GateConsumeError):
            verify_consume_ack(ack, public_key=key.public_key(), expected=dict(pins, fence=2),
                               decision_wire=wire, exchange_wall_ms=wall, exchange_monotonic=tick)
    finally:
        client.close(); server.close(); peer.close(); ledger.close()


@pytest.mark.parametrize('changes', [{'nonce': 'b' * 64}, {'fence': True}])
def test_wrong_request_does_not_consume(tmp_path, monkeypatch, changes):
    ledger, client, server, peer, pins, _, wire, _, _, controller = setup(tmp_path, monkeypatch)
    try:
        send(client, pins, wire, **changes)
        with pytest.raises(GateConsumeError): controller.serve_once()
        with ledger._connection() as db:
            assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == 'reserved'
    finally:
        client.close(); server.close(); peer.close(); ledger.close()


def test_lost_ack_after_consumption_holds(tmp_path, monkeypatch):
    held = []
    ledger, client, server, peer, pins, _, wire, _, _, controller = setup(tmp_path, monkeypatch,
                                                                        hold=lambda _: held.append(True))
    try:
        send(client, pins, wire); client.close()
        with pytest.raises(GateConsumeError): controller.serve_once()
        with ledger._connection() as db:
            assert db.execute('SELECT state FROM mount_gate_challenges').fetchone()[0] == 'consumed'
        assert held == [True]
    finally:
        client.close(); server.close(); peer.close(); ledger.close()
