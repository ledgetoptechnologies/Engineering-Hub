"""Kernel sender pidfd checks; signed authority and host custody are separate."""
import json
import os
import signal
import socket
import time

import pytest

from hub_container_executor import gate_ledger_channel as channel
from tests.test_hub_gate_ledger_channel import context, canonical, state


def _reap_child(pid, *, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            return waited, status
        time.sleep(0.01)
    # An unreaped child retains its PID, so it cannot have been reused.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            return waited, status
        time.sleep(0.01)
    raise AssertionError('owned child did not exit after SIGKILL')


def test_unsupported_socket_option_fails_closed(context, monkeypatch):
    ledger, _, pins, _, public, launch = context
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    monkeypatch.setattr(channel, '_SO_PASSPIDFD', 2**30)
    try:
        with pytest.raises(channel.GateLedgerChannelError):
            channel.GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
                launch_verifier=launch, manifest_digest=pins['manifest_digest'],
                hold_uncertain=lambda _: None, timeout_seconds=2, require_sender_liveness=True)
        assert state(ledger) == []
    finally:
        client.close(); server.close()


@pytest.mark.parametrize('abort_hold', [False, True])
def test_sender_death_during_commit_holds(context, monkeypatch, abort_hold):
    if not hasattr(os, 'fork'): pytest.skip('Linux fork required')
    ledger, _, pins, key, public, launch = context
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    held = []
    received = []
    original_packet = channel._sender_packet
    def packet(sock):
        raw, fd = original_packet(sock)
        received.append(fd)
        return raw, fd
    monkeypatch.setattr(channel, '_sender_packet', packet)
    def hold(value):
        held.append(value)
        if abort_hold: raise KeyboardInterrupt()
    try:
        worker = channel.GateLedgerWorker(channel=server, ledger=ledger, supervisor_public_key=public,
            launch_verifier=launch, manifest_digest=pins['manifest_digest'],
            hold_uncertain=hold, timeout_seconds=4, require_sender_liveness=True)
    except channel.GateLedgerChannelError:
        client.close(); server.close(); pytest.skip('kernel lacks SO_PASSPIDFD')
    binding = list(launch.launch_binding())
    release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            server.close(); os.close(release_write)
            client.settimeout(3)
            challenge = json.loads(client.recv(8192))['challenge']
            now = int(time.time() * 1000)
            payload = dict(protocol=channel.REQUEST, challenge=challenge,
                launch_binding=binding, manifest_digest=pins['manifest_digest'],
                action='reserve', value=pins, issued_at_ms=now, expires_at_ms=now + 3000)
            client.send(canonical(dict(payload=payload,
                signature=key.sign(channel.DOMAIN + canonical(payload)).hex())))
            os.read(release_read, 1)
        finally:
            os._exit(0)
    client.close(); os.close(release_read)
    original = ledger.reserve_mount_gate_challenge
    reaped = []
    def reserve(value):
        os.write(release_write, b'x')
        reaped.append(_reap_child(pid))
        return original(value)
    monkeypatch.setattr(ledger, 'reserve_mount_gate_challenge', reserve)
    try:
        with pytest.raises(KeyboardInterrupt if abort_hold else channel.GateLedgerChannelError):
            worker.serve_once()
        assert reaped and state(ledger) == ['reserved'] and held == [pins]
        assert len(received) == 1
        with pytest.raises(OSError): os.fstat(received[0])
    finally:
        server.close()
        if not reaped:
            try: os.write(release_write, b'x')
            except BrokenPipeError: pass
            _reap_child(pid)
        os.close(release_write)
