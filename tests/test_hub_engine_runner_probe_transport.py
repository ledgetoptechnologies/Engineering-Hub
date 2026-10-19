"""Real Unix HTTP transport to a SYNTHETIC server, not Docker acceptance."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading

import pytest

from scripts.hub_engine_runner_probe import main


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Unix socket transport required')


@pytest.mark.parametrize('identity,architecture,expected', [
    ('pinned-daemon', 'x86_64', 0),
    ('wrong-daemon', 'x86_64', 1),
    ('pinned-daemon', 'aarch64', 1),
])
def test_concrete_backend_only_requests_pinned_info(identity, architecture, expected, capsys):
    # Keep the Unix socket path below the kernel's limit regardless of pytest's
    # generated directory name. All data and the listener are test-owned.
    with tempfile.TemporaryDirectory(prefix='eh-probe-') as temporary:
        root = Path(temporary)
        path = root / 'engine.sock'
        requests = []
        failures = []
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    data = b''
                    while b'\r\n\r\n' not in data:
                        chunk = connection.recv(4096)
                        if not chunk or len(data) + len(chunk) > 16384:
                            raise AssertionError('Invalid synthetic test request')
                        data += chunk
                    requests.append(data)
                    body = json.dumps({'ID': identity, 'OSType': 'linux',
                                       'Architecture': architecture}).encode()
                    connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                                       + b'Content-Length: ' + str(len(body)).encode()
                                       + b'\r\nConnection: close\r\n\r\n' + body)
            except BaseException as error:
                failures.append(type(error).__name__)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            assert main(['--runner-id', 'local-probe', '--daemon-id', 'pinned-daemon',
                         '--architecture', 'amd64', '--socket', str(path),
                         '--api-version', '1.47', '--generation-root', str(root / 'never-created')]) == expected
        finally:
            thread.join(6)
            listener.close()
        assert not thread.is_alive() and not failures
        assert len(requests) == 1
        assert requests[0].split(b'\r\n', 1)[0] == b'GET /v1.47/info HTTP/1.1'
        assert b'Authorization:' not in requests[0]
        assert not (root / 'never-created').exists()
        output = json.loads(capsys.readouterr().out)
        assert output['ok'] is (expected == 0)
        assert output['runner_only'] is True
        assert 'wrong-daemon' not in json.dumps(output)
