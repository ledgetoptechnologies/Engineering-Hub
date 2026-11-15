"""Synthetic requester transport framing, no installed supervisor."""
import os
import struct
from uuid import uuid4

import pytest

pytest.importorskip('fcntl')
from hub_container_executor import proposal_request as request


@pytest.mark.parametrize('parts, expected', [([b'O', b'K', b''], 0), ([b'O', b''], 2)])
def test_fragmented_ack_and_ambiguous_output(tmp_path, monkeypatch, capsys, parts, expected):
    path = tmp_path / 'socket'
    path.touch()
    nonce = 'a' * 64
    monkeypatch.setattr(request.os, 'getuid', lambda: 0)
    monkeypatch.setattr(request.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(request.secrets, 'token_hex', lambda n: nonce)
    class Peer:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): assert 0 < value <= 10
        def connect(self, value): assert value == str(path)
        def getsockopt(self, *args): return struct.pack('=iII', 100, 0, 0)
        def sendall(self, data): assert data
        def shutdown(self, how): pass
        def recv(self, n): return parts.pop(0)
    monkeypatch.setattr(request.socket, 'socket', lambda *args: Peer())
    result = request.main(['--socket', str(path), '--launch-id', str(uuid4())])
    output = capsys.readouterr()
    proposal = str(path.parent / ('proposal-' + nonce + '.json'))
    assert result == expected
    if expected == 0:
        assert output.out.strip() == proposal and not output.err
    else:
        assert output.out == '' and 'possible proposal: ' + proposal in output.err
