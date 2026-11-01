"""Real seqpacket, synthetic executor; no installed launcher or host Engine."""
import asyncio
from contextlib import contextmanager
import os
import socket
import sys
import threading

import pytest
pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux seqpacket required')
pytest.importorskip('fcntl')

from engineering_hub.container_pilot_client import ContainerPilotClient, ContainerPilotClientError, UnknownOutcome
from hub_container_executor.broker_seqpacket import PeerAccount, BrokerTransportError
from hub_container_executor.broker_service import serve_owned_inherited_broker, BrokerCleanupReport
from tests.test_hub_container_executor_adapter import make_fixture, command


def test_authenticated_drain_acknowledges_only_after_owned_cleanup(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path)
    executor, coordinator = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    events = []
    @contextmanager
    def factory():
        try: yield adapter
        finally: events.append(('exit', adapter._closed))
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_inherited_broker(factory, fd=executor.detach(),
            peer=PeerAccount(uid=os.getuid(), gid=os.getgid()), stop_event=stop, cleanup_report=BrokerCleanupReport(),
            on_ready=ready.set, command_timeout_seconds=5))
        client = ContainerPilotClient.for_inherited_seqpacket(coordinator.detach(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), timeout_seconds=5)
        try:
            await asyncio.wait_for(ready.wait(), 3)
            assert (await client.submit_command(command(manifest, 'deploy_container'))).state == 'succeeded'
            await client.drain_inherited_broker()
            assert events == [('exit', True)] and adapter._closed
            await asyncio.wait_for(service, 5)
            with pytest.raises(ContainerPilotClientError): await client.drain_inherited_broker()
        finally:
            stop.set()
            await client.aclose()
            if not service.done(): await asyncio.wait_for(service, 5)
        assert backend.events.count('create') == 1
    asyncio.run(run())


def test_cancel_drains_accepted_work_before_closing(tmp_path):
    adapter, _, _, manifest = make_fixture(tmp_path)
    executor, coordinator = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    started, release = threading.Event(), threading.Event()
    submit = adapter.submit
    def delayed(request):
        started.set()
        assert release.wait(5)
        return submit(request)
    adapter.submit = delayed
    @contextmanager
    def factory(): yield adapter
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_inherited_broker(factory, fd=executor.detach(),
            peer=PeerAccount(uid=os.getuid(), gid=os.getgid()), stop_event=stop, cleanup_report=BrokerCleanupReport(),
            on_ready=ready.set, command_timeout_seconds=5))
        client = ContainerPilotClient.for_inherited_seqpacket(coordinator.detach(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), timeout_seconds=5)
        try:
            await asyncio.wait_for(ready.wait(), 3)
            call = asyncio.create_task(client.submit_command(command(manifest, 'deploy_container')))
            assert await asyncio.to_thread(started.wait, 2)
            service.cancel()
            await asyncio.sleep(.05)
            assert not service.done() and not adapter._closed
            release.set()
            assert (await call).state == 'succeeded'
            with pytest.raises(asyncio.CancelledError): await service
            assert adapter._closed
        finally:
            release.set()
            await client.aclose()
    asyncio.run(run())


def test_cleanup_failure_never_acknowledges_drain(tmp_path):
    adapter, _, _, _ = make_fixture(tmp_path)
    executor, coordinator = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    @contextmanager
    def factory():
        try: yield adapter
        finally: raise RuntimeError('private fixture detail')
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_inherited_broker(factory, fd=executor.detach(),
            peer=PeerAccount(uid=os.getuid(), gid=os.getgid()), stop_event=stop, cleanup_report=BrokerCleanupReport(),
            on_ready=ready.set, command_timeout_seconds=5))
        client = ContainerPilotClient.for_inherited_seqpacket(coordinator.detach(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), timeout_seconds=5)
        try:
            await asyncio.wait_for(ready.wait(), 3)
            with pytest.raises(ContainerPilotClientError): await client.drain_inherited_broker()
            with pytest.raises(BrokerTransportError) as caught: await asyncio.wait_for(service, 5)
            assert 'private fixture detail' not in str(caught.value)
            assert adapter._closed
        finally:
            await client.aclose()
    asyncio.run(run())


def test_cancel_preserves_safe_cleanup_failure_diagnostic(tmp_path):
    adapter, _, _, manifest = make_fixture(tmp_path)
    executor, coordinator = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    started, release = threading.Event(), threading.Event()
    submit = adapter.submit
    def delayed(request):
        started.set()
        assert release.wait(5)
        return submit(request)
    adapter.submit = delayed
    @contextmanager
    def factory():
        try: yield adapter
        finally: raise RuntimeError('private fixture detail')
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        report = BrokerCleanupReport()
        service = asyncio.create_task(serve_owned_inherited_broker(factory, fd=executor.detach(),
            peer=PeerAccount(uid=os.getuid(), gid=os.getgid()), stop_event=stop, cleanup_report=report,
            on_ready=ready.set, command_timeout_seconds=5))
        client = ContainerPilotClient.for_inherited_seqpacket(coordinator.detach(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), timeout_seconds=5)
        try:
            await asyncio.wait_for(ready.wait(), 3)
            call = asyncio.create_task(client.submit_command(command(manifest, 'deploy_container')))
            assert await asyncio.to_thread(started.wait, 2)
            service.cancel()
            release.set()
            assert (await call).state == 'succeeded'
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(service, 5)
            assert (report.state, report.code) == ('failed', 'inherited_broker_shutdown_unverified')
            assert adapter._closed
        finally:
            release.set()
            await client.aclose()
    asyncio.run(run())


def test_wrong_sender_account_cannot_request_drain(tmp_path):
    adapter, _, _, _ = make_fixture(tmp_path)
    executor, coordinator = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    @contextmanager
    def factory(): yield adapter
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_inherited_broker(factory, fd=executor.detach(),
            peer=PeerAccount(uid=os.getuid() + 1, gid=os.getgid()), stop_event=stop, cleanup_report=BrokerCleanupReport(),
            on_ready=ready.set, command_timeout_seconds=5))
        client = ContainerPilotClient.for_inherited_seqpacket(coordinator.detach(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), timeout_seconds=5)
        try:
            await asyncio.wait_for(ready.wait(), 3)
            with pytest.raises(ContainerPilotClientError): await client.drain_inherited_broker()
            with pytest.raises(BrokerTransportError): await asyncio.wait_for(service, 5)
            assert adapter._closed
        finally:
            await client.aclose()
    asyncio.run(run())


def test_failed_adoption_never_closes_reused_descriptor(tmp_path, monkeypatch):
    from hub_container_executor import broker_service
    adapter, _, _, _ = make_fixture(tmp_path)
    executor, coordinator = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    reused = []
    def fail_after_consuming(fd, *args, **kwargs):
        os.close(fd)
        replacement = os.open('/dev/null', os.O_RDONLY)
        assert replacement == fd
        reused.append(replacement)
        raise BrokerTransportError()
    monkeypatch.setattr(broker_service, 'InheritedCommandBroker', fail_after_consuming)
    @contextmanager
    def factory(): yield adapter
    async def run():
        with pytest.raises(BrokerTransportError):
            await serve_owned_inherited_broker(factory, fd=executor.detach(),
                peer=PeerAccount(uid=os.getuid(), gid=os.getgid()), stop_event=asyncio.Event(), cleanup_report=BrokerCleanupReport())
        assert reused and os.fstat(reused[0])
    try: asyncio.run(run())
    finally:
        for fd in reused: os.close(fd)
        coordinator.close()


def test_drain_confirmation_does_not_block_event_loop(tmp_path, monkeypatch):
    from hub_container_executor.broker_seqpacket import InheritedCommandBroker
    original = InheritedCommandBroker.finish_drain
    entered, release = threading.Event(), threading.Event()
    def delayed(broker):
        entered.set()
        assert release.wait(5)
        return original(broker)
    monkeypatch.setattr(InheritedCommandBroker, 'finish_drain', delayed)
    adapter, _, _, _ = make_fixture(tmp_path)
    executor, coordinator = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    @contextmanager
    def factory(): yield adapter
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_inherited_broker(factory, fd=executor.detach(),
            peer=PeerAccount(uid=os.getuid(), gid=os.getgid()), stop_event=stop, cleanup_report=BrokerCleanupReport(),
            on_ready=ready.set, command_timeout_seconds=5))
        client = ContainerPilotClient.for_inherited_seqpacket(coordinator.detach(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), timeout_seconds=5)
        try:
            await asyncio.wait_for(ready.wait(), 3)
            drain = asyncio.create_task(client.drain_inherited_broker())
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(.05)  # loop can run while confirmation is blocked
            assert not drain.done() and not service.done()
            release.set()
            await asyncio.wait_for(drain, 3)
            await asyncio.wait_for(service, 3)
        finally:
            release.set()
            await client.aclose()
    asyncio.run(run())


def test_stop_wins_before_validated_packet_admission(tmp_path, monkeypatch):
    from hub_container_executor.broker_seqpacket import InheritedCommandBroker
    original = InheritedCommandBroker._receive_value
    entered, release = threading.Event(), threading.Event()
    def delayed(broker, deadline):
        request = original(broker, deadline)
        entered.set()
        assert release.wait(5)
        return request
    monkeypatch.setattr(InheritedCommandBroker, '_receive_value', delayed)
    adapter, backend, _, manifest = make_fixture(tmp_path)
    executor, coordinator = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    @contextmanager
    def factory(): yield adapter
    async def run():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve_owned_inherited_broker(factory, fd=executor.detach(),
            peer=PeerAccount(uid=os.getuid(), gid=os.getgid()), stop_event=stop, cleanup_report=BrokerCleanupReport(),
            on_ready=ready.set, command_timeout_seconds=5))
        client = ContainerPilotClient.for_inherited_seqpacket(coordinator.detach(),
            peer_uid=os.getuid(), peer_gid=os.getgid(), timeout_seconds=5)
        try:
            await asyncio.wait_for(ready.wait(), 3)
            call = asyncio.create_task(client.submit_command(command(manifest, 'deploy_container')))
            assert await asyncio.to_thread(entered.wait, 2)
            stop.set()
            release.set()
            with pytest.raises(UnknownOutcome): await asyncio.wait_for(call, 5)
            await asyncio.wait_for(service, 5)
            assert 'create' not in backend.events and adapter._closed
        finally:
            release.set()
            await client.aclose()
    asyncio.run(run())


def test_changed_owner_pid_never_reports_cleanup_complete(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hub_container_executor import broker_service
    from hub_container_executor.broker_seqpacket import InheritedCommandBroker
    adapter, _, _, _ = make_fixture(tmp_path)
    executor, coordinator = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    actual_pid, actual_close = os.getpid(), os.close
    pid = [actual_pid]
    brokers = []
    monkeypatch.setattr(broker_service, 'os', SimpleNamespace(getpid=lambda: pid[0], close=actual_close))
    def make_broker(*args, **kwargs):
        broker = InheritedCommandBroker(*args, **kwargs)
        brokers.append(broker)
        return broker
    monkeypatch.setattr(broker_service, 'InheritedCommandBroker', make_broker)
    @contextmanager
    def factory(): yield adapter
    async def run():
        stop, ready, report = asyncio.Event(), asyncio.Event(), BrokerCleanupReport()
        fd = executor.detach()
        service = asyncio.create_task(serve_owned_inherited_broker(factory, fd=fd,
            peer=PeerAccount(uid=os.getuid(), gid=os.getgid()), stop_event=stop,
            cleanup_report=report, on_ready=ready.set, command_timeout_seconds=5))
        try:
            await asyncio.wait_for(ready.wait(), 3)
            pid[0] += 1
            stop.set()
            with pytest.raises(BrokerTransportError): await asyncio.wait_for(service, 5)
            assert (report.state, report.code) == ('failed', 'inherited_broker_shutdown_unverified')
        finally:
            # Synthetic PID change skips service cleanup; this test still owns
            # the real descriptors and adapter in its unchanged actual process.
            if brokers: brokers[0]._socket.close()
            else: actual_close(fd)
            coordinator.close()
            adapter.close()
    asyncio.run(run())
