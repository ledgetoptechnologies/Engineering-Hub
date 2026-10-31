"""Explicit per-packet authenticated pilot command transport; not an installed broker.

An independently enrolled caller owns listener, adapter and lifetime. This module
never binds a socket, discovers identities or enables a launcher. It does not
establish state/daemon isolation. Every new connection carries one bounded packet
and one reply, with kernel SCM_CREDENTIALS checked on both packets. Effects have
no automatic retry. Timeouts do not cancel an admitted adapter operation.

The inherited mode instead uses one already connected descriptor handed to each
side at launch. A descriptor can be delegated, so it identifies no process:
every packet is still checked against the expected kernel sender account, and
any fault, timeout or out-of-sequence packet closes the channel for good.
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
_CHANNEL_PROTOCOL = 'engineering-hub.pilot-command.inherited-seqpacket.v1'


class BrokerTransportError(RuntimeError):
    def __init__(self):
        super().__init__('pilot broker transport unavailable; inspect original command')


class BrokerAcceptTimeout(BrokerTransportError):
    """No connection was accepted; no operation could have been admitted."""


class BrokerReadTimeout(BrokerTransportError):
    """No complete request packet was admitted."""


class BrokerAdmissionClosed(BrokerTransportError):
    """A validated request was denied before adapter dispatch."""


class BrokerChannelClosed(BrokerTransportError):
    """The inherited channel is closed for good; only a relaunch replaces it."""


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


def _read_packet(connection, *, allow_eof=False):
    """One bounded packet and its kernel credentials; passed descriptors are closed and refused.

    With allow_eof, an orderly peer close returns (None, []).
    """
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
    if allow_eof and not raw and not controls and not invalid and not flags:
        return None, []
    _require(not invalid and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
             and 0 < len(raw) <= _MAX_PACKET)
    return raw, credentials


def _receive(connection, peer):
    raw, credentials = _read_packet(connection)
    _require(credentials == [(peer.pid, peer.uid, peer.gid)])
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
    def __init__(self, listener, adapter, peer, *, admission=None):
        self._pid, self._closed, self._busy = os.getpid(), False, threading.Lock()
        _require(type(listener) is socket.socket and listener.family == socket.AF_UNIX
                 and listener.type & socket.SOCK_SEQPACKET == socket.SOCK_SEQPACKET
                 and listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
                 and type(adapter) is HostAdapter and type(peer) is PeerPin
                 and (admission is None or callable(admission)))
        self._admission = admission
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.listener, self.adapter, self.peer = listener, adapter, peer

    def serve_once(self, timeout_seconds, *, accept_timeout_seconds=None, read_timeout_seconds=None):
        acquired = False
        try:
            _require(os.getpid() == self._pid and not self._closed
                     and type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 125)
            acquired = self._busy.acquire(blocking=False)
            _require(acquired and not self._closed)
            for value in (accept_timeout_seconds, read_timeout_seconds):
                _require(value is None or (type(value) in (int, float) and 0 < value <= timeout_seconds))
            deadline = time.monotonic() + timeout_seconds
            _set_deadline(self.listener, min(deadline, time.monotonic() + (accept_timeout_seconds or timeout_seconds)))
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                raise BrokerAcceptTimeout() from None
            with connection:
                _set_deadline(connection, min(deadline, time.monotonic() + (read_timeout_seconds or timeout_seconds)))
                try:
                    request = _receive(connection, self.peer)
                except socket.timeout:
                    raise BrokerReadTimeout() from None
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
                if self._admission is not None and self._admission(deadline) is not True:
                    raise BrokerAdmissionClosed()
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
        except (BrokerAcceptTimeout, BrokerReadTimeout, BrokerAdmissionClosed):
            raise
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


class PeerAccount:
    """Expected kernel sender account on an inherited channel; not a process identity.

    A descriptor can be delegated, so any process of this uid/gid that holds it
    passes. A sender of any other account is refused and the channel closed.
    The sender pid is not compared: the kernel reports it as 0 when the sender's
    PID namespace isn't visible from the receiver's. A holder with CAP_SETUID or
    CAP_SETGID over its user namespace may claim any mapped account, so this
    refusal holds only for holders without those capabilities.
    """
    def __init__(self, *, uid, gid):
        _require(type(uid) is int and type(gid) is int and 0 <= uid < 2**32 - 1 and 0 <= gid < 2**32 - 1)
        self.uid, self.gid = uid, gid


def _adopt(fd):
    """Own an inherited connected SOCK_SEQPACKET descriptor; a refused one is closed too."""
    channel = None
    try:
        _require(sys.platform == 'linux' and hasattr(socket, 'SO_PASSCRED') and hasattr(socket, 'SCM_CREDENTIALS')
                 and type(fd) is int and fd >= 0)
        _require(stat.S_ISSOCK(os.fstat(fd).st_mode))
        channel = socket.socket(fileno=fd)
        _require(channel.family == socket.AF_UNIX
                 and channel.type & socket.SOCK_SEQPACKET == socket.SOCK_SEQPACKET
                 and channel.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0)
        channel.getpeername()   # refuses an unconnected socket
        channel.set_inheritable(False)
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        return channel
    except BaseException:
        try:
            if channel is not None:
                channel.close()
            elif type(fd) is int and fd >= 0:
                os.close(fd)
        except OSError:
            pass
        raise BrokerTransportError() from None


class _InheritedChannel:
    """One owned descriptor carrying strictly sequenced request/reply packets.

    Sequence numbers start at 1 and only ever advance by one, so a replayed or
    duplicated packet is refused. Every refusal, a timeout once a request was
    sent or read, and a peer close all close the channel for good.
    """
    def __init__(self, fd, peer):
        self._pid, self._closed, self._busy, self._sequence = os.getpid(), False, threading.Lock(), 0
        if type(peer) is not PeerAccount:
            _adopt(fd).close()
            raise BrokerTransportError()
        self._socket, self.peer = _adopt(fd), peer

    @property
    def closed(self):
        return self._closed

    def _poison(self):
        self._closed = True
        try:
            self._socket.close()
        except OSError:
            pass

    def _readable(self, deadline):
        remaining = max(0.0, deadline - time.monotonic())
        return bool(select.select([self._socket], [], [], remaining)[0])

    def _peer_open(self):
        """False once the peer has closed its end; queued packets don't change that."""
        hangup = select.POLLHUP | getattr(select, 'POLLRDHUP', 0)
        poller = select.poll()
        poller.register(self._socket, hangup)
        return not any(events & hangup for _, events in poller.poll(0))

    def _receive_value(self):
        raw, credentials = _read_packet(self._socket, allow_eof=True)
        if raw is None:
            raise BrokerChannelClosed()
        _require(len(credentials) == 1 and credentials[0][1:] == (self.peer.uid, self.peer.gid))
        return _parse(raw)

    def _enter(self, valid):
        """Take the call slot; a refused call leaves the channel as it was."""
        acquired = False
        try:
            _require(os.getpid() == self._pid and not self._closed and valid)
            acquired = self._busy.acquire(blocking=False)
            _require(acquired and not self._closed)
        except Exception:
            if acquired:
                self._busy.release()
            raise BrokerTransportError() from None

    def close(self):
        """Close when idle; a busy call must finish first. Owns and closes the descriptor."""
        _require(os.getpid() == self._pid)
        acquired = self._busy.acquire(blocking=False)
        _require(acquired)
        try:
            self._poison()
        finally:
            self._busy.release()


class InheritedCommandBroker(_InheritedChannel):
    """Executor side: reads one request and sends its one reply per serve_once."""
    def __init__(self, fd, adapter, peer, *, admission=None):
        if type(adapter) is not HostAdapter or not (admission is None or callable(admission)):
            _adopt(fd).close()
            raise BrokerTransportError()
        super().__init__(fd, peer)
        self.adapter, self._admission = adapter, admission

    def serve_once(self, timeout_seconds, *, read_timeout_seconds=None):
        self._enter(type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 125
                    and (read_timeout_seconds is None or (type(read_timeout_seconds) in (int, float)
                                                          and 0 < read_timeout_seconds <= timeout_seconds)))
        try:
            deadline = time.monotonic() + timeout_seconds
            if not self._readable(min(deadline, time.monotonic() + (read_timeout_seconds or timeout_seconds))):
                # Nothing was read, so nothing was admitted; the channel stays usable.
                raise BrokerReadTimeout()
            request = self._receive_value()
            _require(set(request) == {'protocol', 'sequence', 'action', 'payload'}
                     and request['protocol'] == _CHANNEL_PROTOCOL
                     and type(request['sequence']) is int and request['sequence'] == self._sequence + 1)
            self._sequence = request['sequence']
            action, payload = request['action'], request['payload']
            if action == 'submit':
                argument = ContainerPilotRequest.model_validate(payload)
            elif action in {'get', 'reconcile'}:
                argument = _uuid(payload)
            else:
                raise BrokerTransportError()
            _require(time.monotonic() < deadline and not self._closed)
            if self._admission is not None and self._admission(deadline) is not True:
                raise BrokerAdmissionClosed()
            _require(time.monotonic() < deadline and not self._closed)
            # A sender that already gave up (timed out or exited) gets no dispatch.
            # One that gives up after this check still leaves an unknown outcome.
            if not self._peer_open():
                raise BrokerChannelClosed()
            result = getattr(self.adapter, action)(argument)
            _require(result is None or type(result) is ContainerPilotResponse)
            _require(time.monotonic() < deadline)
            _set_deadline(self._socket, deadline)
            _send(self._socket, {'protocol': _CHANNEL_PROTOCOL, 'sequence': self._sequence,
                                 'result': None if result is None else result.model_dump(mode='json')})
        except BrokerReadTimeout:
            raise
        except (BrokerAdmissionClosed, BrokerChannelClosed):
            self._poison()
            raise
        except Exception:
            self._poison()
            raise BrokerTransportError() from None
        finally:
            self._busy.release()


class InheritedCommandClient(_InheritedChannel):
    """Coordinator side; any failure is an unknown outcome and closes the channel."""
    def _call(self, action, command, timeout_seconds):
        self._enter(type(command) is ContainerPilotRequest
                    and type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 125)
        try:
            deadline = time.monotonic() + timeout_seconds
            # Anything already waiting is unsolicited: a late, replayed or injected packet.
            _require(not self._readable(time.monotonic()))
            sequence = self._sequence + 1
            payload = command.model_dump(mode='json') if action == 'submit' else command.command_id
            _set_deadline(self._socket, deadline)
            _send(self._socket, {'protocol': _CHANNEL_PROTOCOL, 'sequence': sequence,
                                 'action': action, 'payload': payload})
            self._sequence = sequence
            _require(self._readable(deadline))
            reply = self._receive_value()
            _require(time.monotonic() < deadline and set(reply) == {'protocol', 'sequence', 'result'}
                     and reply['protocol'] == _CHANNEL_PROTOCOL and type(reply['sequence']) is int
                     and reply['sequence'] == sequence)
            if reply['result'] is None:
                _require(action != 'submit')
                return None
            result = ContainerPilotResponse.model_validate(reply['result'])
            _require(all(getattr(result, name) == getattr(command, name)
                         for name in ContainerPilotRequest.model_fields))
            return result
        except Exception:
            self._poison()
            raise BrokerTransportError() from None
        finally:
            self._busy.release()

    def submit(self, command, timeout_seconds=125):
        return self._call('submit', command, timeout_seconds)

    def get(self, command, timeout_seconds=125):
        return self._call('get', command, timeout_seconds)

    def reconcile(self, command, timeout_seconds=125):
        return self._call('reconcile', command, timeout_seconds)
