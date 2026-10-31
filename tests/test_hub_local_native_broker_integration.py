"""Native queue -> real HostAdapter/backend -> broker -> synthetic Engine.

No Docker socket, real image, host authority or staging resources are used.
"""
import asyncio
import base64
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import socketserver
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import quote
import uuid

import pytest

from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.pilot_ownership import PilotOwner
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.backend import ContainerPlan
from hub_container_executor.baseline_receipt import ProtectedBaselineProvider
from hub_container_executor.baseline_snapshot import FrozenBaselineSnapshot
from hub_container_executor.build_record import verify_image_record
from hub_container_executor.daemon_backend import DockerEngineBackend, _PROBE_SCRIPT
from hub_container_executor.runtime_profile import derive_runtime_profile
from hub_container_executor.state_provider import FrozenSQLiteStateProvisioner
from scripts.hub_local_native_coordinator import LocalNativeCoordinator, MACHINE
from scripts.hub_local_acceptance_components import LocalSource, LocalSuccessor
from scripts.hub_local_test_engine_broker import EffectJournal, LocalTestBroker, LocalTestSocketServer, UnixHTTPConnection
from scripts.hub_local_test_volume import OwnedVolume
from scripts.hub_local_test_oci import prepare_fixture_image, verify_fixture_image
from tests.test_hub_container_executor_adapter import FakeDriverIdentityVerifier
from tests.test_hub_container_executor_build_record import FakeStore, FakeTrust, make_record
from tests.test_hub_container_executor_daemon_backend import binding, container_inspect, docker_config, oci_archive
from tests.test_hub_container_pilot_manifest import make_manifest_data


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
    candidate_manifest, candidate_raw, candidate_blobs = make_record('candidate')
    rollback_manifest, rollback_raw, rollback_blobs = make_record('rollback')
    data = make_manifest_data()
    data['runtime']['resource_name_prefix'] = 'hub-pilot-' + uuid.UUID(task_owner).hex[:24]
    data['runtime']['resources']['cpu_millicores'] = 300
    data['runtime']['resources']['pids_limit'] = 80
    data['retention']['max_generations'] = 4
    fixtures = {}
    for role, provisional, raw, metadata_blobs in (
            ('candidate', candidate_manifest, candidate_raw, candidate_blobs),
            ('rollback', rollback_manifest, rollback_raw, rollback_blobs)):
        selection = getattr(provisional, role)
        preliminary = verify_image_record(provisional, role, FakeStore({digest(raw): raw}),
                                          FakeStore(metadata_blobs), FakeTrust())
        fixture = prepare_fixture_image(io.BytesIO(oci_archive(preliminary, metadata_blobs)),
            tmp_path / ('oci-' + role), preliminary.platform_manifest_digest,
            source_commit=selection.source_commit,
            context_digest=data[role]['provenance']['build_context_digest'],
            recipe_digest=data[role]['provenance']['build_recipe_digest'],
            observations={'claim': 'synthetic offline integration'}, preparation_id=str(uuid.uuid4()))
        fixtures[role] = fixture
        data[role]['image_config_id'] = fixture.config_digest
        data[role]['provenance']['build_inputs_digest'] = fixture.inputs_digest
        data[role]['provenance']['attestation_digest'] = fixture.record_digest
    manifest = PilotManifest.from_bytes(json.dumps(data).encode())
    candidate, _, _, candidate_trust = verify_fixture_image(manifest, 'candidate', fixtures['candidate'])
    rollback, _, _, rollback_trust = verify_fixture_image(manifest, 'rollback', fixtures['rollback'])
    records = FakeStore({item.record_digest: item.record for item in fixtures.values()})
    blobs = {key: value for item in fixtures.values() for key, value in item.blobs.items()}
    metadata = FakeStore(blobs)
    class SyntheticTrust:
        def verify_build_record(self, raw, record, actual_manifest, role):
            verifier = candidate_trust if role == 'candidate' else rollback_trust
            return verifier.verify_build_record(raw, record, actual_manifest, role)
    trust = SyntheticTrust()
    images = {'candidate': candidate, 'rollback': rollback}
    archives = {role: oci_archive(image, dict(fixtures[role].blobs)) for role, image in images.items()}
    secrets = {'CODEX_TOKEN': 'synthetic-codex', 'HERMES_TOKEN': 'synthetic-hermes', 'CLAUDE_TOKEN': 'synthetic-claude'}
    class Secrets:
        def resolve_test_secret(self, slot, _version):
            return secrets[slot.removeprefix('test-').upper() + '_TOKEN']
    private = tmp_path / 'private'; private.mkdir(mode=0o700)
    owner_path = private / 'hub.sqlite3'
    resources = owned_resources
    state = {'calls': [], 'errors': [], 'responses': [], 'adapter': None, 'server': None, 'server_thread': None, 'broker': None}
    # One Engine inventory spans bootstrap and the native broker. Both brokers
    # address this same task-owned synthetic socket, never the Docker daemon.
    profiles, names, by_name, containers, execs = {}, {}, {}, {}, {}
    by_ref = {candidate.runnable_reference: 'candidate', rollback.runnable_reference: 'rollback'}
    volume = OwnedVolume(volume_name, task_owner, 'fixture-daemon', str(volume_root))

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
                        Paused=False, Restarting=False, Dead=False,
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
                    if item['role'] == 'baseline' and not baseline_db.exists():
                        db = sqlite3.connect(baseline_db)
                        try:
                            db.execute('CREATE TABLE synthetic_fixture(v TEXT)')
                            db.execute("INSERT INTO synthetic_fixture VALUES('baseline')")
                            db.commit()
                        finally:
                            db.close()
                        baseline_db.chmod(0o600)
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

    # Bootstrap has its own broker journal and a child-owned PilotOwner. The
    # owner is enrolled before the child and reopened only after it exits.
    initial_owner = PilotOwner(owner_path, allow_ephemeral_test_storage=True)
    initial_owner.close()
    prefix = manifest.runtime.resource_name_prefix
    names.update({'baseline': prefix + '-00000000-000-rollback',
                  'candidate': prefix + '-11111111-111-candidate',
                  'rollback': prefix + '-11111111-111-rollback'})
    profiles.update({
        'baseline': derive_runtime_profile(manifest, HostAdapter._defaults_from_record(rollback), 'baseline-g1', secrets),
        'candidate': derive_runtime_profile(manifest, HostAdapter._defaults_from_record(candidate), 'candidate-placeholder', secrets),
        'rollback': derive_runtime_profile(manifest, HostAdapter._defaults_from_record(rollback), 'rollback-placeholder', secrets),
    })
    by_name.update({names[role]: role for role in names})
    monkeypatch.setattr(LocalTestBroker, '_connection',
        lambda self, timeout, total: UnixHTTPConnection(upstream_path, timeout, total))
    source = LocalSource(manifest=manifest, candidate=candidate, rollback=rollback,
        volume=volume, private_dir=private, daemon_socket_identity=(1, 2))
    resources.callback(source.close)
    source_broker = source.broker
    source_path = str(private / 'bootstrap.sock')
    payload = {'manifest': manifest.canonical_bytes().decode(), 'owner_db': str(owner_path),
        'socket': source_path, 'generation_root': str(generations), 'journal': str(private / 'bootstrap-operations.sqlite'),
        'owner': task_owner, 'record': base64.b64encode(fixtures['rollback'].record).decode(),
        'blobs': {key: base64.b64encode(value).decode() for key, value in fixtures['rollback'].blobs.items()},
        'ids': [str(uuid.uuid4()) for _ in range(6)]}
    child_entry = Path(__file__).resolve().parents[1] / 'scripts/hub_local_baseline_child.py'
    child = subprocess.run([sys.executable, '-I', '-B', str(child_entry)], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=45, check=True,
                           env={'LANG':'C.UTF-8','PATH':'/usr/bin:/bin'})
    observed = json.loads(child.stdout)
    manifest = PilotManifest.from_bytes(observed['manifest'].encode())
    frozen = generations / 'frozen.sqlite3'
    saved = observed['snapshot']
    snapshot = FrozenBaselineSnapshot(saved['sha256'], saved['size_bytes'],
        tuple(tuple(item) for item in saved['source_inventory']),
        tuple(tuple(item) for item in saved['source_identities']))
    receipt = base64.b64decode(observed['receipt'])
    baseline_provider = ProtectedBaselineProvider(FakeStore({observed['receipt_digest']: receipt}),
        receipt_digest=observed['receipt_digest'], campaign_id=manifest.campaign_id, verified_rollback=rollback,
        expected_runtime_profile_digest=observed['profile_digest'])
    resources.callback(baseline_provider.close)
    source_snapshot = source.seal()
    successor = LocalSuccessor(manifest=manifest, candidate=candidate, rollback=rollback,
        records=records, metadata=metadata, verifier=trust, baseline_provider=baseline_provider,
        snapshot=snapshot, source_broker=source_broker, source_snapshot=source_snapshot,
        volume=volume, private_dir=private, daemon_socket_identity=(1, 2))
    resources.callback(successor.close)
    def prepare(bound):
        profiles.update({
            'baseline': derive_runtime_profile(manifest, HostAdapter._defaults_from_record(rollback), 'baseline-g1', secrets),
            'candidate': derive_runtime_profile(manifest, HostAdapter._defaults_from_record(candidate), bound.candidate_generation, secrets),
            'rollback': derive_runtime_profile(manifest, HostAdapter._defaults_from_record(rollback), bound.rollback_generation, secrets),
        })
        names.update({'baseline': bound.baseline_name, 'candidate': bound.candidate_name, 'rollback': bound.rollback_name})
        by_name.update({names[role]: role for role in names})
        return successor.prepare(bound)

    coordinator = LocalNativeCoordinator(database=owner_path, manifest=manifest,
        task_owner=task_owner, prepare_bound=prepare)
    try:
        job, result = await coordinator.run_once()
        assert result == {'id': job['id'], 'state': 'failed'}, (coordinator.client.errors, coordinator.client.responses)
        assert coordinator.repo.list_runs()[0]['status'] == 'rolled_back'
        assert coordinator.client.calls == ['deploy_container', 'smoke_container', 'rollback_container', 'smoke_container_rollback']
        responses = coordinator.client.responses
        assert [row[0] for row in responses] == ['succeeded', 'failed', 'succeeded', 'succeeded']
        assert [row[2] for row in responses] == ['c' * 64, 'c' * 64, 'd' * 64, 'd' * 64]
        assert responses[1][3] == 'unhealthy' and responses[3][3] == 'healthy'
        with coordinator.repo._connection() as db:
            assert db.execute('SELECT job_id FROM hub_work_machines WHERE machine_id=?', (MACHINE,)).fetchone()[0] is None
        assert digest(frozen.read_bytes()) == snapshot.sha256
        bound = coordinator.client.bound
        for generation in (bound.candidate_generation, bound.rollback_generation):
            copy = generations / generation / 'hub.sqlite3'
            with sqlite3.connect(copy.as_uri() + '?mode=ro&immutable=1', uri=True) as db:
                assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
                assert db.execute('SELECT v FROM synthetic_fixture').fetchall() == [('baseline',)]
        await coordinator.close()
        cleaned = successor.seal_and_cleanup(coordinator)
        assert {identity for identity, _name in cleaned.containers} == {'c'*64, 'd'*64, 'e'*64}
    finally:
        try: await coordinator.close()
        finally: resources.close()
