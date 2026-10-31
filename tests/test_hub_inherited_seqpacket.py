"""Inherited-descriptor pilot command channel: real Linux sockets, synthetic adapter, no Docker.

The descriptor can be delegated, so these tests pin what is and isn't proven:
another account is refused, but a same-account holder is not distinguished.
"""
import array
import asyncio
import os
import select
import socket
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux seqpacket credentials required')
pytest.importorskip('fcntl')

from hub_container_executor.broker_seqpacket import (
    BrokerChannelClosed, BrokerReadTimeout, BrokerTransportError, InheritedCommandBroker,
    InheritedCommandClient, PeerAccount, _CHANNEL_PROTOCOL, _canonical, _read_packet,
)
from tests.test_hub_container_executor_adapter import make_fixture, command


def me():
    return PeerAccount(uid=os.getuid(), gid=os.getgid())


def handoff(fd):
    """Pass a descriptor the way the supervisor would: SCM_RIGHTS over another socket."""
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with left, right:
        left.sendmsg([b'x'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))])
        os.close(fd)
        _, controls, _, _ = right.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))
    fds = array.array('i')
    fds.frombytes(controls[0][2][:fds.itemsize])
    return fds[0]


def closed_fd(fd):
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def raw_end(sock):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    return sock


def request_packet(manifest, sequence=1, action='submit'):
    request = command(manifest, 'deploy_container')
    payload = request.model_dump(mode='json') if action == 'submit' else request.command_id
    return _canonical({'protocol': _CHANNEL_PROTOCOL, 'sequence': sequence, 'action': action, 'payload': payload})


@pytest.fixture
def parts(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path)
    yield adapter, backend, manifest
    adapter.close()


@pytest.fixture
def rig(parts):
    adapter, backend, manifest = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    broker = InheritedCommandBroker(handoff(executor_end.detach()), adapter, me())
    client = InheritedCommandClient(handoff(coordinator_end.detach()), me())
    yield backend, manifest, broker, client
    for channel in (client, broker):
        if not channel.closed:
            channel.close()


def serve_once(broker, timeout=5, **options):
    errors = []
    def serve():
        try:
            broker.serve_once(timeout, **options)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=serve)
    thread.start()
    return thread, errors


def test_handed_over_descriptors_round_trip_in_sequence(rig):
    backend, manifest, broker, client = rig
    request = command(manifest, 'deploy_container')
    for call, check in ((client.submit, lambda r: r.state == 'succeeded'),
                        (client.submit, lambda r: r.replayed is True),
                        (client.get, lambda r: r.state == 'succeeded')):
        thread, errors = serve_once(broker)
        assert check(call(request, 5))
        thread.join(5)
        assert not thread.is_alive() and not errors
    assert backend.events.count('create') == 1 and backend.events.count('start') == 1
    assert broker._sequence == client._sequence == 3


def test_async_pilot_client_uses_the_handed_over_descriptor(rig):
    from engineering_hub.container_pilot_client import ContainerPilotClient
    backend, manifest, broker, _ = rig
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    broker.close()
    broker = InheritedCommandBroker(executor_end.detach(), broker.adapter, me())
    async def run():
        client = ContainerPilotClient.for_inherited_seqpacket(
            handoff(coordinator_end.detach()), peer_uid=os.getuid(), peer_gid=os.getgid(), timeout_seconds=5)
        assert type(client) is ContainerPilotClient
        request = command(manifest, 'deploy_container')
        try:
            thread, errors = serve_once(broker)
            assert (await client.submit_command(request)).state == 'succeeded'
            thread.join(5)
            assert not errors
            thread, errors = serve_once(broker)
            assert (await client.get_command(request)).state == 'succeeded'
            thread.join(5)
            assert not errors
        finally:
            await client.aclose()
    try:
        asyncio.run(run())
        assert backend.events.count('create') == 1
    finally:
        broker.close()


def test_async_pilot_client_refusal_still_closes_the_descriptor():
    from engineering_hub.container_pilot_client import ContainerPilotClient
    for options in ({'timeout_seconds': 0}, {'timeout_seconds': True}, {'peer_uid': -1}):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        fd = left.detach()
        try:
            arguments = dict(peer_uid=os.getuid(), peer_gid=os.getgid(), timeout_seconds=5)
            arguments.update(options)
            with pytest.raises(ValueError):
                ContainerPilotClient.for_inherited_seqpacket(fd, **arguments)
            assert closed_fd(fd)
        finally:
            right.close()


def test_a_different_sender_account_is_refused_and_closes_both_ends(parts):
    adapter, backend, manifest = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    broker = InheritedCommandBroker(executor_end.detach(), adapter,
                                    PeerAccount(uid=os.getuid() + 1, gid=os.getgid()))
    client = InheritedCommandClient(coordinator_end.detach(), me())
    thread, errors = serve_once(broker)
    with pytest.raises(BrokerTransportError):
        client.submit(command(manifest, 'deploy_container'), 5)
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and broker.closed and client.closed
    assert 'create' not in backend.events


@pytest.mark.skipif(not hasattr(os, 'geteuid') or os.geteuid() != 0, reason='needs root to switch account')
def test_a_delegated_holder_of_another_account_is_refused(rig):
    backend, manifest, broker, client = rig
    packet = request_packet(manifest)
    thread, errors = serve_once(broker)
    child = os.fork()
    if child == 0:
        try:
            delegated = socket.socket(fileno=os.dup(client._socket.fileno()))
            os.setgroups([])
            os.setresgid(65534, 65534, 65534)
            os.setresuid(65534, 65534, 65534)
            delegated.send(packet)
        finally:
            os._exit(0)
    os.waitpid(child, 0)
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and broker.closed
    assert 'create' not in backend.events


def test_a_same_account_delegated_holder_is_not_distinguished_but_desyncs_closed(rig):
    # The limit of this channel: a same-account process that holds the descriptor
    # is accepted. The real coordinator then fails closed instead of continuing.
    backend, manifest, broker, client = rig
    packet = request_packet(manifest)
    thread, errors = serve_once(broker)
    child = os.fork()
    if child == 0:
        try:
            socket.socket(fileno=os.dup(client._socket.fileno())).send(packet)
        finally:
            os._exit(0)
    os.waitpid(child, 0)
    thread.join(5)
    assert not errors and backend.events.count('create') == 1
    with pytest.raises(BrokerTransportError):
        client.get(command(manifest, 'deploy_container'), 5)   # the unread reply is unsolicited
    assert client.closed
    with pytest.raises(BrokerChannelClosed):
        broker.serve_once(5)
    assert broker.closed


def test_coordinator_exit_closes_the_executor_side_without_dispatch(parts):
    adapter, backend, manifest = parts
    for sends_first in (False, True):
        executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        broker = InheritedCommandBroker(executor_end.detach(), adapter, me())
        child = os.fork()
        if child == 0:
            try:
                if sends_first:
                    raw_end(coordinator_end).send(request_packet(manifest))
            finally:
                os._exit(0)
        coordinator_end.close()
        os.waitpid(child, 0)
        # A request whose sender already exited is read but never dispatched.
        with pytest.raises(BrokerChannelClosed):
            broker.serve_once(5)
        assert broker.closed and 'create' not in backend.events


def test_executor_exit_closes_the_coordinator_side(parts):
    _, _, manifest = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = InheritedCommandClient(coordinator_end.detach(), me())
    child = os.fork()
    if child == 0:
        os._exit(0)
    executor_end.close()
    os.waitpid(child, 0)
    with pytest.raises(BrokerTransportError):
        client.submit(command(manifest, 'deploy_container'), 5)
    assert client.closed


def test_client_timeout_closes_and_the_late_request_is_never_dispatched(rig):
    backend, manifest, broker, client = rig
    started = time.monotonic()
    with pytest.raises(BrokerTransportError):
        client.submit(command(manifest, 'deploy_container'), 0.2)
    assert time.monotonic() - started < 2 and client.closed
    with pytest.raises(BrokerChannelClosed):
        broker.serve_once(5)
    assert broker.closed and 'create' not in backend.events


def test_an_idle_read_timeout_keeps_the_channel(rig):
    backend, manifest, broker, client = rig
    with pytest.raises(BrokerReadTimeout):
        broker.serve_once(5, read_timeout_seconds=0.1)
    assert not broker.closed
    thread, errors = serve_once(broker)
    assert client.submit(command(manifest, 'deploy_container'), 5).state == 'succeeded'
    thread.join(5)
    assert not errors and backend.events.count('create') == 1


def test_a_replayed_request_packet_is_refused(parts):
    adapter, backend, manifest = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    broker = InheritedCommandBroker(executor_end.detach(), adapter, me())
    with raw_end(coordinator_end) as raw:
        packet = request_packet(manifest)
        raw.send(packet)
        broker.serve_once(5)
        assert raw.recv(65536)
        raw.send(packet)
        with pytest.raises(BrokerTransportError):
            broker.serve_once(5)
    assert broker.closed and backend.events.count('create') == 1


@pytest.mark.parametrize('sequence', [0, 2, -1, True, '1', 1.5])
def test_a_first_request_out_of_sequence_is_refused(parts, sequence):
    adapter, backend, manifest = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    broker = InheritedCommandBroker(executor_end.detach(), adapter, me())
    with raw_end(coordinator_end) as raw:
        request = command(manifest, 'deploy_container')
        raw.send(_canonical({'protocol': _CHANNEL_PROTOCOL, 'sequence': sequence, 'action': 'submit',
                             'payload': request.model_dump(mode='json')}))
        with pytest.raises(BrokerTransportError):
            broker.serve_once(5)
    assert broker.closed and 'create' not in backend.events


def test_an_unsolicited_packet_closes_the_client_before_it_sends(parts):
    _, _, manifest = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = InheritedCommandClient(coordinator_end.detach(), me())
    with raw_end(executor_end) as raw:
        raw.send(_canonical({'protocol': _CHANNEL_PROTOCOL, 'sequence': 1, 'result': None}))
        with pytest.raises(BrokerTransportError):
            client.get(command(manifest, 'deploy_container'), 5)
        assert client.closed
        assert select.select([raw], [], [], 1)[0] and raw.recv(65536) == b''   # nothing was sent


@pytest.mark.parametrize('reply_sequence', [2, 0])
def test_a_reply_out_of_sequence_is_refused(parts, reply_sequence):
    _, _, manifest = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = InheritedCommandClient(coordinator_end.detach(), me())
    errors = []
    def call():
        try:
            client.get(command(manifest, 'deploy_container'), 5)
        except Exception as error:
            errors.append(error)
    with raw_end(executor_end) as raw:
        thread = threading.Thread(target=call)
        thread.start()
        assert select.select([raw], [], [], 5)[0] and raw.recv(65536)
        raw.send(_canonical({'protocol': _CHANNEL_PROTOCOL, 'sequence': reply_sequence, 'result': None}))
        thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and client.closed


def test_descriptors_passed_on_the_channel_are_closed_and_refused(parts):
    adapter, backend, manifest = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    broker = InheritedCommandBroker(executor_end.detach(), adapter, me())
    readfd, writefd = os.pipe()
    try:
        with raw_end(coordinator_end) as raw:
            raw.sendmsg([request_packet(manifest)],
                        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [readfd]))])
            with pytest.raises(BrokerTransportError):
                broker.serve_once(5)
    finally:
        os.close(readfd)
        os.close(writefd)
    assert broker.closed and 'create' not in backend.events


def _pipe_fd():
    readfd, writefd = os.pipe()
    os.close(writefd)
    return readfd, []


def _stream_fd():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return left.detach(), [right]


def _listening_fd(tmp_path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(str(tmp_path / 'listen.sock'))
    listener.listen(1)
    return listener.detach(), []


def _unconnected_fd():
    return socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET).detach(), []


def _inet_fd():
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM).detach(), []


@pytest.mark.parametrize('make', ['pipe', 'stream', 'listening', 'unconnected', 'inet'])
def test_adoption_refuses_anything_but_a_connected_seqpacket_and_closes_it(tmp_path, make):
    fd, keep = {'pipe': _pipe_fd, 'stream': _stream_fd, 'listening': lambda: _listening_fd(tmp_path),
                'unconnected': _unconnected_fd, 'inet': _inet_fd}[make]()
    try:
        with pytest.raises(BrokerTransportError):
            InheritedCommandClient(fd, me())
        assert closed_fd(fd)
    finally:
        for item in keep:
            item.close()


@pytest.mark.parametrize('fd', [-1, True, None, '3'])
def test_adoption_refuses_non_descriptors(fd):
    with pytest.raises(BrokerTransportError):
        InheritedCommandClient(fd, me())


def test_a_wrong_peer_or_adapter_still_closes_the_descriptor(parts):
    adapter, _, _ = parts
    for build in (lambda fd: InheritedCommandClient(fd, None),
                  lambda fd: InheritedCommandBroker(fd, object(), me()),
                  lambda fd: InheritedCommandBroker(fd, adapter, me(), admission='yes')):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        fd = left.detach()
        try:
            with pytest.raises(BrokerTransportError):
                build(fd)
            assert closed_fd(fd)
        finally:
            right.close()


def test_peer_account_rejects_malformed_ids():
    for uid, gid in ((-1, 0), (0, 2**32 - 1), (True, 0), (0, '0'), (None, 0)):
        with pytest.raises(BrokerTransportError):
            PeerAccount(uid=uid, gid=gid)


def _root_only():
    return pytest.mark.skipif(not hasattr(os, 'geteuid') or os.geteuid() != 0, reason='needs root')


@_root_only()
@pytest.mark.skipif(not hasattr(os, 'unshare') or not hasattr(os, 'CLONE_NEWPID'), reason='needs os.unshare')
def test_a_receiver_in_a_child_pid_namespace_sees_pid_0_and_still_checks_the_account():
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    report_read, report_write = os.pipe()
    child = os.fork()
    if child == 0:
        status = 1
        try:
            coordinator_end.close()
            os.close(report_read)
            os.unshare(os.CLONE_NEWPID)
            grandchild = os.fork()
            if grandchild == 0:
                code = 1
                try:
                    channel = InheritedCommandClient(executor_end.detach(), me())
                    assert select.select([channel._socket], [], [], 5)[0]
                    _, credentials = _read_packet(channel._socket)
                    accepted = len(credentials) == 1 and credentials[0][1:] == (os.getuid(), os.getgid())
                    os.write(report_write, b'%d %d' % (credentials[0][0], accepted))
                    code = 0
                finally:
                    os._exit(code)
            _, waited = os.waitpid(grandchild, 0)
            status = 0 if os.WIFEXITED(waited) and os.WEXITSTATUS(waited) == 0 else 1
        finally:
            os._exit(status)
    executor_end.close()
    os.close(report_write)
    with raw_end(coordinator_end) as raw:
        raw.send(b'{}')
        _, waited = os.waitpid(child, 0)
    report = os.read(report_read, 64)
    os.close(report_read)
    assert os.WIFEXITED(waited) and os.WEXITSTATUS(waited) == 0
    assert report == b'0 1'   # the sender's pid isn't visible there; the account still matches


@_root_only()
def test_a_privileged_holder_can_claim_the_expected_account(rig):
    # Pins the documented limit: CAP_SETUID/CAP_SETGID lets a holder attach
    # credentials of another mapped account, so the profile must drop them.
    backend, manifest, broker, client = rig
    other = PeerAccount(uid=65534, gid=65534)
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    claimed = InheritedCommandBroker(executor_end.detach(), broker.adapter, other)
    try:
        with raw_end(coordinator_end) as raw:
            creds = array.array('i', [os.getpid(), 65534, 65534])
            raw.sendmsg([request_packet(manifest)],
                        [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, creds.tobytes())])
            claimed.serve_once(5)
            assert raw.recv(65536)
        assert backend.events.count('create') == 1
    finally:
        claimed.close()


def steal_after_readiness(channel, thief):
    """Report readiness, then let another holder take the ready packet before the read."""
    original = channel._readable
    def readable(deadline):
        ready = original(deadline)
        if ready:
            thief.setblocking(False)
            thief.recv(65536)
        return ready
    channel._readable = readable


def test_a_request_taken_by_another_holder_fails_closed_without_blocking(rig):
    backend, manifest, broker, client = rig
    thief = socket.socket(fileno=os.dup(broker._socket.fileno()))
    try:
        steal_after_readiness(broker, thief)
        thread, errors = serve_once(broker, timeout=30)
        started = time.monotonic()
        with pytest.raises(BrokerTransportError):
            client.submit(command(manifest, 'deploy_container'), 2)
        thread.join(5)
        assert not thread.is_alive() and time.monotonic() - started < 5
        assert len(errors) == 1 and not isinstance(errors[0], BrokerReadTimeout)
        assert broker.closed and client.closed and 'create' not in backend.events
    finally:
        thief.close()


def test_a_reply_taken_by_another_holder_fails_the_client_closed_without_blocking(rig):
    backend, manifest, broker, client = rig
    thief = socket.socket(fileno=os.dup(client._socket.fileno()))
    try:
        original = client._readable
        calls = []
        def readable(deadline):
            calls.append(deadline)
            ready = original(deadline)
            if ready and len(calls) == 2:   # the first call is the unsolicited-packet check
                thief.setblocking(False)
                thief.recv(65536)
            return ready
        client._readable = readable
        thread, errors = serve_once(broker)
        started = time.monotonic()
        with pytest.raises(BrokerTransportError):
            client.submit(command(manifest, 'deploy_container'), 30)
        assert time.monotonic() - started < 5 and client.closed
        thread.join(5)
        assert not thread.is_alive() and not errors
        assert backend.events.count('create') == 1   # dispatched; the coordinator's outcome is unknown
    finally:
        thief.close()


def test_a_peer_exit_wakes_a_waiting_executor_promptly(parts):
    adapter, backend, _ = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    broker = InheritedCommandBroker(executor_end.detach(), adapter, me())
    child = os.fork()
    if child == 0:
        time.sleep(0.3)
        os._exit(0)
    coordinator_end.close()
    started = time.monotonic()
    with pytest.raises(BrokerChannelClosed):
        broker.serve_once(30)
    os.waitpid(child, 0)
    assert time.monotonic() - started < 5 and broker.closed and 'create' not in backend.events


def test_a_peer_exit_wakes_a_waiting_coordinator_promptly(parts):
    _, _, manifest = parts
    executor_end, coordinator_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = InheritedCommandClient(coordinator_end.detach(), me())
    child = os.fork()
    if child == 0:
        time.sleep(0.3)
        os._exit(0)
    executor_end.close()
    started = time.monotonic()
    with pytest.raises(BrokerTransportError):
        client.get(command(manifest, 'deploy_container'), 30)
    os.waitpid(child, 0)
    assert time.monotonic() - started < 5 and client.closed
