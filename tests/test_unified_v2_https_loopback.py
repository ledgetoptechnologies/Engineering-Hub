"""Loopback TLS and synthetic ASGI executor; no proxy, Engine or live service."""
import asyncio
import socket
from uuid import uuid4

import pytest
import uvicorn

from engineering_hub.container_pilot_client import (
    ContainerPilotClient, ContainerPilotClientError, ContainerPilotRequest,
)
from hub_container_executor.asgi import create_app
from hub_container_executor.ledger import _make_response
from tests.test_hub_container_pilot import pilot_manifest
from tests.test_hub_unified_pilot_startup import certificate


@pytest.mark.asyncio
async def test_v2_https_real_tls_auth_and_modes(tmp_path):
    class Adapter:
        _single_pilot_protocol = True
        def __init__(self): self.calls = []
        def submit(self, request):
            self.calls.append(('submit', request.command_id))
            return _make_response(request)
        def acknowledge_single_pilot(self, request, digest):
            self.calls.append(('ack', request.command_id, digest))
            return _make_response(request)

    cert, key = certificate('localhost')
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert); key_path.write_bytes(key); key_path.chmod(0o600)
    adapter = Adapter(); app = create_app(adapter, bearer_token='synthetic-token')
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0)); listener.listen(16); listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', interface='asgi3',
        loop='asyncio', http='h11', ws='none', access_log=False, proxy_headers=False,
        log_config=None, ssl_certfile=str(cert_path), ssl_keyfile=str(key_path)))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    clients = []
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail('synthetic TLS listener stopped')
                await asyncio.sleep(0.01)
        def client(token, mode):
            value = ContainerPilotClient.for_verified_https(
                f'https://localhost:{port}', token, cert, single_pilot_protocol=mode)
            clients.append(value)
            return value
        manifest = pilot_manifest()
        command = ContainerPilotRequest(schema_version='container_pilot_v1', command_id=str(uuid4()),
            run_id=str(uuid4()), target_id='engineering-hub:staging', action='deploy_container',
            manifest_digest=manifest.digest(), candidate_commit=manifest.candidate.source_commit,
            policy_revision=manifest.policy_revision, fencing_generation=1)
        wrong = client('wrong-token', True)
        with pytest.raises(ContainerPilotClientError): await wrong.submit_command(command)
        wrong_host = ContainerPilotClient.for_verified_https(
            f'https://127.0.0.1:{port}', 'synthetic-token', cert, single_pilot_protocol=True)
        clients.append(wrong_host)
        with pytest.raises(ContainerPilotClientError): await wrong_host.submit_command(command)
        legacy = client('synthetic-token', False)
        with pytest.raises(ContainerPilotClientError): await legacy.submit_command(command)
        assert adapter.calls == []
        valid = client('synthetic-token', True)
        response = await valid.submit_command(command)
        assert response.command_id == command.command_id
        outcome_digest = 'sha256:' + 'a' * 64
        acknowledged = await valid.acknowledge_command(command, outcome_digest)
        assert acknowledged.command_id == command.command_id
        assert adapter.calls == [('submit', command.command_id), ('ack', command.command_id, outcome_digest)]
        adapter._single_pilot_protocol = False
        with pytest.raises(ContainerPilotClientError): await valid.submit_command(command)
        assert adapter.calls == [('submit', command.command_id), ('ack', command.command_id, outcome_digest)]
    finally:
        for value in clients: await value.aclose()
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
