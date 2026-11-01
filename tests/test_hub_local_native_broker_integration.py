"""Native queue -> real HostAdapter/backend -> broker -> synthetic Engine.

No Docker socket, real image, host authority or staging resources are used.
"""
import asyncio
import base64
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import hmac
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

if sys.platform != 'linux':
    pytest.skip('local native broker integration requires Linux', allow_module_level=True)

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
from scripts.hub_local_cleanup_receipt import DOMAIN, write_cleanup_receipt
from scripts.hub_local_test_engine_broker import EffectJournal, LocalTestBroker, LocalTestSocketServer, UnixHTTPConnection
from scripts.hub_local_test_volume import OwnedVolume, TMPFS_OPTIONS
from scripts.hub_local_test_oci import prepare_fixture_image, verify_fixture_image
from tests.test_hub_container_executor_adapter import FakeDriverIdentityVerifier
from tests.test_hub_container_executor_build_record import FakeStore, FakeTrust, make_record
from tests.local_synthetic_engine import SyntheticEngine
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
@pytest.mark.parametrize('via_runner',[
    'components','fixed-runner',
    pytest.param('fixed-fd',marks=pytest.mark.skipif(os.environ.get('HUB_LOCAL_FD_SYNTHETIC')!='1',
                                                    reason='requires disposable synthetic-FD QA container'))])
async def test_native_queue_real_adapter_and_broker_with_synthetic_engine_failure_rollback(tmp_path, monkeypatch, owned_resources, via_runner):
    """Synthetic Engine responses prove wiring, not actual Engine acceptance."""
    task_owner = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    volume_name = 'hub-local-test-' + task_owner
    fake_root = tmp_path / 'fake-engine-root'
    volume_root = fake_root / 'volumes' / volume_name / '_data'
    generations = volume_root / 'generations'; generations.mkdir(parents=True, mode=0o700)
    volume_root.chmod(0o700)
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
    initial_manifest = manifest
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
    # One task-owned Engine inventory spans source and successor brokers.
    volume = OwnedVolume(volume_name, task_owner, 'fixture-daemon', str(volume_root))
    engine_volume = replace(volume, options=TMPFS_OPTIONS) if via_runner != 'components' else volume
    upstream_path = '/var/run/docker.sock' if via_runner == 'fixed-fd' else str(tmp_path/'synthetic-engine.sock')
    engine = SyntheticEngine(socket_path=upstream_path, fake_root=fake_root, volume=engine_volume,
        owner=task_owner, images=images, archives=archives, manifest=manifest, secrets=secrets,
        baseline_db=baseline_db, dynamic_names=via_runner == 'fixed-fd')
    resources.callback(engine.close)
    engine.start()
    profiles,names,by_name,containers = engine.profiles,engine.names,engine.by_name,engine.containers

    # Bootstrap has its own broker journal and a child-owned PilotOwner. The
    # owner is enrolled before the child and reopened only after it exits.
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
    if via_runner != 'fixed-fd':
        monkeypatch.setattr(LocalTestBroker, '_connection',
            lambda self, timeout, total: UnixHTTPConnection(upstream_path, timeout, total))
    if via_runner != 'components':
        from scripts import hub_local_acceptance_runner as runner
        if via_runner == 'fixed-runner':
            monkeypatch.setattr(runner,'_socket_identity',lambda:(1,2))
            monkeypatch.setattr(runner,'discover_cached_image',lambda image,**_kwargs:image)
            monkeypatch.setattr(runner,'_bounded_volume',lambda _path:None)  # Synthetic Engine, no actual named tmpfs mount.
        actual_prepare=LocalSuccessor.prepare
        def prepare_runner(self,bound):
            profiles.update({
                'baseline':derive_runtime_profile(manifest,HostAdapter._defaults_from_record(rollback),'baseline-g1',secrets),
                'candidate':derive_runtime_profile(manifest,HostAdapter._defaults_from_record(candidate),bound.candidate_generation,secrets),
                'rollback':derive_runtime_profile(manifest,HostAdapter._defaults_from_record(rollback),bound.rollback_generation,secrets)})
            names.update({'baseline':bound.baseline_name,'candidate':bound.candidate_name,'rollback':bound.rollback_name})
            by_name.update({names[role]:role for role in names})
            return actual_prepare(self,bound)
        if via_runner == 'fixed-runner': monkeypatch.setattr(LocalSuccessor,'prepare',prepare_runner)
        def fixture_request(fixture):
            return {'record':base64.b64encode(fixture.record).decode(),
                'inputs':base64.b64encode(fixture.inputs).decode(),
                'manifest_digest':fixture.manifest_digest,'config_digest':fixture.config_digest,
                'blobs':{key:base64.b64encode(value).decode() for key,value in fixture.blobs.items()}}
        request={'schema':runner.SCHEMA,'task_id':task_owner,'daemon_id':'fixture-daemon',
            'volume_mount':str(volume_root),'private_dir':str(private),
            'manifest':manifest.canonical_bytes().decode(),'candidate':fixture_request(fixtures['candidate']),
            'rollback':fixture_request(fixtures['rollback']),'coordinator_id':'f'*64,
            'coordinator_image_id':'sha256:'+'b'*64,'cleanup_nonce':'a'*64,'cleanup_key':(b'k'*32).hex(),
            'startup_wait_ms':0,'volume_storage':'tmpfs-512m-v1'}
        try:
            if via_runner == 'fixed-fd':
                import signal
                map_path=Path('/tmp/hub-local-fake-docker-map.json')
                script_path=Path('/tmp/hub-local-fake-docker.py')
                for path in (map_path,script_path): assert not os.path.lexists(path)
                image_map={image.platform_manifest_digest:[image.image_config_id,image.platform.os,
                           image.platform.architecture,image.platform.variant or ''] for image in (candidate,rollback)}
                fd=os.open(map_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                resources.callback(lambda: map_path.unlink(missing_ok=True))
                with os.fdopen(fd,'w',encoding='utf8') as out: out.write(json.dumps(image_map))
                fd=os.open(script_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                resources.callback(lambda: script_path.unlink(missing_ok=True))
                with os.fdopen(fd,'w',encoding='utf8') as out:
                    out.write('''import json,sys\na=sys.argv[1:]\nif a[2:3]==["info"]: print("fixture-daemon")\nelif a[2:4]==["image","inspect"]: print("|".join(json.load(open("/tmp/hub-local-fake-docker-map.json"))[a[-1]]))\nelse: raise SystemExit(2)\n''')
                read_fd,write_fd=os.pipe(); process=None; writer=None
                encoded=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')
                try:
                    entry=Path(__file__).resolve().parents[1]/'scripts/hub_local_acceptance_runner.py'
                    boot='import os,sys; fd=int(sys.argv[1]); os.dup2(fd,3); os.close(fd) if fd!=3 else None; os.set_inheritable(3,True); os.execv(sys.executable,[sys.executable,"-I","-B",sys.argv[2],"--request-fd","3"])'
                    argv=[sys.executable,'-I','-B','-c',boot,str(read_fd),str(entry)]
                    process=subprocess.Popen(argv,
                        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                        env={'LANG':'C.UTF-8','PATH':'/usr/bin:/bin'},pass_fds=(read_fd,),
                        start_new_session=True)
                    os.close(read_fd); read_fd=None
                    def send():
                        try:
                            offset=0
                            while offset<len(encoded): offset+=os.write(write_fd,encoded[offset:offset+65536])
                        finally: os.close(write_fd)
                    writer=threading.Thread(target=send); writer.start()
                    output,error=await asyncio.to_thread(process.communicate,timeout=145)
                    assert process.returncode==0 and error==b'', (output,error)
                    outcome=json.loads(output)
                finally:
                    if process is not None and process.poll() is None:
                        os.killpg(process.pid,signal.SIGKILL); process.wait(timeout=5)
                    if read_fd is not None: os.close(read_fd)
                    if writer is None: os.close(write_fd)
                    else:
                        writer.join(timeout=5); assert not writer.is_alive()
            else:
                outcome=await runner.run(request)
            assert outcome['state']=='rolled_back' and outcome['cleanup']=='receipt_written'
            assert containers=={}
            envelope=json.loads((private/'cleanup-receipt.json').read_bytes())
            body=json.dumps(envelope['payload'],sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')
            assert envelope['signature']==hmac.new(b'k'*32,DOMAIN+body,hashlib.sha256).hexdigest()
            assert envelope['payload']['initial_manifest_digest']==manifest.digest()
        finally:
            resources.close()
        return
    initial_owner = PilotOwner(owner_path, allow_ephemeral_test_storage=True)
    initial_owner.close()
    source = LocalSource(manifest=manifest, candidate=candidate, rollback=rollback,
        volume=volume, private_dir=private, daemon_socket_identity=(1, 2))
    resources.callback(source.close)
    source_broker = source.broker
    source_path = str(private / 'bootstrap.sock')
    payload = {'manifest': manifest.canonical_bytes().decode(), 'owner_db': str(owner_path),
        'socket': source_path, 'generation_root': str(generations), 'journal': str(private / 'bootstrap-operations.sqlite'),
        'owner': task_owner, 'record': base64.b64encode(fixtures['rollback'].record).decode(),
        'blobs': {key: base64.b64encode(value).decode() for key, value in fixtures['rollback'].blobs.items()},
        'ids': [str(uuid.uuid4()) for _ in range(6)], 'startup_wait_ms':0}
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
        receipt_dir = private/'cleanup'; receipt_dir.mkdir(mode=0o700)
        key = b'k'*32; nonce = 'a'*64
        with pytest.raises(ValueError,match='handoff unavailable'):
            write_cleanup_receipt(directory=receipt_dir,key=key,nonce=nonce,
                coordinator_id='f'*64,coordinator_image_id='sha256:'+'b'*64,initial_manifest_digest=initial_manifest.digest(),
                baseline_backup_digest=snapshot.sha256,manifest_digest=manifest.digest(),
                coordinator=coordinator,source=source,successor=successor,cleaned=cleaned)
        assert list(receipt_dir.iterdir()) == []
        successor.close(); source.close()
        written = write_cleanup_receipt(directory=receipt_dir,key=key,nonce=nonce,
            coordinator_id='f'*64,coordinator_image_id='sha256:'+'b'*64,initial_manifest_digest=initial_manifest.digest(),
            baseline_backup_digest=snapshot.sha256,manifest_digest=manifest.digest(),
            coordinator=coordinator,source=source,successor=successor,cleaned=cleaned)
        envelope = json.loads((receipt_dir/'cleanup-receipt.json').read_bytes())
        raw = json.dumps(written,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
        assert envelope == {'payload':written,'signature':hmac.new(key,DOMAIN+raw,hashlib.sha256).hexdigest()}
        assert written['nonce'] == nonce and written['manifest_digest'] == manifest.digest()
        with pytest.raises(FileExistsError):
            write_cleanup_receipt(directory=receipt_dir,key=key,nonce=nonce,
                coordinator_id='f'*64,coordinator_image_id='sha256:'+'b'*64,initial_manifest_digest=initial_manifest.digest(),
                baseline_backup_digest=snapshot.sha256,manifest_digest=manifest.digest(),
                coordinator=coordinator,source=source,successor=successor,cleaned=cleaned)
    finally:
        try: await coordinator.close()
        finally: resources.close()
