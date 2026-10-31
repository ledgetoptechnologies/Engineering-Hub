"""Explicit per-packet authenticated pilot command transport; not an installed broker.

An independently enrolled caller owns listener, adapter and lifetime. This module
never binds a socket, discovers identities or enables a launcher. It does not
establish state/daemon isolation. Every new connection carries one bounded packet
and one reply, with kernel SCM_CREDENTIALS checked on both packets. Effects have
no automatic retry. Timeouts do not cancel an admitted adapter operation.
"""
from __future__ import annotations

import array
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import sys
import threading
import time
from uuid import UUID

from engineering_hub.container_pilot_client import ContainerPilotRequest, ContainerPilotResponse
from .adapter import HostAdapter

_MAX_PACKET = 32768
_CREDS = struct.Struct('=iII')
_PROTOCOL = 'engineering-hub.pilot-command.seqpacket.v1'


class BrokerTransportError(RuntimeError):
    def __init__(self):
        super().__init__('pilot broker transport unavailable; inspect original command')


def _require(value):
    if not value:
        raise BrokerTransportError()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result
    def reject(_):
        raise BrokerTransportError()
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject, parse_float=reject)
    _require(type(value) is dict and _canonical(value) == raw)
    return value


def _start(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    end = raw.rfind(')')
    _require(end >= 0)
    fields = raw[end + 2:].split()
    _require(len(fields) >= 20 and fields[0] not in {'Z', 'X'})
    value = int(fields[19])
    _require(value > 0)
    return value


class PeerPin:
    """Pinned peer process; host identity/ownership is independently enrolled."""
    def __init__(self, *, pid, uid, gid, start_ticks):
        self.fd = None
        self.owner = os.getpid()
        try:
            _require(sys.platform == 'linux' and hasattr(os, 'pidfd_open')
                     and hasattr(socket, 'SO_PASSCRED') and hasattr(socket, 'SCM_CREDENTIALS'))
            _require(all(type(v) is int for v in (pid, uid, gid, start_ticks))
                     and 0 < pid < 2**31 and 0 <= uid < 2**32 - 1
                     and 0 <= gid < 2**32 - 1 and start_ticks > 0)
            self.pid, self.uid, self.gid, self.start = pid, uid, gid, start_ticks
            _require(_start(pid) == start_ticks)
            self.fd = os.pidfd_open(pid, 0)
            self.check()
        except BaseException:
            self.close()
            raise BrokerTransportError() from None

    def check(self):
        _require(os.getpid() == self.owner and self.fd is not None
                 and not select.select([self.fd], [], [], 0)[0]
                 and _start(self.pid) == self.start)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _receive(connection, peer):
    # Reserve ancillary capacity for credentials plus bounded malicious FDs;
    # close any received rights before rejecting, including on truncation.
    raw, controls, flags, _ = connection.recvmsg(_MAX_PACKET + 1,
        socket.CMSG_SPACE(_CREDS.size) + socket.CMSG_SPACE(64 * array.array('i').itemsize))
    credentials, invalid = [], False
    for level, kind, data in controls:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds = array.array('i')
            fds.frombytes(data[:len(data) - len(data) % fds.itemsize])
            invalid = True
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    # Attempt every installed descriptor even when one close fails.
                    pass
        elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) == _CREDS.size:
            credentials.append(_CREDS.unpack(data))
        else:
            invalid = True
    _require(not invalid and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
             and 0 < len(raw) <= _MAX_PACKET and credentials == [(peer.pid, peer.uid, peer.gid)])
    peer.check()
    return _parse(raw)


def _set_deadline(connection, deadline):
    remaining = deadline - time.monotonic()
    _require(remaining > 0)
    connection.settimeout(remaining)


def _send(connection, value):
    raw = _canonical(value)
    _require(0 < len(raw) <= _MAX_PACKET and connection.send(raw) == len(raw))


def _identity(path):
    info = os.lstat(path)
    _require(stat.S_ISSOCK(info.st_mode))
    return info.st_dev, info.st_ino


def _uuid(value):
    _require(type(value) is str and str(UUID(value)) == value)
    return value


class PilotCommandBroker:
    """Single-threaded one-packet dispatch; adapter/listener owned by caller."""
    def __init__(self, listener, adapter, peer):
        self._pid, self._closed, self._busy = os.getpid(), False, threading.Lock()
        _require(type(listener) is socket.socket and listener.family == socket.AF_UNIX
                 and listener.type & socket.SOCK_SEQPACKET == socket.SOCK_SEQPACKET
                 and listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
                 and type(adapter) is HostAdapter and type(peer) is PeerPin)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.listener, self.adapter, self.peer = listener, adapter, peer

    def serve_once(self, timeout_seconds):
        acquired = False
        try:
            _require(os.getpid() == self._pid and not self._closed
                     and type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 125)
            acquired = self._busy.acquire(blocking=False)
            _require(acquired and not self._closed)
            deadline = time.monotonic() + timeout_seconds
            _set_deadline(self.listener, deadline)
            connection, _ = self.listener.accept()
            with connection:
                _set_deadline(connection, deadline)
                request = _receive(connection, self.peer)
                _require(set(request) == {'protocol', 'action', 'payload'} and request['protocol'] == _PROTOCOL)
                action, payload = request['action'], request['payload']
                if action == 'submit':
                    argument = ContainerPilotRequest.model_validate(payload)
                elif action in {'get', 'reconcile'}:
                    argument = _uuid(payload)
                else:
                    raise BrokerTransportError()
                # Parsing, peer and scheduling checks consume the same deadline.
                self.peer.check()
                _require(time.monotonic() < deadline and not self._closed)
                if action == 'submit':
                    result = self.adapter.submit(argument)
                elif action == 'get':
                    result = self.adapter.get(argument)
                else:
                    result = self.adapter.reconcile(argument)
                _require(result is None or type(result) is ContainerPilotResponse)
                self.peer.check()
                _require(time.monotonic() < deadline)
                _set_deadline(connection, deadline)
                _send(connection, {'protocol': _PROTOCOL, 'result': None if result is None else result.model_dump(mode='json')})
        except Exception:
            raise BrokerTransportError() from None
        finally:
            if acquired:
                self._busy.release()

    def close(self):
        """Close admission only when idle; caller owns and closes listener/peer/adapter.

        An admitted or blocked call must be drained by its caller; close never
        cancels it or implies that an adapter effect has stopped.
        """
        _require(os.getpid() == self._pid)
        acquired = self._busy.acquire(blocking=False)
        _require(acquired)
        try:
            self._closed = True
        finally:
            self._busy.release()


class PilotCommandClient:
    """One connection per call; a transport failure is an unknown outcome."""
    def __init__(self, socket_path, peer):
        _require(type(socket_path) is str and socket_path.startswith('/') and not socket_path.startswith('//')
                 and os.path.normpath(socket_path) == socket_path and len(os.fsencode(socket_path)) < 108
                 and type(peer) is PeerPin)
        self.path, self.peer, self._pid, self._closed = socket_path, peer, os.getpid(), False
        self._busy = threading.Lock()

    def _call(self, action, command, timeout_seconds):
        acquired = False
        try:
            _require(os.getpid() == self._pid and not self._closed and type(command) is ContainerPilotRequest
                     and type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 125)
            acquired = self._busy.acquire(blocking=False)
            _require(acquired and not self._closed)
            deadline = time.monotonic() + timeout_seconds
            identity = _identity(self.path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
                _set_deadline(connection, deadline)
                connection.connect(self.path)
                self.peer.check()
                _require(_CREDS.unpack(connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                             _CREDS.size)) ==
                         (self.peer.pid, self.peer.uid, self.peer.gid)
                         and _identity(self.path) == identity)
                payload = command.model_dump(mode='json') if action == 'submit' else command.command_id
                _set_deadline(connection, deadline)
                _send(connection, {'protocol': _PROTOCOL, 'action': action, 'payload': payload})
                _set_deadline(connection, deadline)
                reply = _receive(connection, self.peer)
                _require(time.monotonic() < deadline and _identity(self.path) == identity
                         and set(reply) == {'protocol', 'result'} and reply['protocol'] == _PROTOCOL)
                if reply['result'] is None:
                    _require(action != 'submit')
                    return None
                result = ContainerPilotResponse.model_validate(reply['result'])
                _require(all(getattr(result, name) == getattr(command, name)
                             for name in ContainerPilotRequest.model_fields))
                return result
        except Exception:
            raise BrokerTransportError() from None
        finally:
            if acquired:
                self._busy.release()

    def submit(self, command, timeout_seconds=125):
        return self._call('submit', command, timeout_seconds)

    def get(self, command, timeout_seconds=125):
        return self._call('get', command, timeout_seconds)

    def reconcile(self, command, timeout_seconds=125):
        return self._call('reconcile', command, timeout_seconds)

    def close(self):
        """Close admission when idle; a busy call must finish before close."""
        _require(os.getpid() == self._pid)
        acquired = self._busy.acquire(blocking=False)
        _require(acquired)
        try:
            self._closed = True
        finally:
            self._busy.release()
