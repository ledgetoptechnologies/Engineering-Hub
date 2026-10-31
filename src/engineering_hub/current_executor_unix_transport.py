"""Bounded current-executor observation over one explicitly pinned private UDS.

The private path and credential are separately enrolled. Same-UID or root
mutation is outside this transport's trust boundary; signed observations still
require independent release and launch verification.
"""
from __future__ import annotations

import os
import re
import socket
import threading
import time

from .current_executor_observation import MAX_OBSERVATION_BYTES, _strict
from .current_executor_transport import (CurrentExecutorTransportError, PATH, _DeadlineReader,
    _Response, _ResponseSocket, _remaining)
from .private_browser_socket import _path, _private_parent, _check_parent, _socket_entry


class CurrentExecutorUnixTransport:
    def __init__(self, path, *, credential):
        self._parent = None
        self._pid = os.getpid()
        self._lock = threading.Lock()
        try:
            if (type(credential) is not bytes or not 0 < len(credential) <= 4096
                    or any(not 33 <= byte <= 126 for byte in credential)):
                raise ValueError()
            self._path = _path(path)
            self._parent, self._parent_identity = _private_parent(self._path)
            self._entry = _socket_entry(self._path, self._parent)
            self.__credential = credential
            self._closed = False
        except Exception:
            if self._parent is not None:
                os.close(self._parent)
            raise CurrentExecutorTransportError('Invalid private executor transport') from None

    def __repr__(self):
        return 'CurrentExecutorUnixTransport(credential=<redacted>)'

    def observe_current_launch(self, *, nonce, timeout_ms):
        if os.getpid() != self._pid or not self._lock.acquire(blocking=False):
            raise CurrentExecutorTransportError('Current executor transport unavailable')
        connection = reader = response = None
        try:
            if (os.getpid() != self._pid or self._closed or type(nonce) is not str
                    or re.fullmatch('[0-9a-f]{64}', nonce) is None
                    or type(timeout_ms) is not int or not 1 <= timeout_ms <= 10000):
                raise ValueError()
            deadline = time.monotonic() + timeout_ms / 1000
            _check_parent(self._path, self._parent, self._parent_identity)
            _socket_entry(self._path, self._parent, self._entry)
            body = b'{"nonce":"' + nonce.encode('ascii') + b'"}'
            request = (b'POST ' + PATH.encode('ascii') + b' HTTP/1.1\r\nHost: localhost\r\n'
                b'Authorization: Bearer ' + self.__credential + b'\r\nAccept: application/json\r\n'
                b'Content-Type: application/json\r\nConnection: close\r\nContent-Length: '
                + str(len(body)).encode('ascii') + b'\r\n\r\n' + body)
            address = f'/proc/self/fd/{self._parent}/{self._path.name}'
            if len(os.fsencode(address)) >= 108: raise ValueError()
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(_remaining(deadline))
            connection.connect(address)
            _check_parent(self._path, self._parent, self._parent_identity)
            _socket_entry(self._path, self._parent, self._entry)
            connection.settimeout(_remaining(deadline))
            connection.sendall(request)
            reader = _DeadlineReader(connection, deadline)
            response = _Response(_ResponseSocket(reader), method='POST')
            response.begin(); reader.in_headers = False
            fields = response.getheaders()
            names = [name.lower() for name, _ in fields]
            if len(names) != len(set(names)): raise ValueError()
            headers = {name.lower(): value for name, value in fields}
            length = headers.get('content-length', '')
            if ('transfer-encoding' in headers or 'content-encoding' in headers
                    or headers.get('content-type', '').lower() != 'application/json'
                    or re.fullmatch('[1-9][0-9]{0,3}', length) is None
                    or int(length) > MAX_OBSERVATION_BYTES):
                raise ValueError()
            raw = reader.read(int(length))
            if len(raw) != int(length) or reader.read(1) != b'': raise ValueError()
            value = _strict(raw)
            if type(value) is not dict or set(value) != {'payload', 'signature'}: raise ValueError()
            _check_parent(self._path, self._parent, self._parent_identity)
            _socket_entry(self._path, self._parent, self._entry)
            _remaining(deadline)
            return raw
        except Exception:
            raise CurrentExecutorTransportError('Current executor transport unavailable') from None
        finally:
            try:
                if response is not None: response.close()
                elif reader is not None: reader.close()
            finally:
                try:
                    if connection is not None: connection.close()
                finally:
                    self._lock.release()

    def close(self):
        if os.getpid() != self._pid: return
        with self._lock:
            if not self._closed:
                self._closed = True
                os.close(self._parent)
                self._parent = None
                self.__credential = b''
