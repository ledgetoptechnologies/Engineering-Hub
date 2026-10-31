"""Native queue -> real HostAdapter/backend -> broker -> synthetic Engine.

No Docker socket, real image, host authority or staging resources are used.
"""
import asyncio
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import sqlite3
import sys
import threading
from urllib.parse import quote
import uuid

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.backend import BaselineIdentity, ContainerPlan
from hub_container_executor.build_record import verify_image_record
from hub_container_executor.daemon_backend import DockerEngineBackend, _PROBE_SCRIPT
from hub_container_executor.runtime_profile import derive_runtime_profile
from hub_container_executor.state_provider import FrozenSQLiteStateProvisioner
from scripts.hub_local_native_binding import LocalNativeBinding
from scripts.hub_local_test_engine_broker import EffectJournal, LocalTestBroker, LocalTestSocketServer, UnixHTTPConnection
from scripts.hub_local_test_volume import OwnedVolume
from tests.manual_session_fixtures import synthetic_workflow_reconciliation
from tests.test_hub_container_executor_adapter import FakeBaselineProvider, FakeDriverIdentityVerifier
from tests.test_hub_container_executor_build_record import FakeStore, FakeTrust, make_record
from tests.test_hub_container_executor_daemon_backend import binding, container_inspect, docker_config, oci_archive
from tests.test_hub_container_pilot import PRINCIPAL
from tests.test_hub_container_pilot_manifest import make_manifest_data
from tests.test_hub_queued_pilot import ACTION, MACHINE, DistinctContainerClient


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


@pytest.fixture
def owned_resources():
    with ExitStack() as owned:
        yield owned


@pytest.mark.skipif(sys.platform != 'linux' or os.getuid() != 10001 or os.getgid() != 10001,
                    reason='isolated Linux app UID/GID 10001 required')
@pytest.mark.asyncio
async def test_native_queue_real_adapter_and_broker_with_synthetic_engine_failure_rollback(tmp_path, monkeypatch, owned_resources):
    """Synthetic Engine responses prove wiring, not actual Engine acceptance."""
    task_owner = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    volume_name = 'hub-local-test-' + task_owner
    fake_root = tmp_path / 'fake-engine-root'
    volume_root = fake_root / 'volumes' / volume_name / '_data'
    generations = volume_root / 'generations'; generations.mkdir(parents=True, mode=0o700)
    reservations = volume_root / 'reservations'; reservations.mkdir(mode=0o700)
    baseline_dir = generations / 'baseline-g1'; baseline_dir.mkdir(mode=0o700)
    baseline_db = baseline_dir / 'hub.sqlite3'
    with sqlite3.connect(baseline_db) as db:
        db.execute('CREATE TABLE synthetic_fixture(v TEXT)'); db.execute("INSERT INTO synthetic_fixture VALUES('baseline')")
    baseline_db.chmod(0o600)
    frozen = generations / 'frozen.sqlite3'; frozen.write_bytes(baseline_db.read_bytes()); frozen.chmod(0o600)
    candidate_manifest, candidate_raw, candidate_blobs = make_record('candidate')
    rollback_manifest, rollback_raw, rollback_blobs = make_record('rollback')
    data = make_manifest_data()
    data['runtime']['resource_name_prefix'] = 'hub-pilot-' + uuid.UUID(task_owner).hex[:24]
    data['runtime']['resources']['cpu_millicores'] = 300
    data['runtime']['resources']['pids_limit'] = 80
    data['state_policy']['baseline_backup_digest'] = digest(frozen.read_bytes())
    data['retention']['max_generations'] = 4
    for role, raw in (('candidate', candidate_raw), ('rollback', rollback_raw)):
        record = json.loads(raw)
        data[role]['image_config_id'] = record['selection']['image_config_id']
        data[role]['provenance']['attestation_digest'] = digest(raw)
    manifest = PilotManifest.from_bytes(json.dumps(data).encode())
    records = FakeStore({digest(candidate_raw): candidate_raw, digest(rollback_raw): rollback_raw})
    blobs = {**candidate_blobs, **rollback_blobs}; metadata = FakeStore(blobs); trust = FakeTrust()
    candidate = verify_image_record(manifest, 'candidate', records, metadata, trust)
    rollback = verify_image_record(manifest, 'rollback', records, metadata, trust)
    images = {'candidate': candidate, 'rollback': rollback}
    archives = {'candidate': oci_archive(candidate, candidate_blobs), 'rollback': oci_archive(rollback, rollback_blobs)}
    secrets = {'CODEX_TOKEN': 'synthetic-codex', 'HERMES_TOKEN': 'synthetic-hermes', 'CLAUDE_TOKEN': 'synthetic-claude'}
    class Secrets:
        def resolve_test_secret(self, slot, _version):
            return secrets[slot.removeprefix('test-').upper() + '_TOKEN']
    private = tmp_path / 'private'; private.mkdir(mode=0o700)
    owner_path = private / 'hub.sqlite3'
    owner = PilotOwner(owner_path, allow_ephemeral_test_storage=True)
    resources = owned_resources
    resources.callback(owner.close)
    repo = HubRepository(owner_path, execution_principal=PRINCIPAL, pilot_owner=owner)
    state = {'calls': [], 'errors': [], 'responses': [], 'adapter': None, 'server': None, 'server_thread': None, 'broker': None}

    class Client(DistinctContainerClient):
        async def submit_command(self, command):
            try:
                if command.action == 'deploy_container':
                    bound = native_binding.bind_first_deploy(command)
                    state['bound'] = bound
                    await asyncio.to_thread(prepare, bound)
                state['calls'].append(command.action)
                response = await asyncio.to_thread(state['adapter'].submit, command)
                state['responses'].append((response.state, response.reason,
                                           response.evidence.container_id, response.evidence.health))
                return response
            except Exception as error:
                state['errors'].append((type(error).__name__, str(error)))
                raise
    client = Client()
    workflow = ContainerPilotWorkflow(repo, client, [ContainerPilotTarget.from_manifest(manifest)], pilot_owner=owner)
    queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project='engineering-hub')})
    bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
                                  reconciliation=synthetic_workflow_reconciliation(workflow))
    native_binding = LocalNativeBinding(bridge=bridge, manifest=manifest, owner=task_owner)

    def prepare(bound):
        profiles = {
            'baseline': derive_runtime_profile(manifest, HostAdapter._defaults_from_record(rollback), 'baseline-g1', secrets),
            'candidate': derive_runtime_profile(manifest, HostAdapter._defaults_from_record(candidate), bound.candidate_generation, secrets),
            'rollback': derive_runtime_profile(manifest, HostAdapter._defaults_from_record(rollback), bound.rollback_generation, secrets),
        }
        names = {'baseline': bound.baseline_name, 'candidate': bound.candidate_name, 'rollback': bound.rollback_name}
        by_name = {names[role]: role for role in names}
        by_ref = {candidate.runnable_reference: 'candidate', rollback.runnable_reference: 'rollback'}
        backend_stub = object.__new__(DockerEngineBackend); backend_stub._gate_mount_verifier = None
        bodies = {}
        for role in names:
            image = candidate if role == 'candidate' else rollback
            profile = profiles[role]
            plan = ContainerPlan(names[role], image, image.runnable_reference, False, profile)
            bodies[names[role]] = backend_stub._create_body(plan, generations / profile.state_generation_id, profile.environment)
        probes = []
        for role in ('candidate', 'rollback'):
            image = images[role]
            for probe in getattr(manifest.probes, role):
                probes.append({'AttachStdin': False, 'AttachStdout': True, 'AttachStderr': False, 'Tty': False,
                    'User': '10001:10001', 'Cmd': ['/usr/local/bin/python3', '-I', '-c', _PROBE_SCRIPT,
                    probe.kind, probe.path, str(probe.expected_status), probe.expectation_id,
                    str(probe.max_response_bytes), str(probe.timeout_ms), image.source_commit]})
        journal = EffectJournal(private / 'broker.sqlite'); resources.callback(journal.close)
        volume = OwnedVolume(volume_name, task_owner, 'fixture-daemon', str(volume_root))
        broker = LocalTestBroker(socket_path='/var/run/docker.sock', socket_identity=(1, 2), version='1.41',
            owner=task_owner, volume=volume, images={image.runnable_reference:
                (image.image_config_id, image.platform_manifest_digest) for image in images.values()},
            creates=bodies, exec_bodies=probes, journal=journal)
        containers, execs = {}, {}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def reply(self, status, value, binary=False):
                raw = value if binary else json.dumps(value).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                path = self.path
                if path.endswith('/info'):
                    return self.reply(200, {'ID': 'fixture-daemon', 'OSType': 'linux', 'Architecture': 'amd64',
                                            'DockerRootDir': str(fake_root)})
                if '/volumes/' in path:
                    return self.reply(200, {'Name': volume.name, 'Driver': 'local', 'Scope': 'local',
                        'Mountpoint': volume.mountpoint, 'Labels': {'engineering-hub.local-test-owner': task_owner}, 'Options': {}})
                for ref, role in by_ref.items():
                    base = '/v1.41/images/' + quote(ref, safe='')
                    if path == base + '/json': return self.reply(200, docker_config(images[role]))
                    if path == base + '/get': return self.reply(200, archives[role], True)
                for name in names.values():
                    if path == '/v1.41/containers/' + name + '/json': return self.reply(404, {})
                for ident, item in containers.items():
                    if path == '/v1.41/containers/' + ident + '/json':
                        role = item['role']; profile = profiles[role]; image = candidate if role == 'candidate' else rollback
                        value = container_inspect(image, profile, generations / profile.state_generation_id,
                                                  status=item['status'], container_id=ident)
                        value['Config']['Labels'] = {'engineering-hub.local-test-owner': task_owner}
                        value['HostConfig']['PublishAllPorts'] = False
                        value['HostConfig']['Mounts'][0].pop('ReadOnly')
                        value['Mounts'] = value['Mounts'][:1]; value['Mounts'][0]['Mode'] = ''
                        value['State'].update(Running=item['status'] == 'running', Pid=123 if item['status'] == 'running' else 0,
                            StartedAt='2026-10-21T00:00:00Z' if item['status'] == 'running' else '0001-01-01T00:00:00Z')
                        return self.reply(200, value)
                for ident, item in execs.items():
                    if path == '/v1.41/exec/' + ident + '/json':
                        return self.reply(200, {'ID': ident, 'Running': False, 'ExitCode': 1 if item['failed'] else 0})
                self.reply(404, {})
            def do_POST(self):
                path = self.path
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length)) if length else None
                if path.startswith('/v1.41/containers/create?name='):
                    name = path.split('=', 1)[1]; role = by_name[name]
                    assert body['Labels'] == {'engineering-hub.local-test-owner': task_owner}
                    ident = {'baseline': 'e', 'candidate': 'c', 'rollback': 'd'}[role] * 64
                    containers[ident] = {'role': role, 'status': 'created'}
                    return self.reply(201, {'Id': ident, 'Warnings': []})
                for ident, item in list(containers.items()):
                    if path == '/v1.41/containers/' + ident + '/start':
                        item['status'] = 'running'; self.send_response(204); self.end_headers(); return
                    if path == '/v1.41/containers/' + ident + '/stop?t=5':
                        item['status'] = 'exited'; self.send_response(204); self.end_headers(); return
                    if path == '/v1.41/containers/' + ident + '/exec':
                        cmd = body['Cmd']; ident_exec = uuid.uuid4().hex + uuid.uuid4().hex
                        failed = item['role'] == 'candidate' and cmd[4] == 'health_json'
                        execs[ident_exec] = {'failed': failed, 'expectation': cmd[7],
                                             'status': 503 if failed else int(cmd[6])}
                        return self.reply(201, {'Id': ident_exec})
                for ident, item in execs.items():
                    if path == '/v1.41/exec/' + ident + '/start':
                        raw = json.dumps({'expectation_id': item['expectation'], 'ok': not item['failed'],
                                          'status': item['status']}).encode()
                        return self.reply(200, b'\x01\x00\x00\x00' + len(raw).to_bytes(4, 'big') + raw, True)
                self.reply(404, {})
            def do_DELETE(self):
                ident = self.path.split('/containers/', 1)[1].split('?', 1)[0]
                if ident not in containers or containers[ident]['status'] != 'exited': return self.reply(409, {})
                del containers[ident]; self.send_response(204); self.end_headers()
        upstream_path = str(private / 'synthetic-engine.sock')
        upstream = socketserver.UnixStreamServer(upstream_path, Handler)
        upstream_thread = threading.Thread(target=upstream.serve_forever); upstream_thread.start()
        def close_upstream():
            upstream.shutdown(); upstream_thread.join(timeout=5); upstream.server_close()
            assert not upstream_thread.is_alive()
        resources.callback(close_upstream)
        broker._connection = lambda timeout, total: UnixHTTPConnection(upstream_path, timeout, total)
        socket_path = str(private / 'broker.sock')
        server = LocalTestSocketServer(socket_path, broker)
        worker = threading.Thread(target=server.serve_forever); worker.start()
        def close_broker_server():
            server.shutdown(); worker.join(timeout=5); server.server_close()
            assert not worker.is_alive()
        resources.callback(close_broker_server)
        backend = DockerEngineBackend(binding(manifest, tmp_path, api_version='1.41').__class__(
            manifest.runner_binding.runner_id, manifest.runner_binding.daemon_identity, manifest.runner_binding.platform,
            socket_path, '1.41', str(generations)))
        resources.callback(backend.close)
        baseline_profile = profiles['baseline']
        baseline_plan = ContainerPlan(names['baseline'], rollback, rollback.runnable_reference, False, baseline_profile)
        baseline_id = backend.create_container(baseline_plan, str(uuid.uuid4()), 10000)
        backend.start_container(baseline_id, str(uuid.uuid4()), 10000)
        baseline = BaselineIdentity(manifest.state_policy.baseline_id, manifest.state_policy.baseline_backup_digest,
                                    baseline_id, rollback.image_config_id, 'baseline-g1', True, 'synthetic-proof')
        baseline_provider = FakeBaselineProvider(baseline)
        inventory = (('hub.sqlite3', baseline_db.stat().st_size, digest(baseline_db.read_bytes())),)
        provider = FrozenSQLiteStateProvisioner(manifest, baseline, baseline_provider=baseline_provider,
            frozen_backup=frozen, generation_root=generations, reservation_root=reservations,
            baseline_generation_inventory=inventory, create=True, allow_ephemeral_test_storage=True)
        resources.callback(provider.close)
        adapter = HostAdapter(private / 'adapter.sqlite3', manifest, record_store=records, metadata_store=metadata,
            provenance_verifier=trust, backend=backend, baseline_provider=baseline_provider,
            state_provisioner=provider, secret_resolver=Secrets(),
            driver_identity_verifier=FakeDriverIdentityVerifier(manifest.driver.implementation_commit))
        resources.callback(adapter.close)
        state.update(adapter=adapter, server=server, server_thread=worker, broker=broker)

    task = repo.create_task('human:1234', 'engineering-hub', 'Synthetic local native broker', 'native-broker')
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], manifest.candidate.source_commit)
    task = repo.review_task('claude', task['id'], task['revision'], manifest.candidate.source_commit, 'approve')
    job = queue.submit(actor='codex', request_id='native-broker', action_key=ACTION, environment='staging',
        target=WorkTarget(task['id'], manifest.candidate.source_commit, manifest.candidate.image_config_id))
    feed = NativeHubEventFeed(owner_path); runtime = QueuedPilotRuntime(bridge, feed)
    try:
        assert (await feed.start())['available']; await runtime.start()
        async with asyncio.timeout(30):
            while runtime.status['last_job'] is None:
                await asyncio.sleep(.01)
        assert runtime.status['last_job'] == {'id': job['id'], 'state': 'failed'}, (state['errors'], state['responses'])
        assert repo.list_runs()[0]['status'] == 'rolled_back'
        assert state['calls'] == ['deploy_container', 'smoke_container', 'rollback_container', 'smoke_container_rollback']
        assert [row[0] for row in state['responses']] == ['succeeded', 'failed', 'succeeded', 'succeeded']
        assert [row[2] for row in state['responses']] == ['c' * 64, 'c' * 64, 'd' * 64, 'd' * 64]
        assert state['responses'][1][3] == 'unhealthy' and state['responses'][3][3] == 'healthy'
        with repo._connection() as db:
            assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', (MACHINE,)).fetchone()[0] is None
        assert baseline_db.read_bytes() == frozen.read_bytes()
        for generation in (state['bound'].candidate_generation, state['bound'].rollback_generation):
            copy = generations / generation / 'hub.sqlite3'
            with sqlite3.connect(copy.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
                assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
                assert db.execute('SELECT v FROM synthetic_fixture').fetchall() == [('baseline',)]
    finally:
        try: await runtime.stop()
        finally:
            try: await feed.stop()
            finally: resources.close()
