"""Real signed UDS observation with synthetic issuer facts; no host acceptance."""
from dataclasses import replace
import asyncio
from contextlib import contextmanager
import os
import socket
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip('fcntl')
from engineering_hub.container_pilot import ContainerPilotTarget
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.app import Settings, create_app
from engineering_hub.container_pilot import ContainerPilotWorkflow
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubRepository
from engineering_hub.work_queue import WorkQueue, WorkAction
from agent_message_bus.auth import AuthSettings
from tests.test_hub_queued_pilot import DistinctContainerClient, ACTION, MACHINE
from engineering_hub.trusted_queued_session import parse_enrollment
from hub_container_executor.colocated_session import ColocatedTrustedSession
from tests.test_hub_container_executor_installed_coordinator import configuration, canonical
from hub_container_executor.installed_coordinator import parse_configuration, SCHEMA_SINGLE_PILOT
from tests.test_hub_trusted_queued_session import document
from tests.test_hub_unified_pilot_startup import encode
from tests.test_hub_current_observation_end_to_end import owned_supervisor_wait
from tests.test_hub_current_observation_end_to_end import request, NoCommands
from hub_container_executor.asgi import create_app as executor_app
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from hub_container_executor.coordinator_observation_transport import CoordinatorObservationTransport
from engineering_hub.current_executor_transport import CurrentExecutorTransportError
from tests.test_hub_container_executor_launch_supervisor import (
    short_root, root_journal, synthetic_issuer, current_ready, current_pins,
)


@contextmanager
def relay_server(path, app, replay=False):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); server.bind(str(path)); os.chmod(path, 0o600); server.listen(2)
    errors = []
    def run():
        try:
            saved = None
            for attempt in range(2 if replay else 1):
                conn, _ = server.accept()
                with conn:
                    data = b''
                    while b'\r\n\r\n' not in data: data += conn.recv(4096)
                    head, body = data.split(b'\r\n\r\n', 1)
                    length = int(next(line.split(b':', 1)[1] for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:')))
                    while len(body) < length: body += conn.recv(4096)
                    if attempt == 0:
                        with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
                            start, end = runner.run(request(app, head, body))
                        saved = b'HTTP/1.1 ' + str(start['status']).encode() + b' OK\r\n' + b'\r\n'.join(k+b': '+v for k,v in start['headers']) + b'\r\n\r\n' + end['body']
                    conn.sendall(saved)
        except BaseException as error:
            errors.append(error)
    thread=threading.Thread(target=run); thread.start()
    try: yield
    finally:
        thread.join(5); server.close(); assert not thread.is_alive() and not errors


@pytest.mark.parametrize('scenario', ['accept', 'wrong_key', 'replay'])
def test_signed_executor_relay_requires_matching_key_and_manual_acceptance(synthetic_issuer, monkeypatch, scenario):
    import engineering_hub.pilot_ownership as ownership
    old = ownership._local_filesystem
    monkeypatch.setattr(ownership, '_local_filesystem', lambda fd, **_: old(fd))
    authority, issuer_path, _ = synthetic_issuer
    raw = configuration(); raw['schema'] = SCHEMA_SINGLE_PILOT; raw['single_pilot_protocol'] = True
    database_root = issuer_path.parent / 'database'; database_root.mkdir(mode=0o700)
    raw['database_path'] = str(database_root / 'hub.sqlite3')
    socket_root = issuer_path.parent / 'executor'; socket_root.mkdir(mode=0o700)
    raw['executor_socket_path'] = str(socket_root / 'host.sock')
    config = parse_configuration(canonical(raw))
    authority.release = replace(authority.release, implementation_commit=config.manifest.driver.implementation_commit,
                                platform=config.manifest.runner_binding.platform)
    authority._daemon_pin = replace(authority._daemon_pin, daemon_id=config.manifest.runner_binding.daemon_identity)
    current_ready(authority)
    release, launch = current_pins(authority)
    data = document()
    data['release'] = {name: getattr(release, name) for name in release.__dataclass_fields__
                       if name not in {'public_key', 'platform'}}
    data['release'].update(public_key_hex=release.public_key.hex(), platform=release.platform.model_dump(mode='json'))
    if scenario == 'wrong_key':
        data['release']['public_key_hex'] = 'a' * 64
    data['launch'] = {name: getattr(launch, name) for name in launch.__dataclass_fields__}
    data['report']['executor'] = dict(data['launch'], image_config_id=release.image_config_id)
    data['report']['manifest_digest'] = config.manifest.digest()
    data['report']['expires_at_ms'] = launch.approval_expires_at_ms
    enrollment = parse_enrollment(encode(data))
    token = 'synthetic-private-executor-token'
    issuer_relay = UnixCurrentObservationTransport(socket_path=str(issuer_path),
        expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    app = executor_app(NoCommands(), bearer_token=token, current_observation_transport=issuer_relay)
    composition = ColocatedTrustedSession(config=config, enrollment=enrollment, credential=token)
    try:
        with owned_supervisor_wait(authority), relay_server(raw['executor_socket_path'], app, replay=scenario == 'replay'), PilotOwner(config.database, allow_ephemeral_test_storage=True) as owner:
            target = ContainerPilotTarget.from_manifest(config.manifest)
            gate = composition.reconciliation_factory(owner, target)
            assert gate.admission_state == 'pending'
            if scenario in ('wrong_key', 'replay'):
                from engineering_hub.current_executor_observation import CurrentExecutorObservationError
                if scenario == 'replay':
                    composition._current_verified()
                with pytest.raises(CurrentExecutorObservationError):
                    composition._current_verified()
                assert gate.admission_state != 'accepted'
                return
            repo = HubRepository(config.database, execution_principal='executor:container-pilot', pilot_owner=owner)
            client = DistinctContainerClient()
            workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
            queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project='engineering-hub')})
            bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE, reconciliation=gate)
            feed = NativeHubEventFeed(config.database); runtime = QueuedPilotRuntime(bridge, feed)
            core = create_app(Settings(config.database, AuthSettings('h','c',claude_token='cl'), config.auth,
                staging_machine_id=MACHINE), repository=repo, event_feed=feed, work_queue=queue, queued_pilot_runtime=runtime)
            broker = composition.session_acceptance_factory(core, owner, repo, target, gate)
            snapshot = broker.snapshot(actor=data['operator_actor'])
            async def accept():
                return await broker.decide_async(actor=data['operator_actor'], request_id=snapshot['request_id'],
                    expected_revision=snapshot['revision'], action='accept')
            with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner: runner.run(accept())
            assert gate.admission_state == 'accepted' and client.submissions == []
    finally:
        composition.close()
        issuer_relay.close()


def test_missing_or_invalid_enrollment_never_starts_service(monkeypatch):
    from hub_container_executor import installed_coordinator as module
    config = parse_configuration(canonical(configuration()))
    monkeypatch.setattr(module, 'load_configuration', lambda _: config)
    assert module.main(['serve', '--config', '/unused']) == 2


def test_wrong_bearer_is_rejected_before_observation(short_root):
    calls = []
    class ForbiddenObserver:
        def observe_current_launch(self, **_):
            calls.append(True)
            raise AssertionError('unauthenticated observation reached issuer')
    path = short_root / 'executor.sock'
    app = executor_app(NoCommands(), bearer_token='expected-token', current_observation_transport=ForbiddenObserver())
    transport = CoordinatorObservationTransport(socket_path=str(path), credential='wrong-token')
    try:
        with relay_server(path, app):
            with pytest.raises(CurrentExecutorTransportError):
                transport.observe_current_launch(nonce='a' * 64, timeout_ms=1000)
    finally:
        transport.close()
    assert calls == []


def test_readonly_requirement_is_checked_on_open_file(tmp_path, monkeypatch):
    from engineering_hub import unified_pilot_startup as startup
    parent = tmp_path / 'operator'; parent.mkdir(mode=0o700)
    path = parent / 'session.json'; path.write_bytes(b'{}'); path.chmod(0o600)
    calls = []
    def writable(fd):
        calls.append(os.fstat(fd).st_ino)
        return SimpleNamespace(f_flag=0)
    monkeypatch.setattr(startup.os, 'fstatvfs', writable)
    with pytest.raises(startup.UnifiedPilotStartupError):
        startup.read_protected_file(path, maximum_bytes=100, require_readonly=True)
    assert calls == [path.stat().st_ino]


def test_cli_supplies_both_owned_factories_and_v2(monkeypatch):
    from hub_container_executor import installed_coordinator as module
    from engineering_hub import unified_pilot_startup
    raw = configuration(); raw['schema'] = SCHEMA_SINGLE_PILOT; raw['single_pilot_protocol'] = True
    config = parse_configuration(canonical(raw)); data = document()
    data['report']['manifest_digest'] = config.manifest.digest()
    data['release']['implementation_commit'] = config.manifest.driver.implementation_commit
    data['release']['platform'] = config.manifest.runner_binding.platform.model_dump(mode='json')
    data['launch']['engine_id'] = config.manifest.runner_binding.daemon_identity
    data['report']['executor'] = dict(data['launch'], image_config_id=data['release']['image_config_id'])
    monkeypatch.setattr(module, 'load_configuration', lambda _: config)
    monkeypatch.setattr(module.os, 'statvfs', lambda _: SimpleNamespace(f_flag=os.ST_RDONLY))
    monkeypatch.setattr(unified_pilot_startup, 'read_protected_file', lambda *a, **kw: encode(data))
    calls = []
    async def serve(received, **kwargs):
        assert received is config and received.single_pilot_protocol is True
        assert kwargs['session_enrollment'].manifest_digest == config.manifest.digest()
        calls.append(True)
    monkeypatch.setattr(module, 'run_installed_coordinator', serve)
    assert module.main(['serve','--config','/unused','--session-config','/approved/session.json']) == 0
    assert calls == [True]


def test_owned_coordinator_composes_v2_with_same_socket_and_bearer(monkeypatch):
    from hub_container_executor import installed_coordinator as module
    raw = configuration(); raw['schema'] = SCHEMA_SINGLE_PILOT; raw['single_pilot_protocol'] = True
    config = parse_configuration(canonical(raw)); data = document()
    data['report']['manifest_digest'] = config.manifest.digest()
    data['release']['implementation_commit'] = config.manifest.driver.implementation_commit
    data['release']['platform'] = config.manifest.runner_binding.platform.model_dump(mode='json')
    data['launch']['engine_id'] = config.manifest.runner_binding.daemon_identity
    data['report']['executor'] = dict(data['launch'], image_config_id=data['release']['image_config_id'])
    enrollment = parse_enrollment(encode(data)); monkeypatch.setattr(module, '_process', lambda: None)
    class Secrets:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def resolve_test_secret(self, slot, _version): return 'fixture-' + slot
    class Client:
        @staticmethod
        def for_private_unix(path, token, **kwargs):
            assert path == config.executor_socket and token == 'fixture-test-executor'
            assert kwargs == {'single_pilot_protocol': True}
            return object()
    monkeypatch.setattr(module, 'ImmutableTestSecretResolver', Secrets)
    monkeypatch.setattr(module, 'ContainerPilotClient', Client)
    calls=[]
    async def serve(_settings, _manifest, factory, **kwargs):
        composition = kwargs['reconciliation_factory'].__self__
        assert kwargs['single_pilot_protocol'] is True
        assert kwargs['session_acceptance_factory'].__self__ is composition
        assert str(composition._transport._path) == config.executor_socket
        assert composition._transport._credential == b'fixture-test-executor'
        factory(); calls.append(composition)
    monkeypatch.setattr(module, 'serve_owned_queued_pilot', serve)
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        runner.run(module.run_installed_coordinator(config, session_enrollment=enrollment))
    assert len(calls) == 1 and calls[0]._closed
