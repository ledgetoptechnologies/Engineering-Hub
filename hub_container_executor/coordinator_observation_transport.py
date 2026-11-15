"""Bounded authenticated HTTP over the existing private executor UDS relay."""
import json
import os
import re
import socket
import time

from engineering_hub.container_pilot_client import _validate_credential_and_timeout
from engineering_hub.current_executor_observation import MAX_OBSERVATION_BYTES, _strict
from engineering_hub.current_executor_transport import (
    PATH, _DeadlineReader, _Response, _ResponseSocket, _remaining, _require, _NONCE,
    CurrentExecutorTransportError,
)
from engineering_hub.private_browser_socket import _path, _private_parent, _socket_entry, _check_parent


class CoordinatorObservationTransport:
    """No retries, proxies or discovery; signed response is verified separately."""
    def __init__(self, *, socket_path, credential):
        _validate_credential_and_timeout(credential, 10.0, 125.0)
        self._path = _path(socket_path)
        self._credential = credential.encode('ascii')
        self._pid = os.getpid()
        self._closed = False

    def observe_current_launch(self, *, nonce, timeout_ms):
        directory = sock = reader = response = None
        try:
            _require(not self._closed and self._pid == os.getpid() and type(nonce) is str
                     and _NONCE.fullmatch(nonce) and type(timeout_ms) is int and 1 <= timeout_ms <= 10000)
            deadline = time.monotonic() + timeout_ms / 1000
            directory, parent = _private_parent(self._path)
            entry = _socket_entry(self._path, directory)
            _check_parent(self._path, directory, parent)
            body = json.dumps({'nonce': nonce}, separators=(',', ':')).encode('ascii')
            request = (b'POST ' + PATH.encode('ascii') + b' HTTP/1.1\r\nHost: 127.0.0.1\r\n'
                       b'Authorization: Bearer ' + self._credential + b'\r\nAccept: application/json\r\n'
                       b'Content-Type: application/json\r\nConnection: close\r\nContent-Length: '
                       + str(len(body)).encode('ascii') + b'\r\n\r\n' + body)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(_remaining(deadline)); sock.connect(str(self._path))
            _check_parent(self._path, directory, parent); _socket_entry(self._path, directory, entry)
            sock.settimeout(_remaining(deadline)); sock.sendall(request)
            reader = _DeadlineReader(sock, deadline)
            response = _Response(_ResponseSocket(reader), method='POST'); response.begin(); reader.in_headers = False
            fields = response.getheaders(); lowered = [name.lower() for name, _ in fields]
            _require(len(lowered) == len(set(lowered)))
            headers = {name.lower(): value for name, value in fields}
            _require('transfer-encoding' not in headers and 'content-encoding' not in headers
                     and headers.get('content-type', '').lower() == 'application/json')
            length = headers.get('content-length', '')
            _require(re.fullmatch(r'[1-9][0-9]{0,3}', length) is not None and int(length) <= MAX_OBSERVATION_BYTES)
            result = reader.read(int(length)); _require(len(result) == int(length) and reader.read(1) == b'')
            value = _strict(result); _require(type(value) is dict and set(value) == {'payload', 'signature'})
            _check_parent(self._path, directory, parent); _socket_entry(self._path, directory, entry)
            _remaining(deadline)
            return result
        except Exception:
            raise CurrentExecutorTransportError('Current executor transport unavailable') from None
        finally:
            if response is not None: response.close()
            elif reader is not None: reader.close()
            if sock is not None: sock.close()
            if directory is not None: os.close(directory)

    def close(self):
        self._closed = True
