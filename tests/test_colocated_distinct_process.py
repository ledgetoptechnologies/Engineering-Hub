"""Synthetic distinct-process relay; does not attest an Engine or a container."""
import asyncio
import multiprocessing
import os
import socket
import threading

import pytest

pytest.importorskip('fcntl')

from engineering_hub.current_executor_observation import CurrentExecutorObserver
from hub_container_executor.asgi import create_app
from hub_container_executor.coordinator_observation_transport import CoordinatorObservationTransport
from hub_container_executor.launch_supervisor import _Process
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from hub_container_executor.runtime_identity import RuntimeIdentityError
from tests.test_hub_container_executor_launch_supervisor import (
    short_root, root_journal, synthetic_issuer, current_pins, current_ready,
)
from tests.test_hub_current_observation_end_to_end import owned_supervisor_wait, request


def _executor(issuer_path, relay_path, connection):
    """A separate executor process, with a real ASGI app and issuer transport."""
    class Adapter:
        _single_pilot_protocol = True

    relay = UnixCurrentObservationTransport(socket_path=issuer_path,
        expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    app = create_app(Adapter(), bearer_token='synthetic-token', current_observation_transport=relay)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(relay_path)
        os.chmod(relay_path, 0o600)
        server.listen(2)
        server.settimeout(5)
        connection.send(('ready', os.getpid()))
        for _ in range(2):
            peer, _ = server.accept()
            with peer:
                peer.settimeout(3)
                raw = b''
                while b'\r\n\r\n' not in raw:
                    piece = peer.recv(4096)
                    if not piece or len(raw) > 8192:
                        raise ValueError('invalid request')
                    raw += piece
                head, body = raw.split(b'\r\n\r\n', 1)
                lengths = [int(line.split(b':', 1)[1]) for line in head.split(b'\r\n')
                           if line.lower().startswith(b'content-length:')]
                length = lengths[0] if lengths else 0
                while len(body) < length:
                    piece = peer.recv(4096)
                    if not piece:
                        raise ValueError('short request')
                    body += piece
                with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
                    start, end = runner.run(request(app, head, body))
                peer.sendall(b'HTTP/1.1 ' + str(start['status']).encode() + b' OK\r\n'
                    + b'\r\n'.join(k + b': ' + v for k, v in start['headers'])
                    + b'\r\n\r\n' + end['body'])
        connection.send(('done', os.getpid()))
        if not connection.poll(5) or connection.recv() != 'stop':
            raise RuntimeError('coordinator did not release executor')
    finally:
        server.close()
        relay.close()
        connection.close()


def test_distinct_executor_peer_v2_and_signed_observation(synthetic_issuer, short_root):
    from engineering_hub.container_pilot_client import ContainerPilotClient

    authority, issuer_path, checks = synthetic_issuer
    relay_path = str(short_root / 'relay.sock')
    context = multiprocessing.get_context('fork')
    parent, child = context.Pipe()
    process = context.Process(target=_executor, args=(str(issuer_path), relay_path, child))
    process.start()
    child.close()
    try:
        assert parent.poll(5)
        assert parent.recv() == ('ready', process.pid)
        assert process.pid != os.getpid()
        authority._consumer.close()
        authority._consumer = _Process(process.pid)
        current_ready(authority)
        release, launch = current_pins(authority)
        # The parent has the same UID/GID as the child but is the wrong PID.
        # Capture the server's actual rejection, not just a client exception.
        results = []
        rejected = threading.Thread(target=lambda: results.append(
            authority.serve_one_current_observation(timeout_ms=1000)))
        rejected.start()
        wrong_peer = UnixCurrentObservationTransport(socket_path=str(issuer_path),
            expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
        try:
            with pytest.raises(RuntimeIdentityError):
                wrong_peer.observe_current_launch(nonce='a' * 64, timeout_ms=1000)
        finally:
            wrong_peer.close()
            rejected.join(2)
        assert not rejected.is_alive() and results == [False] and checks == []
        transport = CoordinatorObservationTransport(socket_path=relay_path, credential='synthetic-token')
        observer = CurrentExecutorObserver(release=release, launch=launch, transport=transport)
        try:
            async def capability():
                client = ContainerPilotClient.for_private_unix(relay_path, 'synthetic-token',
                    single_pilot_protocol=True)
                try:
                    await client._require_single_pilot_capability('synthetic')
                finally:
                    await client.aclose()
            with owned_supervisor_wait(authority):
                asyncio.run(capability())
                proof = observer.observe_verified()
                assert proof.identity.generation_id == launch.launch_id
                assert launch.authenticated_host_pid == process.pid
                assert parent.poll(5) and parent.recv() == ('done', process.pid)
            parent.send('stop')
        finally:
            observer.close()
            transport.close()
    finally:
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
    assert process.exitcode == 0
