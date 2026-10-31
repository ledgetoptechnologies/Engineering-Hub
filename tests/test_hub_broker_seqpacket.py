"""Real Linux seqpacket credentials; synthetic adapter and no Docker effects."""
import array
import os
import socket
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or not hasattr(os, 'pidfd_open'),
                                reason='Linux seqpacket credentials and pidfd required')
pytest.importorskip('fcntl')

from hub_container_executor.broker_seqpacket import (
    BrokerTransportError, PeerPin, PilotCommandBroker, PilotCommandClient, _start, _canonical, _PROTOCOL,
)
from tests.test_hub_container_executor_adapter import make_fixture, command


@pytest.fixture
def rig(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path)
    path = str(tmp_path / 'broker.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(path)
    listener.listen(2)
    pin = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    broker = PilotCommandBroker(listener, adapter, pin)
    client = PilotCommandClient(path, pin)
    yield adapter, backend, manifest, path, broker, client
    client.close()
    broker.close()
    pin.close()
    listener.close()
    adapter.close()


def start_once(broker):
    errors = []
    def serve():
        try:
            broker.serve_once(5)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=serve)
    thread.start()
    return thread, errors


def test_real_seqpacket_submit_and_replay_use_original_adapter(rig):
    _, backend, manifest, _, broker, client = rig
    request = command(manifest, 'deploy_container')
    thread, errors = start_once(broker)
    response = client.submit(request, 5)
    thread.join(5)
    assert not thread.is_alive() and not errors and response.state == 'succeeded'
    thread, errors = start_once(broker)
    replay = client.submit(request, 5)
    thread.join(5)
    assert not thread.is_alive() and not errors and replay.replayed is True
    assert backend.events.count('create') == 1 and backend.events.count('start') == 1
    thread, errors = start_once(broker)
    assert client.get(request, 5).state == 'succeeded'
    thread.join(5)
    assert not errors


def test_inherited_connected_socket_packet_from_child_is_rejected(rig):
    _, backend, manifest, path, broker, _ = rig
    request = command(manifest, 'deploy_container')
    thread, errors = start_once(broker)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock.connect(path)
    child = os.fork()
    if child == 0:
        try:
            sock.send(_canonical({'protocol': _PROTOCOL, 'action': 'submit',
                                  'payload': request.model_dump(mode='json')}))
        finally:
            os._exit(0)
    os.waitpid(child, 0)
    sock.close()
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], BrokerTransportError)
    assert 'create' not in backend.events


def test_scm_rights_is_rejected_without_dispatch(rig):
    _, backend, manifest, path, broker, _ = rig
    request = command(manifest, 'deploy_container')
    thread, errors = start_once(broker)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.connect(path)
        readfd, writefd = os.pipe()
        try:
            packet = _canonical({'protocol': _PROTOCOL, 'action': 'submit',
                                 'payload': request.model_dump(mode='json')})
            sock.sendmsg([packet], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [readfd]))])
        finally:
            os.close(readfd)
            os.close(writefd)
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1
    assert 'create' not in backend.events


def test_wrong_process_start_and_peer_credentials_refuse(rig):
    _, backend, manifest, path, broker, _ = rig
    with pytest.raises(BrokerTransportError):
        PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()) + 1)
    request = command(manifest, 'deploy_container')
    thread, errors = start_once(broker)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.connect(path)
        sock.send(_canonical({'protocol': _PROTOCOL, 'action': 'arbitrary', 'payload': request.command_id}))
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1
    assert 'create' not in backend.events


def test_connection_transferred_to_child_cannot_impersonate_original_peer(rig):
    _, backend, manifest, path, broker, _ = rig
    request = command(manifest, 'deploy_container')
    parent, child_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    child = os.fork()
    if child == 0:
        parent.close()
        try:
            _, controls, _, _ = child_channel.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))
            fds = array.array('i')
            fds.frombytes(controls[0][2][:array.array('i').itemsize])
            with socket.socket(fileno=fds[0]) as transferred:
                transferred.send(_canonical({'protocol': _PROTOCOL, 'action': 'submit',
                    'payload': request.model_dump(mode='json')}))
        finally:
            os._exit(0)
    child_channel.close()
    thread, errors = start_once(broker)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.connect(path)
        parent.sendmsg([b'x'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [connection.fileno()]))])
        os.waitpid(child, 0)
    parent.close()
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1
    assert 'create' not in backend.events


def test_different_uid_pin_rejects_real_sender(rig):
    adapter, backend, manifest, _, broker, _ = rig
    wrong = PeerPin(pid=os.getpid(), uid=os.getuid() + 1, gid=os.getgid(), start_ticks=_start(os.getpid()))
    alternate = PilotCommandBroker(broker.listener, adapter, wrong)
    thread, errors = start_once(alternate)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.connect(broker.listener.getsockname())
        request = command(manifest, 'deploy_container')
        connection.send(_canonical({'protocol': _PROTOCOL, 'action': 'submit',
                                    'payload': request.model_dump(mode='json')}))
    thread.join(5)
    alternate.close()
    wrong.close()
    assert not thread.is_alive() and len(errors) == 1 and 'create' not in backend.events


@pytest.mark.parametrize('payload', [b'x' * 32769, b'{"action":"get","action":"get"}', b'{}', b''])
def test_malformed_or_oversized_packet_never_reaches_adapter(rig, payload):
    _, backend, _, path, broker, _ = rig
    thread, errors = start_once(broker)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.connect(path)
        connection.send(payload)
    thread.join(5)
    assert not thread.is_alive() and len(errors) == 1
    assert 'create' not in backend.events


def test_client_rejects_unpinned_response_identity(rig):
    _, backend, manifest, path, broker, _ = rig
    wrong = PeerPin(pid=os.getpid(), uid=os.getuid() + 1, gid=os.getgid(), start_ticks=_start(os.getpid()))
    client = PilotCommandClient(path, wrong)
    request = command(manifest, 'deploy_container')
    thread, _ = start_once(broker)
    with pytest.raises(BrokerTransportError):
        client.submit(request, 5)
    thread.join(5)
    assert not thread.is_alive() and 'create' not in backend.events
    client.close()
    wrong.close()


def test_close_reports_busy_accept_and_succeeds_after_owner_drains(rig):
    import time
    _, _, _, _, broker, _ = rig
    errors = []
    thread = threading.Thread(target=lambda: _capture_server(broker, errors, .2))
    thread.start()
    end = time.monotonic() + 2
    while not broker._busy.locked() and time.monotonic() < end:
        time.sleep(.001)
    assert broker._busy.locked()
    with pytest.raises(BrokerTransportError):
        broker.close()
    thread.join(2)
    assert not thread.is_alive() and len(errors) == 1
    broker.close()


def _capture_server(broker, errors, timeout):
    try:
        broker.serve_once(timeout)
    except Exception as error:
        errors.append(error)


def test_expired_during_parse_never_dispatches(rig, monkeypatch):
    import time
    from hub_container_executor import broker_seqpacket as module
    _, backend, manifest, path, broker, _ = rig
    request = command(manifest, 'deploy_container')
    original = module._parse
    def slow_parse(raw):
        result = original(raw)
        time.sleep(.15)
        return result
    monkeypatch.setattr(module, '_parse', slow_parse)
    errors = []
    thread = threading.Thread(target=lambda: _capture_server(broker, errors, .1))
    thread.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
        connection.connect(path)
        connection.send(_canonical({'protocol': _PROTOCOL, 'action': 'submit',
                                    'payload': request.model_dump(mode='json')}))
    thread.join(3)
    assert not thread.is_alive() and len(errors) == 1 and 'create' not in backend.events


def test_receive_attempts_all_passed_fd_closes_after_error(monkeypatch):
    from hub_container_executor import broker_seqpacket as module
    fds = array.array('i', [41, 42, 43])
    class Connection:
        def recvmsg(self, *_):
            return b'{}', [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds.tobytes())], socket.MSG_CTRUNC, None
    class Peer:
        pid = uid = gid = 0
        def check(self):
            pytest.fail('must reject ancillary rights')
    closed = []
    def close(fd):
        closed.append(fd)
        if fd == 41:
            raise OSError('injected')
    monkeypatch.setattr(module.os, 'close', close)
    with pytest.raises(BrokerTransportError):
        module._receive(Connection(), Peer())
    assert closed == [41, 42, 43]


def test_distinct_pinned_child_process_is_accepted(tmp_path):
    from uuid import uuid4
    adapter, backend, _, _ = make_fixture(tmp_path)
    path = str(tmp_path / 'process.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(path)
    listener.listen(1)
    ready_r, ready_w = os.pipe()
    result_r, result_w = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_w)
        os.close(result_r)
        try:
            os.read(ready_r, 1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
                sock.connect(path)
                sock.send(_canonical({'protocol': _PROTOCOL, 'action': 'get', 'payload': str(uuid4())}))
                reply = sock.recv(32768)
                os.write(result_w, b'1' if b'"result":null' in reply else b'0')
        finally:
            os._exit(0)
    os.close(ready_r)
    os.close(result_w)
    pin = PeerPin(pid=child, uid=os.getuid(), gid=os.getgid(), start_ticks=_start(child))
    broker = PilotCommandBroker(listener, adapter, pin)
    try:
        os.write(ready_w, b'1')
        broker.serve_once(5)
        assert os.read(result_r, 1) == b'1'
    finally:
        os.waitpid(child, 0)
        broker.close()
        pin.close()
        listener.close()
        adapter.close()
        os.close(ready_w)
        os.close(result_r)
    assert 'create' not in backend.events


def test_client_does_not_connect_after_deadline_is_exhausted(rig, monkeypatch):
    import time
    from hub_container_executor import broker_seqpacket as module
    _, backend, manifest, _, broker, client = rig
    original = module._identity
    def delayed(path):
        result = original(path)
        time.sleep(.1)
        return result
    monkeypatch.setattr(module, '_identity', delayed)
    request = command(manifest, 'deploy_container')
    with pytest.raises(BrokerTransportError):
        client.submit(request, .05)
    assert 'create' not in backend.events


def test_exact_coordinator_client_uses_real_seqpacket_adapter(rig):
    import asyncio
    from engineering_hub.container_pilot_client import ContainerPilotClient
    _, backend, manifest, path, broker, _ = rig
    async def run():
        client = ContainerPilotClient.for_broker_seqpacket(path, peer_pid=os.getpid(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), peer_start_ticks=_start(os.getpid()), timeout_seconds=5)
        assert type(client) is ContainerPilotClient
        request = command(manifest, 'deploy_container')
        try:
            thread, errors = start_once(broker)
            assert (await client.submit_command(request)).state == 'succeeded'
            thread.join(5)
            assert not errors
            thread, errors = start_once(broker)
            assert (await client.submit_command(request)).replayed is True
            thread.join(5)
            assert not errors
            thread, errors = start_once(broker)
            assert (await client.get_command(request)).state == 'succeeded'
            thread.join(5)
            assert not errors
        finally:
            await client.aclose()
        assert backend.events.count('create') == 1
    asyncio.run(run())


def test_cancelled_async_client_drains_active_call_before_close(rig):
    import asyncio
    from engineering_hub.container_pilot_client import ContainerPilotClient
    adapter, _, manifest, path, broker, _ = rig
    started, release = threading.Event(), threading.Event()
    original_submit = adapter.submit
    def delayed(request):
        started.set()
        assert release.wait(5)
        return original_submit(request)
    adapter.submit = delayed
    async def run():
        client = ContainerPilotClient.for_broker_seqpacket(path, peer_pid=os.getpid(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), peer_start_ticks=_start(os.getpid()), timeout_seconds=5)
        thread, errors = start_once(broker)
        task = asyncio.create_task(client.submit_command(command(manifest, 'deploy_container')))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(.05)
        close = asyncio.create_task(client.aclose())
        await asyncio.sleep(.05)
        assert not task.done() and not close.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await close
        thread.join(5)
        assert not thread.is_alive() and not errors
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_owned_broker_service_drains_and_closes_real_adapter(tmp_path):
    import asyncio
    from contextlib import contextmanager
    from engineering_hub.container_pilot_client import ContainerPilotClient
    from hub_container_executor.broker_service import serve_owned_broker
    adapter, backend, _, manifest = make_fixture(tmp_path)
    path = str(tmp_path / 'owned.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(path)
    listener.listen(2)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    entered = []
    @contextmanager
    def factory():
        entered.append('enter')
        try:
            yield adapter
        finally:
            entered.append('exit')
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_broker(factory, listener=listener, peer=peer,
            stop_event=stop, on_ready=ready.set, command_timeout_seconds=5))
        await asyncio.wait_for(ready.wait(), 3)
        client = ContainerPilotClient.for_broker_seqpacket(path, peer_pid=os.getpid(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), peer_start_ticks=_start(os.getpid()), timeout_seconds=5)
        try:
            request = command(manifest, 'deploy_container')
            assert (await client.submit_command(request)).state == 'succeeded'
            stop.set()
            await asyncio.wait_for(service, 5)
        finally:
            await client.aclose()
        assert adapter._closed and listener.fileno() == -1 and peer.fd is None
        assert entered == ['enter', 'exit'] and backend.events.count('create') == 1
    asyncio.run(run())


def test_owned_broker_cancel_drains_accepted_adapter_work(tmp_path):
    import asyncio
    from contextlib import contextmanager
    from engineering_hub.container_pilot_client import ContainerPilotClient
    from hub_container_executor.broker_service import serve_owned_broker
    adapter, _, _, manifest = make_fixture(tmp_path)
    path = str(tmp_path / 'drain.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(path)
    listener.listen(2)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    started, release = threading.Event(), threading.Event()
    submit = adapter.submit
    def delayed(request):
        started.set()
        assert release.wait(5)
        return submit(request)
    adapter.submit = delayed
    @contextmanager
    def factory():
        yield adapter
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_broker(factory, listener=listener, peer=peer,
            stop_event=stop, on_ready=ready.set, command_timeout_seconds=5))
        await asyncio.wait_for(ready.wait(), 3)
        client = ContainerPilotClient.for_broker_seqpacket(path, peer_pid=os.getpid(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), peer_start_ticks=_start(os.getpid()), timeout_seconds=5)
        call = asyncio.create_task(client.submit_command(command(manifest, 'deploy_container')))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            service.cancel()
            await asyncio.sleep(.05)
            assert not service.done() and not adapter._closed and listener.fileno() >= 0
            release.set()
            assert (await call).state == 'succeeded'
            with pytest.raises(asyncio.CancelledError):
                await service
            assert adapter._closed and listener.fileno() == -1 and peer.fd is None
        finally:
            release.set()
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_stop_wins_admission_when_request_waits_before_dispatch(tmp_path, monkeypatch, cancel):
    import asyncio
    from contextlib import contextmanager
    from hub_container_executor import broker_seqpacket as module
    from hub_container_executor.broker_service import serve_owned_broker
    adapter, backend, _, manifest = make_fixture(tmp_path)
    path = str(tmp_path / 'stop-race.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(path)
    listener.listen(2)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    parsed, proceed = threading.Event(), threading.Event()
    original = module._parse
    def pause(raw):
        value = original(raw)
        parsed.set()
        assert proceed.wait(5)
        return value
    monkeypatch.setattr(module, '_parse', pause)
    @contextmanager
    def factory():
        yield adapter
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_broker(factory, listener=listener, peer=peer,
            stop_event=stop, on_ready=ready.set, command_timeout_seconds=5))
        await asyncio.wait_for(ready.wait(), 2)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            connection.connect(path)
            request = command(manifest, 'deploy_container')
            connection.send(_canonical({'protocol': _PROTOCOL, 'action': 'submit',
                                        'payload': request.model_dump(mode='json')}))
            assert await asyncio.to_thread(parsed.wait, 2)
            if cancel:
                service.cancel()
                await asyncio.sleep(.05)
            else:
                stop.set()
            proceed.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(service, 3)
            else:
                await asyncio.wait_for(service, 3)
            assert 'create' not in backend.events and 'start' not in backend.events
        finally:
            proceed.set()
            connection.close()
    asyncio.run(run())


def test_stale_readability_accept_timeout_is_short(tmp_path):
    from hub_container_executor.broker_seqpacket import BrokerAcceptTimeout
    import time
    adapter, _, _, _ = make_fixture(tmp_path)
    path = str(tmp_path / 'stale.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(path)
    listener.listen(1)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    broker = PilotCommandBroker(listener, adapter, peer)
    try:
        start = time.monotonic()
        with pytest.raises(BrokerAcceptTimeout):
            broker.serve_once(5, accept_timeout_seconds=.05, read_timeout_seconds=.1)
        assert time.monotonic() - start < 1
    finally:
        broker.close()
        peer.close()
        listener.close()
        adapter.close()


def test_stop_during_incomplete_packet_is_bounded_and_no_effect(tmp_path):
    import asyncio
    import time
    from contextlib import contextmanager
    from hub_container_executor.broker_service import serve_owned_broker
    adapter, backend, _, _ = make_fixture(tmp_path)
    path = str(tmp_path / 'idle.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(path)
    listener.listen(1)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    @contextmanager
    def factory():
        yield adapter
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_broker(factory, listener=listener, peer=peer,
            stop_event=stop, on_ready=ready.set, command_timeout_seconds=5))
        await asyncio.wait_for(ready.wait(), 2)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
            connection.connect(path)
            await asyncio.sleep(.1)
            start = time.monotonic()
            stop.set()
            await asyncio.wait_for(service, 2)
            assert time.monotonic() - start < 1.5
        assert 'create' not in backend.events and adapter._closed
    asyncio.run(run())


def test_incomplete_packet_timeout_then_valid_command_no_retry(tmp_path):
    import asyncio
    from contextlib import contextmanager
    from engineering_hub.container_pilot_client import ContainerPilotClient
    from hub_container_executor.broker_service import serve_owned_broker
    adapter, backend, _, manifest = make_fixture(tmp_path)
    path = str(tmp_path / 'timeout-then-valid.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(path)
    listener.listen(2)
    peer = PeerPin(pid=os.getpid(), uid=os.getuid(), gid=os.getgid(), start_ticks=_start(os.getpid()))
    @contextmanager
    def factory():
        yield adapter
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_broker(factory, listener=listener, peer=peer,
            stop_event=stop, on_ready=ready.set, command_timeout_seconds=3))
        await asyncio.wait_for(ready.wait(), 2)
        client = ContainerPilotClient.for_broker_seqpacket(path, peer_pid=os.getpid(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), peer_start_ticks=_start(os.getpid()), timeout_seconds=3)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as idle:
                idle.connect(path)
                await asyncio.sleep(1.2)
                assert not service.done() and 'create' not in backend.events
                result = await client.submit_command(command(manifest, 'deploy_container'))
                assert result.state == 'succeeded'
            stop.set()
            await asyncio.wait_for(service, 3)
            assert backend.events.count('create') == 1 and backend.events.count('start') == 1
        finally:
            await client.aclose()
    asyncio.run(run())
