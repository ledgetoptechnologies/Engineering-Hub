"""Local component tests; no supervisor, host isolation or deployment proof."""
import array
import json
import os
import socket
import threading
import time
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub_container_executor import application_mount_gate as gate
from hub_container_executor import mount_gate_protocol as protocol


TOKENS = {'CODEX_TOKEN': 'test-codex', 'HERMES_TOKEN': 'test-hermes', 'CLAUDE_TOKEN': 'test-claude'}


def pins(path):
    info = path.stat()
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: flags = gate._flags(fd)
    finally: os.close(fd)
    return dict(nonce='a' * 64, launch_id=str(uuid4()), operation_id=str(uuid4()),
        run_id=str(uuid4()), fence=1, manifest_digest='sha256:'+'b'*64,
        image_config_id='sha256:'+'c'*64, generation_id='candidate-1',
        expected_device=info.st_dev, expected_inode=info.st_ino, boot_id=str(uuid4()),
        container_id='d'*64, host_pid=42, process_start_ticks=1234,
        mount_target='/var/lib/hub', mount_flags=flags, runnable_digest='sha256:'+'e'*64,
        platform_manifest_digest='sha256:'+'f'*64, implementation_commit='a'*40)


def test_matching_decision_consumes_before_fixed_exec(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, '_TARGET', str(tmp_path))
    expected, key, events = pins(tmp_path), Ed25519PrivateKey.generate(), []
    supplied_tokens = dict(TOKENS)
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    class Executed(BaseException): pass
    def execve(path, argv, environment):
        assert 'LD_PRELOAD' not in environment and 'LD_LIBRARY_PATH' not in environment
        assert environment['CODEX_TOKEN'] == TOKENS['CODEX_TOKEN']
        events.append(('exec', path, argv)); raise Executed()
    monkeypatch.setenv('LD_PRELOAD', '/untrusted.so')
    monkeypatch.setenv('LD_LIBRARY_PATH', '/untrusted')
    monkeypatch.setattr(gate.os, 'execve', execve)
    def issuer():
        observation = json.loads(server.recv(8192))
        assert observation['identity'] == expected
        now = int(time.time() * 1000)
        payload = dict(expected, protocol=protocol.PROTOCOL, audience=protocol.AUDIENCE,
            observed_device=expected['expected_device'], observed_inode=expected['expected_inode'],
            decision='allow', issued_at_ms=now, expires_at_ms=now+4000)
        server.send(protocol.issue_decision(payload, key))
    thread = threading.Thread(target=issuer); thread.start()
    def consume(_):
        supplied_tokens['LD_PRELOAD'] = '/late.so'
        supplied_tokens['CODEX_TOKEN'] = 'changed-after-snapshot'
        events.append(('consume',))
        return True
    try:
        with pytest.raises(Executed):
            gate.run_gate(channel=client, public_key=key.public_key(), expected=expected,
                consume_challenge=consume, application_tokens=supplied_tokens, timeout_seconds=5)
        assert events == [('consume',), ('exec', gate._COMMAND[0], gate._COMMAND)]
    finally:
        thread.join(5); client.close(); server.close()
    assert not thread.is_alive()


def test_replaced_mount_is_denied_before_consumption(tmp_path, monkeypatch):
    target = tmp_path / 'state'; target.mkdir()
    monkeypatch.setattr(gate, '_TARGET', str(target))
    expected, key, events = pins(target), Ed25519PrivateKey.generate(), []
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    def issuer():
        server.recv(8192)
        target.rename(tmp_path / 'old'); target.mkdir()
        now = int(time.time() * 1000)
        payload = dict(expected, protocol=protocol.PROTOCOL, audience=protocol.AUDIENCE,
            observed_device=expected['expected_device'], observed_inode=expected['expected_inode'],
            decision='allow', issued_at_ms=now, expires_at_ms=now+4000)
        server.send(protocol.issue_decision(payload, key))
    thread = threading.Thread(target=issuer); thread.start()
    try:
        with pytest.raises(gate.ApplicationMountGateError):
            gate.run_gate(channel=client, public_key=key.public_key(), expected=expected,
                consume_challenge=lambda _: events.append('consume') or True, application_tokens=TOKENS, timeout_seconds=5)
        assert events == []
    finally:
        thread.join(5); client.close(); server.close()
    assert not thread.is_alive()


@pytest.mark.parametrize('failure', ['wrong_key', 'consumer_denied', 'disconnect'])
def test_failed_decision_never_executes(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(gate, '_TARGET', str(tmp_path))
    expected, key, calls = pins(tmp_path), Ed25519PrivateKey.generate(), []
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    monkeypatch.setattr(gate.os, 'execve', lambda *_: calls.append('exec'))
    def issuer():
        server.recv(8192)
        if failure == 'disconnect':
            server.close(); return
        now = int(time.time() * 1000)
        payload = dict(expected, protocol=protocol.PROTOCOL, audience=protocol.AUDIENCE,
            observed_device=expected['expected_device'], observed_inode=expected['expected_inode'],
            decision='allow', issued_at_ms=now, expires_at_ms=now+4000)
        server.send(protocol.issue_decision(payload, key))
    thread = threading.Thread(target=issuer); thread.start()
    try:
        with pytest.raises(gate.ApplicationMountGateError):
            gate.run_gate(channel=client,
                public_key=(Ed25519PrivateKey.generate().public_key() if failure == 'wrong_key' else key.public_key()),
                expected=expected, consume_challenge=lambda _: calls.append('consume') or False, application_tokens=TOKENS,
                timeout_seconds=5)
        assert 'exec' not in calls
        assert ('consume' in calls) is (failure == 'consumer_denied')
    finally:
        thread.join(5); client.close(); server.close()
    assert not thread.is_alive()


def test_wrong_local_inode_denied_before_any_exchange(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, '_TARGET', str(tmp_path))
    expected = pins(tmp_path); expected['expected_inode'] += 1
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(gate.ApplicationMountGateError):
            gate.run_gate(channel=client, public_key=Ed25519PrivateKey.generate().public_key(),
                expected=expected, consume_challenge=lambda _: True, application_tokens=TOKENS, timeout_seconds=1)
        server.setblocking(False)
        with pytest.raises(BlockingIOError): server.recv(1)
    finally:
        client.close(); server.close()


def test_mount_replacement_during_decision_verification_denies_consume(tmp_path, monkeypatch):
    target = tmp_path / 'state'; target.mkdir()
    monkeypatch.setattr(gate, '_TARGET', str(target))
    expected, key, calls = pins(target), Ed25519PrivateKey.generate(), []
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    original = gate.verify_decision
    def replace_before_verify(*args, **kwargs):
        target.rename(tmp_path / 'old'); target.mkdir()
        return original(*args, **kwargs)
    monkeypatch.setattr(gate, 'verify_decision', replace_before_verify)
    def issuer():
        server.recv(8192)
        now = int(time.time() * 1000)
        payload = dict(expected, protocol=protocol.PROTOCOL, audience=protocol.AUDIENCE,
            observed_device=expected['expected_device'], observed_inode=expected['expected_inode'],
            decision='allow', issued_at_ms=now, expires_at_ms=now+4000)
        server.send(protocol.issue_decision(payload, key))
    thread = threading.Thread(target=issuer); thread.start()
    try:
        with pytest.raises(gate.ApplicationMountGateError):
            gate.run_gate(channel=client, public_key=key.public_key(), expected=expected,
                consume_challenge=lambda _: calls.append('consume') or True, application_tokens=TOKENS, timeout_seconds=5)
        assert calls == []
    finally:
        thread.join(5); client.close(); server.close()
    assert not thread.is_alive()


def test_mount_replacement_after_consumption_denies_exec(tmp_path, monkeypatch):
    target = tmp_path / 'state'; target.mkdir()
    monkeypatch.setattr(gate, '_TARGET', str(target))
    expected, key, calls = pins(target), Ed25519PrivateKey.generate(), []
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    monkeypatch.setattr(gate.os, 'execve', lambda *_: calls.append('exec'))
    def issuer():
        server.recv(8192)
        now = int(time.time() * 1000)
        payload = dict(expected, protocol=protocol.PROTOCOL, audience=protocol.AUDIENCE,
            observed_device=expected['expected_device'], observed_inode=expected['expected_inode'],
            decision='allow', issued_at_ms=now, expires_at_ms=now+4000)
        server.send(protocol.issue_decision(payload, key))
    def consume(_):
        calls.append('consume'); target.rename(tmp_path / 'old'); target.mkdir(); return True
    thread = threading.Thread(target=issuer); thread.start()
    try:
        with pytest.raises(gate.ApplicationMountGateError):
            gate.run_gate(channel=client, public_key=key.public_key(), expected=expected,
                consume_challenge=consume, application_tokens=TOKENS, timeout_seconds=5)
        assert calls == ['consume']
    finally:
        thread.join(5); client.close(); server.close()
    assert not thread.is_alive()


def test_received_rights_are_closed_and_channel_is_cloexec(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, '_TARGET', str(tmp_path))
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    read_fd, write_fd = os.pipe()
    client.set_inheritable(True)
    baseline = len(os.listdir('/proc/self/fd'))
    def issuer():
        server.recv(8192)
        server.sendmsg([b'bad'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [write_fd]))])
    thread = threading.Thread(target=issuer); thread.start()
    try:
        with pytest.raises(gate.ApplicationMountGateError):
            gate.run_gate(channel=client, public_key=Ed25519PrivateKey.generate().public_key(),
                expected=pins(tmp_path), consume_challenge=lambda _: True,
                application_tokens=TOKENS, timeout_seconds=5)
        thread.join(5)
        assert not thread.is_alive() and not client.get_inheritable()
        assert len(os.listdir('/proc/self/fd')) == baseline
    finally:
        thread.join(5); client.close(); server.close(); os.close(read_fd); os.close(write_fd)

