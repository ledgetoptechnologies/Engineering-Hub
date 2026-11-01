"""Real local socket transport with synthetic HTTP and no Engine or host state."""
import os
import socket
import sys
import threading

import pytest
pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux private UDS required')
from engineering_hub.current_executor_unix_transport import CurrentExecutorUnixTransport
from engineering_hub.current_executor_transport import CurrentExecutorTransportError


def test_private_unix_observation_and_socket_replacement_denial(tmp_path):
    os.chmod(tmp_path, 0o700)
    path = tmp_path / 'executor.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path)); os.chmod(path, 0o600); listener.listen(1)
    raw = b'{"payload":{},"signature":"fixture"}'
    errors = []
    def serve():
        try:
            peer, _ = listener.accept()
            with peer:
                request = b''
                while b'\r\n\r\n' not in request: request += peer.recv(4096)
                assert b'Authorization: Bearer synthetic-test-token\r\n' in request
                peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                    + str(len(raw)).encode() + b'\r\nConnection: close\r\n\r\n' + raw)
        except BaseException as error: errors.append(error)
    transport = CurrentExecutorUnixTransport(str(path), credential=b'synthetic-test-token')
    thread = threading.Thread(target=serve)
    try:
        thread.start()
        assert transport.observe_current_launch(nonce='a' * 64, timeout_ms=1000) == raw
        thread.join(2)
        assert not thread.is_alive() and not errors
        listener.close(); path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            replacement.bind(str(path)); os.chmod(path, 0o600); replacement.listen(1)
            with pytest.raises(CurrentExecutorTransportError):
                transport.observe_current_launch(nonce='b' * 64, timeout_ms=1000)
        finally:
            replacement.close()
    finally:
        transport.close(); listener.close()
