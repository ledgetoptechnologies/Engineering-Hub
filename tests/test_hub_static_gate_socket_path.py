"""Opt-in isolated test of v3 fixed-path receiver; synthetic issuer."""
import json
import os
import signal
import socket
import time

import pytest


@pytest.mark.skipif(os.environ.get('HUB_GATE_SOCKET_PATH_TEST') != '1',
                    reason='requires explicitly isolated container and freshly built v3 gate')
def test_v3_gate_connects_fixed_path_and_ignores_fd3():
    binary = os.environ['HUB_STATIC_GATE_TEST_BINARY']
    path = '/run/hub-gate/controller.sock'
    assert not os.path.lexists(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    decoy, inherited = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    child = None; created_identity = None
    try:
        listener.bind(path); created = os.lstat(path); created_identity = (created.st_dev, created.st_ino)
        listener.listen(1); listener.settimeout(5)
        env = {'CODEX_TOKEN': 'synthetic', 'HERMES_TOKEN': 'synthetic', 'CLAUDE_TOKEN': 'synthetic'}
        child = os.posix_spawn(binary, [binary], env,
            file_actions=[(os.POSIX_SPAWN_DUP2, inherited.fileno(), 3)])
        inherited.close()
        channel, _ = listener.accept()
        with channel:
            channel.settimeout(5)
            request = json.loads(channel.recv(8192))
            assert set(request) == {'protocol', 'nonce'}
            assert request['protocol'] == 'engineering-hub.application-gate-start-request.v1'
            assert len(request['nonce']) == 64
            channel.send(b'not a signed response')
        decoy.settimeout(5)
        assert decoy.recv(1) == b''  # Runtime-supplied FD 3 was closed, not used.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            waited, status = os.waitpid(child, os.WNOHANG)
            if waited == child:
                child = None
                assert os.waitstatus_to_exitcode(status) != 0
                break
            time.sleep(.01)
        else: pytest.fail('owned gate did not exit')
    finally:
        if child is not None:
            try: os.kill(child, signal.SIGKILL)
            except ProcessLookupError: pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waited, _ = os.waitpid(child, os.WNOHANG)
                if waited == child: break
                time.sleep(.01)
            else: pytest.fail('owned gate could not be reaped')
        listener.close(); decoy.close(); inherited.close()
        try:
            current = os.lstat(path)
            if created_identity == (current.st_dev, current.st_ino): os.unlink(path)
        except FileNotFoundError:
            pass


@pytest.mark.skipif(os.environ.get('HUB_GATE_SOCKET_PATH_TEST') != '1',
                    reason='requires explicitly isolated container and freshly built v2 gate')
def test_legacy_gate_still_uses_fd3():
    binary = os.environ['HUB_STATIC_GATE_V2_TEST_BINARY']
    receiver, inherited = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    child = None
    try:
        env = {'CODEX_TOKEN': 'synthetic', 'HERMES_TOKEN': 'synthetic', 'CLAUDE_TOKEN': 'synthetic'}
        child = os.posix_spawn(binary, [binary], env,
            file_actions=[(os.POSIX_SPAWN_DUP2, inherited.fileno(), 3)])
        inherited.close(); receiver.settimeout(5)
        request = json.loads(receiver.recv(8192))
        assert set(request) == {'protocol', 'nonce'}
        assert request['protocol'] == 'engineering-hub.application-gate-start-request.v1'
        receiver.send(b'not a signed response')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            waited, status = os.waitpid(child, os.WNOHANG)
            if waited == child:
                child = None
                assert os.waitstatus_to_exitcode(status) != 0
                break
            time.sleep(.01)
        else: pytest.fail('owned v2 gate did not exit')
    finally:
        if child is not None:
            try: os.kill(child, signal.SIGKILL)
            except ProcessLookupError: pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waited, _ = os.waitpid(child, os.WNOHANG)
                if waited == child: break
                time.sleep(.01)
            else: pytest.fail('owned v2 gate could not be reaped')
        receiver.close(); inherited.close()
