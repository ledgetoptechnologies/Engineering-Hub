"""Offline policy tests; never connects to a Docker daemon."""
import json
import os
from pathlib import Path
import threading
import uuid

import httpx
import pytest

from hub_container_executor.backend import ContainerPlan
from hub_container_executor.daemon_backend import DockerEngineBackend, _PROBE_SCRIPT
from scripts.hub_local_test_engine_broker import (CleanedBrokerSnapshot, Denied, EffectJournal,
                                                  LocalTestBroker, LocalTestSocketServer)
from scripts.hub_local_test_volume import OwnedVolume
from tests.test_hub_container_executor_daemon_backend import make_profile, verified_image


@pytest.fixture
def rig(tmp_path):
    from dataclasses import replace
    _, image, _ = verified_image()
    rollback_manifest, rollback_image, _ = verified_image('rollback')
    profile = replace(make_profile(image, secrets={'CODEX_TOKEN': 'synthetic-codex',
        'HERMES_TOKEN': 'synthetic-hermes', 'CLAUDE_TOKEN': 'synthetic-claude'}),
        cpu_millicores=300, pids_limit=80)
    owner = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    volume_name = 'hub-local-test-' + owner
    volume = str(tmp_path / 'fake-daemon-root' / 'volumes' / volume_name / '_data')
    source = Path(volume) / 'generations' / profile.state_generation_id
    name = 'hub-pilot-' + owner.replace('-', '')[:24] + '-bbbbbbbb-bbb-candidate'
    other = 'hub-pilot-' + owner.replace('-', '')[:24] + '-bbbbbbbb-bbb-rollback'
    baseline = 'hub-pilot-' + owner.replace('-', '')[:24] + '-00000000-000-rollback'
    backend = object.__new__(DockerEngineBackend)
    backend._gate_mount_verifier = None
    body = backend._create_body(ContainerPlan(name, image, image.runnable_reference, False, profile), source, profile.environment)
    rollback_profile = replace(make_profile(rollback_image, generation_id='rollback', secrets={'CODEX_TOKEN': 'synthetic-codex',
        'HERMES_TOKEN': 'synthetic-hermes', 'CLAUDE_TOKEN': 'synthetic-claude'}), cpu_millicores=300, pids_limit=80)
    second = backend._create_body(ContainerPlan(other, rollback_image, rollback_image.runnable_reference, False, rollback_profile),
                                  Path(volume) / 'generations' / 'rollback', rollback_profile.environment)
    baseline_profile = replace(make_profile(rollback_image, generation_id='baseline-g1', secrets={'CODEX_TOKEN': 'synthetic-codex',
        'HERMES_TOKEN': 'synthetic-hermes', 'CLAUDE_TOKEN': 'synthetic-claude'}), cpu_millicores=300, pids_limit=80)
    baseline_body = backend._create_body(ContainerPlan(baseline, rollback_image, rollback_image.runnable_reference, False, baseline_profile),
                                        Path(volume) / 'generations' / 'baseline-g1', baseline_profile.environment)
    probe = {'AttachStdin': False, 'AttachStdout': True, 'AttachStderr': False, 'Tty': False,
             'User': '10001:10001', 'Cmd': ['/usr/local/bin/python3', '-I', '-c', _PROBE_SCRIPT,
             'health_json', '/health', '200', 'probe', '1024', '1000', 'a' * 40]}
    journal = EffectJournal(tmp_path / 'journal.sqlite')
    broker = LocalTestBroker(socket_path='/var/run/docker.sock', socket_identity=(1, 2), version='1.41',
                             owner=owner, volume=OwnedVolume(volume_name, owner, 'fixture-daemon', volume),
                             images={body['Image']: (image.image_config_id, image.platform_manifest_digest),
                                     second['Image']: (rollback_image.image_config_id, rollback_image.platform_manifest_digest)},
                             creates={name: body, other: second, baseline: baseline_body}, exec_bodies=[probe], journal=journal)
    broker._attest_volume = lambda: None
    broker._read_bounded = lambda response, maximum: response.read(maximum + 1)
    broker._fixture_rollback_manifest = rollback_manifest
    broker._fixture_rollback_image = rollback_image
    yield broker, journal, name, body, probe
    journal.close()


def test_backend_generated_profile_is_enrolled_and_other_calls_never_forward(rig, monkeypatch):
    broker, _, name, body, _ = rig
    monkeypatch.setattr(broker, '_request', lambda *args, **kwargs: pytest.fail('must not reach daemon'))
    for method, target, data in [('DELETE', '/v1.41/containers/' + 'f' * 64, b''),
                                  ('GET', '/v1.41/containers/json', b''),
                                  ('POST', '/v1.41/containers/create?name=' + name, b'{}'),
                                  ('POST', '/v1.41/containers/create?name=' + name + '&x=1', json.dumps(body).encode()),
                                  ('POST', '/v1.41/containers/create?name=' + name, b'{"Image":1,"Image":2}')]:
        with pytest.raises(Denied):
            broker.dispatch(method, target, data, lambda *_: None)


def test_uncertain_effect_is_durable_and_reopen_never_replays(rig, monkeypatch, tmp_path):
    broker, journal, name, body, _ = rig
    seen = []
    def disconnected(*args, **kwargs):
        seen.append(args)
        raise OSError('lost response')
    monkeypatch.setattr(broker, '_request', disconnected)
    target = '/v1.41/containers/create?name=' + name
    with pytest.raises(OSError):
        broker.dispatch('POST', target, json.dumps(body).encode(), lambda *_: None)
    journal.close()
    replacement = EffectJournal(tmp_path / 'journal.sqlite')
    broker.journal = replacement
    with pytest.raises(Denied):
        broker.dispatch('POST', target, json.dumps(body).encode(), lambda *_: None)
    assert len(seen) == 1
    replacement.close()
    # The fixture closes its journal in teardown; make that idempotent here.
    broker.journal = journal


def test_policy_rejects_missing_bounds_and_dangerous_mounts(rig, tmp_path):
    broker, journal, name, body, _ = rig
    from copy import deepcopy
    for mutate in (lambda b: b['HostConfig'].update(Privileged=True),
                   lambda b: b['HostConfig'].update(NetworkMode='host'),
                   lambda b: b['HostConfig']['Mounts'][0].update(Source='/etc'),
                   lambda b: b['HostConfig'].update(Memory=3 * 1024**3)):
        bad = deepcopy(broker.creates); mutate(bad[name])
        with pytest.raises(Denied):
            LocalTestBroker(socket_path=broker.socket_path, socket_identity=(1, 2), version=broker.version,
                            owner=broker.owner, volume=broker.volume, images=broker.images,
                            creates=bad, exec_bodies=broker.exec_bodies, journal=journal)


def test_aggregate_concurrent_application_budget_is_enforced(rig):
    from copy import deepcopy
    broker, journal, _, _, _ = rig
    bad = deepcopy(broker.creates)
    for body in bad.values(): body['HostConfig']['NanoCpus'] = 500_000_000
    with pytest.raises(Denied, match='aggregate application budget'):
        LocalTestBroker(socket_path=broker.socket_path, socket_identity=(1, 2), version=broker.version,
                        owner=broker.owner, volume=broker.volume, images=broker.images,
                        creates=bad, exec_bodies=broker.exec_bodies, journal=journal)


@pytest.mark.parametrize('change', ['daemon', 'root', 'name', 'label', 'mountpoint', 'driver'])
def test_live_volume_and_daemon_are_rechecked_before_effect(rig, monkeypatch, change):
    broker, _, _, _, _ = rig
    del broker._attest_volume
    info = {'ID': broker.volume.daemon_id, 'DockerRootDir': str(Path(broker.mount).parents[2])}
    volume = {'Name': broker.volume.name, 'Driver': 'local', 'Scope': 'local',
              'Mountpoint': broker.volume.mountpoint, 'Labels': {'engineering-hub.local-test-owner': broker.owner},
              'Options': {}}
    if change == 'daemon': info['ID'] = 'foreign-daemon'
    elif change == 'root': info['DockerRootDir'] = '/foreign'
    elif change == 'name': volume['Name'] = 'foreign-volume'
    elif change == 'label': volume['Labels'] = {}
    elif change == 'mountpoint': volume['Mountpoint'] = '/foreign'
    else: volume['Driver'] = 'foreign'
    class Connection:
        def close(self): pass
    class Response:
        status = 200
        def __init__(self, value): self.value = value
        def read(self, _): return json.dumps(self.value).encode()
    monkeypatch.setattr(broker, '_request', lambda _m, target, **_k:
                        (Connection(), Response(info if target.endswith('/info') else volume)))
    with pytest.raises(Denied):
        broker._attest_volume()


def test_successful_create_injects_label_checks_returned_container_and_is_one_shot(rig, monkeypatch):
    broker, journal, name, body, _ = rig
    identity = 'e' * 64
    calls = []
    class Connection:
        def close(self): pass
    class Response:
        def __init__(self, value, status): self.raw, self.status = json.dumps(value).encode(), status
        def read(self, _): return self.raw
        def getheader(self, _, default=None): return default
    def request(method, target, sent=None, **_):
        calls.append((method, target, sent))
        if method == 'POST':
            assert sent['Labels'] == {'engineering-hub.local-test-owner': broker.owner}
            assert {key: value for key, value in sent.items() if key != 'Labels'} == body
            return Connection(), Response({'Id': identity}, 201)
        expected = body['HostConfig']['Mounts'][0]
        actual = {'Id': identity, 'Image': broker.images[body['Image']][0],
                  'Config': {'Image': body['Image'], 'User': body['User'], 'Env': body['Env'],
                             'Labels': {'engineering-hub.local-test-owner': broker.owner}},
                  'HostConfig': body['HostConfig'], 'State': {'Status': 'created'},
                  'Mounts': [{'Type': 'bind', 'Source': expected['Source'], 'Destination': expected['Target'],
                              'RW': True, 'Mode': '', 'Propagation': 'rprivate'}]}
        return Connection(), Response(actual, 200)
    monkeypatch.setattr(broker, '_request', request)
    captured = []
    emit = lambda status, headers, chunks: captured.append((status, b''.join(chunks)))
    target = '/v1.41/containers/create?name=' + name
    broker.dispatch('POST', target, json.dumps(body).encode(), emit)
    assert journal.owned('container', identity) == (name,)
    assert captured[0][0] == 201 and len(calls) == 2
    with pytest.raises(Denied):
        broker.dispatch('POST', target, json.dumps(body).encode(), emit)
    assert len(calls) == 2


def test_failed_reply_delivery_holds_future_effects(rig, monkeypatch):
    broker, journal, name, body, _ = rig
    class Connection:
        def close(self): pass
    class Response:
        status = 201
        def read(self, _): return json.dumps({'Id': 'e' * 64}).encode()
        def getheader(self, _, default=None): return default
    monkeypatch.setattr(broker, '_request', lambda *_a, **_k: (Connection(), Response()))
    monkeypatch.setattr(broker, '_inspect_owned', lambda *_: None)
    def lost(*_): raise BrokenPipeError()
    with pytest.raises(BrokenPipeError):
        broker.dispatch('POST', '/v1.41/containers/create?name=' + name, json.dumps(body).encode(), lost)
    assert journal.db.execute('SELECT state FROM effects').fetchone() == ('held',)
    with pytest.raises(Denied):
        journal.begin('start', 'e' * 64)


def test_identical_probe_create_is_never_replayed(rig, monkeypatch):
    broker, journal, name, _, probe = rig
    container = 'e' * 64
    journal.db.execute('INSERT INTO owned VALUES(?,?,?)', ('container', container, name))
    class Connection:
        def close(self): pass
    class Response:
        status = 201
        def read(self, _): return json.dumps({'Id': 'f' * 64}).encode()
        def getheader(self, _, default=None): return default
    calls = []
    def request(*_a, **_k): calls.append(1); return Connection(), Response()
    monkeypatch.setattr(broker, '_request', request)
    monkeypatch.setattr(broker, '_inspect_owned', lambda *_: None)
    target = '/v1.41/containers/' + container + '/exec'
    broker.dispatch('POST', target, json.dumps(probe).encode(), lambda *_: None)
    with pytest.raises(Denied):
        broker.dispatch('POST', target, json.dumps(probe).encode(), lambda *_: None)
    assert len(calls) == 1


def test_pending_effect_blocks_seal_and_retains_resources(rig):
    broker, journal, _, _, _ = rig
    journal.begin('create', 'uncertain')
    with pytest.raises(Denied, match='uncertain effect'):
        broker.seal()
    assert not journal.is_sealed()


def test_seal_waits_for_active_request_and_persistently_closes_admission(rig, monkeypatch, tmp_path):
    broker, journal, name, _, _ = rig
    journal.db.execute('INSERT INTO owned VALUES(?,?,?)', ('container', 'e' * 64, name))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    result, errors = [], []
    def stream(*_):
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(broker, '_stream', stream)
    def active():
        try: broker.dispatch('GET', '/v1.41/info', b'', lambda *_: None)
        except BaseException as error: errors.append(error)
    def seal():
        try: result.append(broker.seal())
        except BaseException as error: errors.append(error)
        finally: finished.set()
    running = threading.Thread(target=active); closing = threading.Thread(target=seal)
    running.start(); assert entered.wait(2); closing.start()
    try:
        assert not finished.wait(.1)
    finally:
        release.set(); running.join(timeout=3); closing.join(timeout=3)
    assert not errors and not running.is_alive() and not closing.is_alive()
    assert result[0].containers == (('e' * 64, name),) and journal.is_sealed()
    with pytest.raises(Denied, match='admission closed'):
        broker.dispatch('GET', '/v1.41/info', b'', lambda *_: None)
    reopened = EffectJournal(tmp_path / 'journal.sqlite')
    try:
        assert reopened.is_sealed()
        with pytest.raises(Denied): reopened.begin('start', 'e' * 64)
    finally:
        reopened.close()


def test_cleanup_requires_joined_listener_and_stops_deletes_only_exact_owned_id(rig, monkeypatch, tmp_path):
    broker, journal, name, _, _ = rig
    identity = 'e' * 64
    journal.db.execute('INSERT INTO owned VALUES(?,?,?)', ('container', identity, name))
    tmp_path.chmod(0o700)
    server = LocalTestSocketServer(str(tmp_path / 'cleanup.sock'), broker)
    worker = threading.Thread(target=server.serve_forever); worker.start()
    snapshot = broker.seal()
    try:
        with pytest.raises(Denied, match='cleanup boundary'):
            broker.cleanup_owned(snapshot)
    finally:
        server.shutdown(); worker.join(timeout=3); server.server_close()
    assert not worker.is_alive()
    statuses = iter(['running', 'exited', 'exited'])
    monkeypatch.setattr(broker, '_inspect_owned', lambda *_: next(statuses))
    requests = []
    class Connection:
        def close(self): pass
    class Response:
        def __init__(self, status): self.status = status
        def read(self, _): return b'{}'
    def request(method, target, **_):
        requests.append((method, target))
        if method == 'GET':
            return Connection(), Response(200 if len(requests) == 1 else 404)
        return Connection(), Response(204)
    monkeypatch.setattr(broker, '_request', request)
    cleaned = broker.cleanup_owned(snapshot)
    assert type(cleaned) is CleanedBrokerSnapshot and cleaned.containers == snapshot.containers
    assert requests == [('GET', '/v1.41/containers/' + identity + '/json'),
                        ('POST', '/v1.41/containers/' + identity + '/stop?t=5'),
                        ('DELETE', '/v1.41/containers/' + identity + '?force=0&v=0'),
                        ('GET', '/v1.41/containers/' + identity + '/json')]
    assert journal.db.execute("SELECT state FROM effects WHERE kind LIKE 'cleanup_%' ORDER BY id").fetchall() == [('complete',), ('complete',)]


def test_unknown_cleanup_stop_is_never_retried(rig, monkeypatch, tmp_path):
    broker, journal, name, _, _ = rig
    identity = 'e' * 64
    journal.db.execute('INSERT INTO owned VALUES(?,?,?)', ('container', identity, name))
    tmp_path.chmod(0o700)
    server = LocalTestSocketServer(str(tmp_path / 'cleanup.sock'), broker)
    worker = threading.Thread(target=server.serve_forever); worker.start()
    snapshot = broker.seal(); server.shutdown(); worker.join(timeout=3); server.server_close()
    class Connection:
        def close(self): pass
    class Response:
        status = 200
        def read(self, _): return b'{}'
    calls = []
    def request(method, target, **_):
        calls.append((method, target))
        if method == 'POST': raise OSError('lost stop response')
        return Connection(), Response()
    monkeypatch.setattr(broker, '_request', request)
    monkeypatch.setattr(broker, '_inspect_owned', lambda *_: 'running')
    with pytest.raises(OSError): broker.cleanup_owned(snapshot)
    assert journal.db.execute("SELECT state FROM effects WHERE kind='cleanup_stop'").fetchone() == ('pending',)
    with pytest.raises(Denied): broker.cleanup_owned(snapshot)
    assert [method for method, _ in calls].count('POST') == 1


def _baseline_transfer_fixture(rig, tmp_path, monkeypatch):
    import hashlib
    from uuid import uuid4
    from hub_container_executor.adapter import HostAdapter
    from hub_container_executor.baseline_receipt import ProtectedBaselineProvider
    from hub_container_executor.runtime_profile import derive_runtime_profile, probe_set_digest, runtime_profile_digest
    from engineering_hub.container_pilot_manifest import PilotManifest
    from tests.test_hub_container_executor_build_record import FakeStore
    source, _, _, _, _ = rig
    rollback = source._fixture_rollback_image
    data = source._fixture_rollback_manifest.model_dump(mode='json', by_alias=True)
    data['runtime']['resource_name_prefix'] = 'hub-pilot-' + uuid.UUID(source.owner).hex[:24]
    data['runtime']['resources']['cpu_millicores'] = 300
    data['runtime']['resources']['pids_limit'] = 80
    manifest = PilotManifest.from_bytes(json.dumps(data).encode())
    baseline_name = 'hub-pilot-' + uuid.UUID(source.owner).hex[:24] + '-00000000-000-rollback'
    baseline_id = 'e' * 64
    secrets = {'CODEX_TOKEN': 'synthetic-codex', 'HERMES_TOKEN': 'synthetic-hermes', 'CLAUDE_TOKEN': 'synthetic-claude'}
    profile = derive_runtime_profile(manifest, HostAdapter._defaults_from_record(rollback), 'baseline-g1', secrets)
    row = dict(schema='hub_baseline_receipt_v1', campaign_id=manifest.campaign_id, proof_id=str(uuid4()),
        manifest_digest=manifest.digest(), baseline_id=manifest.state_policy.baseline_id,
        runner=manifest.runner_binding.model_dump(mode='json'),
        rollback=dict(runnable_reference=rollback.runnable_reference, runnable_media_type=rollback.runnable_media_type,
            platform_manifest_digest=rollback.platform_manifest_digest, image_config_id=rollback.image_config_id,
            source_commit=rollback.source_commit, record_digest=rollback.record_digest),
        container_id=baseline_id, state_generation_id='baseline-g1',
        backup=dict(digest=manifest.state_policy.baseline_backup_digest, size_bytes=4096),
        runtime_profile_digest=runtime_profile_digest(profile), probe_set_digest=probe_set_digest(manifest.probes.rollback),
        observation=dict(health='healthy', evidence_verified=True, observed_at=1700000000),
        baseline_inventory=[dict(name='hub.sqlite3', size_bytes=4096, digest='sha256:' + 'a' * 64)])
    raw = json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    pin = 'sha256:' + hashlib.sha256(raw).hexdigest()
    store = FakeStore({pin: raw})
    provider = ProtectedBaselineProvider(store, receipt_digest=pin, campaign_id=manifest.campaign_id,
        verified_rollback=rollback, expected_runtime_profile_digest=row['runtime_profile_digest'])
    source.journal.db.execute('INSERT INTO owned VALUES(?,?,?)', ('container', baseline_id, baseline_name))
    tmp_path.chmod(0o700)
    server = LocalTestSocketServer(str(tmp_path / 'bootstrap.sock'), source)
    worker = threading.Thread(target=server.serve_forever); worker.start()
    snapshot = source.seal(); server.shutdown(); worker.join(timeout=3); server.server_close()
    assert not worker.is_alive()
    creates = {name.replace('-bbbbbbbb-bbb-', '-cccccccc-ccc-'): body for name, body in source.creates.items()}
    target_journal = EffectJournal(tmp_path / 'target.sqlite')
    target = LocalTestBroker(socket_path=source.socket_path, socket_identity=source.socket_identity, version=source.version,
        owner=source.owner, volume=source.volume, images=source.images, creates=creates,
        exec_bodies=source.exec_bodies, journal=target_journal)
    monkeypatch.setattr(source, '_attest_volume', lambda: None)
    monkeypatch.setattr(target, '_attest_volume', lambda: None)
    monkeypatch.setattr(source, '_inspect_owned', lambda *_: 'running')
    monkeypatch.setattr(target, '_inspect_owned', lambda *_: 'running')
    return source, target, target_journal, snapshot, manifest, rollback, provider, store, pin, baseline_name, baseline_id


def test_exact_protected_baseline_transfers_once_and_source_cannot_cleanup(rig, tmp_path, monkeypatch):
    source, target, journal, snapshot, manifest, image, provider, _, _, name, identity = _baseline_transfer_fixture(rig, tmp_path, monkeypatch)
    try:
        result = target.transfer_baseline_from(source=source, snapshot=snapshot, manifest=manifest,
                                               rollback_image=image, provider=provider)
        assert result.container_id == identity and journal.owned('container', identity) == (name,)
        assert source.journal.transfer_started()
        with pytest.raises(Denied): source.cleanup_owned(snapshot)
        with pytest.raises(Denied):
            target.transfer_baseline_from(source=source, snapshot=snapshot, manifest=manifest,
                                          rollback_image=image, provider=provider)
        server = LocalTestSocketServer(str(tmp_path / 'run.sock'), target)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            target_snapshot = target.seal()
        finally:
            try:
                server.shutdown()
            finally:
                try:
                    worker.join(timeout=3)
                finally:
                    server.server_close()
        assert not worker.is_alive()
        class Connection:
            def close(self): pass
        class Response:
            status = 404
        monkeypatch.setattr(target, '_request', lambda *_a, **_k: (Connection(), Response()))
        monkeypatch.setattr(target, '_read_bounded', lambda *_: b'')
        cleaned = target.cleanup_owned(target_snapshot)
        assert type(cleaned) is CleanedBrokerSnapshot and cleaned.containers == ((identity, name),)
    finally:
        journal.close(); provider.close()


def test_transfer_inspection_uncertainty_holds_both_journals(rig, tmp_path, monkeypatch):
    source, target, journal, snapshot, manifest, image, provider, _, _, _, _ = _baseline_transfer_fixture(rig, tmp_path, monkeypatch)
    def failure(*_): raise OSError('synthetic inspection lost')
    monkeypatch.setattr(target, '_inspect_owned', failure)
    try:
        with pytest.raises(OSError):
            target.transfer_baseline_from(source=source, snapshot=snapshot, manifest=manifest,
                                          rollback_image=image, provider=provider)
        assert source.journal.db.execute("SELECT state FROM effects WHERE kind='baseline_transfer_out'").fetchone() == ('pending',)
        assert journal.db.execute("SELECT state FROM effects WHERE kind='baseline_transfer_in'").fetchone() == ('pending',)
        with pytest.raises(Denied): source.cleanup_owned(snapshot)
        with pytest.raises(Denied):
            target.transfer_baseline_from(source=source, snapshot=snapshot, manifest=manifest,
                                          rollback_image=image, provider=provider)
    finally:
        journal.close(); provider.close()


def test_transfer_receipt_drift_after_intent_holds_both_journals(rig, tmp_path, monkeypatch):
    source, target, journal, snapshot, manifest, image, provider, store, pin, _, _ = _baseline_transfer_fixture(rig, tmp_path, monkeypatch)
    def drift(*_): store.values[pin] = b'changed receipt'
    monkeypatch.setattr(target, '_inspect_owned', drift)
    try:
        with pytest.raises(Denied, match='inputs changed'):
            target.transfer_baseline_from(source=source, snapshot=snapshot, manifest=manifest,
                                          rollback_image=image, provider=provider)
        assert source.journal.db.execute("SELECT state FROM effects WHERE kind='baseline_transfer_out'").fetchone() == ('pending',)
        assert journal.db.execute("SELECT state FROM effects WHERE kind='baseline_transfer_in'").fetchone() == ('pending',)
    finally:
        journal.close(); provider.close()


def test_transfer_failure_after_source_commit_keeps_target_held(rig, tmp_path, monkeypatch):
    source, target, journal, snapshot, manifest, image, provider, _, _, _, _ = _baseline_transfer_fixture(rig, tmp_path, monkeypatch)
    def failure(*_a, **_k): raise OSError('synthetic target commit loss')
    monkeypatch.setattr(journal, 'complete', failure)
    try:
        with pytest.raises(OSError):
            target.transfer_baseline_from(source=source, snapshot=snapshot, manifest=manifest,
                                          rollback_image=image, provider=provider)
        assert source.journal.db.execute("SELECT state FROM effects WHERE kind='baseline_transfer_out'").fetchone() == ('complete',)
        assert journal.db.execute("SELECT state FROM effects WHERE kind='baseline_transfer_in'").fetchone() == ('pending',)
        with pytest.raises(Denied): source.cleanup_owned(snapshot)
        with pytest.raises(Denied):
            target.transfer_baseline_from(source=source, snapshot=snapshot, manifest=manifest,
                                          rollback_image=image, provider=provider)
    finally:
        journal.close(); provider.close()


def test_baseline_transfer_rechecks_both_brokers_through_synthetic_engine_uds(rig, tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler
    import socketserver
    from scripts.hub_local_test_engine_broker import UnixHTTPConnection
    source, target, journal, snapshot, manifest, image, provider, _, _, name, identity = _baseline_transfer_fixture(rig, tmp_path, monkeypatch)
    expected = source.creates[name]; mount = expected['HostConfig']['Mounts'][0]
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            requests.append(self.path)
            if self.path.endswith('/info'):
                value = {'ID': source.volume.daemon_id, 'DockerRootDir': str(Path(source.mount).parents[2])}
            elif '/volumes/' in self.path:
                value = {'Name': source.volume.name, 'Driver': 'local', 'Scope': 'local', 'Mountpoint': source.mount,
                         'Labels': {'engineering-hub.local-test-owner': source.owner}, 'Options': {}}
            else:
                assert self.path.endswith('/containers/' + identity + '/json')
                value = {'Id': identity, 'Image': image.image_config_id, 'State': {'Status': 'running'},
                         'Config': {'Image': expected['Image'], 'User': expected['User'], 'Env': expected['Env'],
                                    'Labels': {'engineering-hub.local-test-owner': source.owner}},
                         'HostConfig': expected['HostConfig'],
                         'Mounts': [{'Type': 'bind', 'Source': mount['Source'], 'Destination': mount['Target'],
                                     'RW': True, 'Mode': '', 'Propagation': 'rprivate'}]}
            raw = json.dumps(value).encode(); self.send_response(200)
            self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    path = str(tmp_path / 'synthetic-engine.sock')
    upstream = socketserver.UnixStreamServer(path, Handler)
    worker = threading.Thread(target=upstream.serve_forever); worker.start()
    for broker in (source, target):
        del broker._attest_volume; del broker._inspect_owned
        if '_read_bounded' in broker.__dict__: del broker._read_bounded
        monkeypatch.setattr(broker, '_connection', lambda timeout, total: UnixHTTPConnection(path, timeout, total))
    try:
        result = target.transfer_baseline_from(source=source, snapshot=snapshot, manifest=manifest,
                                               rollback_image=image, provider=provider)
        assert result.container_id == identity and journal.owned('container', identity) == (name,)
        assert len(requests) == 6 and all(path.startswith('/v1.41/') for path in requests)
    finally:
        upstream.shutdown(); worker.join(timeout=3); upstream.server_close(); journal.close(); provider.close()
    assert not worker.is_alive()


def test_real_unix_socket_denies_unknown_path_and_removes_own_socket(rig, tmp_path):
    broker, _, _, _, _ = rig
    path = tmp_path / 'broker.sock'
    tmp_path.chmod(0o700)
    server = LocalTestSocketServer(str(path), broker)
    worker = threading.Thread(target=server.serve_forever, daemon=False)
    worker.start()
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), base_url='http://docker', timeout=3) as client:
            assert client.get('/v1.41/containers/json').status_code == 403
    finally:
        server.shutdown(); worker.join(timeout=3); server.server_close()
    assert not worker.is_alive() and not os.path.lexists(path)


def test_idle_header_client_cannot_indefinitely_block_server(rig, tmp_path, monkeypatch):
    import socket
    from scripts import hub_local_test_engine_broker as module
    broker, _, _, _, _ = rig
    monkeypatch.setattr(module, '_REQUEST_TIMEOUT_SECONDS', .1)
    tmp_path.chmod(0o700)
    path = tmp_path / 'broker.sock'
    server = LocalTestSocketServer(str(path), broker)
    worker = threading.Thread(target=server.serve_forever); worker.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(path)); client.sendall(b'GET /v1.41/info HTTP/1.1\r\nHost:')
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), base_url='http://docker', timeout=2) as other:
            assert other.get('/not-allowed').status_code == 403
    finally:
        client.close(); server.shutdown(); worker.join(timeout=3); server.server_close()
    assert not worker.is_alive()


def test_slow_drip_headers_cannot_keep_server_busy(rig, tmp_path, monkeypatch):
    import socket
    from scripts import hub_local_test_engine_broker as module
    broker, _, _, _, _ = rig
    monkeypatch.setattr(module, '_REQUEST_TIMEOUT_SECONDS', .15)
    tmp_path.chmod(0o700)
    path = tmp_path / 'broker.sock'
    server = LocalTestSocketServer(str(path), broker)
    worker = threading.Thread(target=server.serve_forever); worker.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stop = threading.Event()
    def drip():
        while not stop.wait(.02):
            try: client.send(b'X')
            except OSError: break
    sender = threading.Thread(target=drip)
    try:
        client.connect(str(path)); sender.start()
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), base_url='http://docker', timeout=2) as other:
            assert other.get('/not-allowed').status_code == 403
    finally:
        stop.set(); client.close(); sender.join(timeout=1)
        server.shutdown(); worker.join(timeout=3); server.server_close()
    assert not sender.is_alive() and not worker.is_alive()


def test_enrollment_mutation_fails_before_forwarding(rig, monkeypatch):
    broker, _, name, _, _ = rig
    monkeypatch.setattr(broker, '_request', lambda *_a, **_k: pytest.fail('must not forward'))
    broker.creates[name]['HostConfig']['Privileged'] = True
    with pytest.raises(Denied):
        broker.dispatch('GET', '/v1.41/info', b'', lambda *_: None)


def test_stream_closes_upstream_if_downstream_header_write_fails(rig, monkeypatch):
    from types import SimpleNamespace
    broker, _, _, _, _ = rig
    pair = __import__('socket').socketpair()
    class Connection:
        closed = False
        def close(self): self.closed = True
    class Response:
        status = 200
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=pair[0]))
        def getheader(self, _, default=None): return default
    conn = Connection()
    monkeypatch.setattr(broker, '_request', lambda *_a, **_k: (conn, Response()))
    try:
        def lost(*_): raise BrokenPipeError()
        with pytest.raises(BrokenPipeError):
            broker._stream('GET', '/v1.41/info', lost, 1000)
        assert conn.closed
    finally:
        pair[0].close(); pair[1].close()


def test_stream_watchdog_interrupts_slow_drip_before_absolute_deadline_is_lost(rig, monkeypatch):
    from types import SimpleNamespace
    import socket
    import time
    from scripts import hub_local_test_engine_broker as module
    broker, _, _, _, _ = rig
    left, right = socket.socketpair()
    stop = threading.Event()
    def drip():
        while not stop.wait(.02):
            try: right.send(b'x')
            except OSError: break
    writer = threading.Thread(target=drip); writer.start()
    class Connection:
        def close(self): left.close()
    class Response:
        status = 200
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=left))
        def getheader(self, _, default=None): return default
        def read1(self, _):
            while left.recv(1): pass
            return b''
    monkeypatch.setattr(module, '_MAX_STREAM_SECONDS', .15)
    monkeypatch.setattr(broker, '_request', lambda *_a, **_k: (Connection(), Response()))
    started = time.monotonic()
    try:
        with pytest.raises(Denied):
            broker._stream('GET', '/v1.41/info', lambda _s, _h, chunks: b''.join(chunks), 1000)
        assert time.monotonic() - started < 1.0
    finally:
        stop.set(); right.close(); writer.join(timeout=1)
    assert not writer.is_alive()


def test_real_backend_inspection_accepts_the_added_test_label(tmp_path):
    from tests.test_hub_container_executor_daemon_backend import engine29_inspection_fixture
    backend, observed, _, _ = engine29_inspection_fixture(tmp_path)
    observed['Config']['Labels'] = {'engineering-hub.local-test-owner': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}
    try:
        assert backend.inspect_container('c' * 64, 3000).status == 'created'
    finally:
        backend.close()


def test_owned_inspection_accepts_engine_private_modes_but_rejects_host_modes(rig, tmp_path, monkeypatch):
    from tests.test_hub_container_executor_daemon_backend import engine29_inspection_fixture
    broker, _, name, body, _ = rig
    backend, observed, _, _ = engine29_inspection_fixture(tmp_path)
    try:
        observed['Id'] = 'e' * 64
        observed['Image'] = broker.images[body['Image']][1]
        observed['Config'].update(Image=body['Image'], Env=body['Env'],
                                  Labels={'engineering-hub.local-test-owner': broker.owner})
        source = body['HostConfig']['Mounts'][0]['Source']
        observed['Mounts'][0]['Source'] = source
        observed['HostConfig']['Mounts'][0]['Source'] = source
        observed['HostConfig']['PublishAllPorts'] = False
        observed['HostConfig']['NanoCpus'] = body['HostConfig']['NanoCpus']
        observed['HostConfig']['PidsLimit'] = body['HostConfig']['PidsLimit']
        observed['HostConfig']['PidMode'] = observed['HostConfig']['IpcMode'] = 'private'
        class Connection:
            def close(self): pass
        class Response:
            status = 200
            def read(self, _): return json.dumps(observed).encode()
        monkeypatch.setattr(broker, '_request', lambda *_a, **_k: (Connection(), Response()))
        broker._inspect_owned('e' * 64, name, 'created')
        observed['HostConfig']['IpcMode'] = 'host'
        with pytest.raises(Denied):
            broker._inspect_owned('e' * 64, name, 'created')
    finally:
        backend.close()


def test_native_backend_reads_runner_through_real_broker_and_fake_unix_engine(rig, tmp_path, monkeypatch):
    from dataclasses import replace
    from http.server import BaseHTTPRequestHandler
    import socketserver
    import time
    from types import SimpleNamespace
    from scripts.hub_local_test_engine_broker import UnixHTTPConnection
    from tests.test_hub_container_executor_daemon_backend import binding
    broker, _, _, _, _ = rig
    manifest, _, _ = verified_image()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            assert self.path == '/v1.41/info'
            body = json.dumps({'ID': 'fixture-daemon', 'OSType': 'linux', 'Architecture': 'amd64',
                               'DockerRootDir': str(Path(broker.mount).parents[2])}).encode()
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    upstream_path = str(tmp_path / 'engine.sock')
    upstream = socketserver.UnixStreamServer(upstream_path, Handler)
    upstream_thread = threading.Thread(target=upstream.serve_forever); upstream_thread.start()
    monkeypatch.setattr(broker, '_connection', lambda timeout, total: UnixHTTPConnection(upstream_path, timeout, total))
    broker_path = str(tmp_path / 'broker.sock')
    server = LocalTestSocketServer(broker_path, broker)
    worker = threading.Thread(target=server.serve_forever); worker.start()
    backend = DockerEngineBackend(replace(binding(manifest, tmp_path, api_version='1.41'), unix_socket_path=broker_path))
    try:
        assert backend.inspect_runner(3000).daemon_identity == 'fixture-daemon'
    finally:
        backend.close(); server.shutdown(); worker.join(timeout=3); server.server_close()
        upstream.shutdown(); upstream_thread.join(timeout=3); upstream.server_close()
    assert not worker.is_alive() and not upstream_thread.is_alive()


def test_real_backend_create_through_broker_to_synthetic_unix_engine(rig, tmp_path, monkeypatch):
    """Actual adapter backend and socket framing; upstream is deliberately synthetic."""
    from dataclasses import replace
    from http.server import BaseHTTPRequestHandler
    import socketserver
    import time
    from types import SimpleNamespace
    from scripts.hub_local_test_engine_broker import UnixHTTPConnection
    from tests.test_hub_container_executor_daemon_backend import (binding, container_inspect, docker_config,
        make_profile, oci_archive)
    broker, journal, name, body, _ = rig
    manifest, image, blobs = verified_image()
    profile = replace(make_profile(image, secrets={'CODEX_TOKEN': 'synthetic-codex',
        'HERMES_TOKEN': 'synthetic-hermes', 'CLAUDE_TOKEN': 'synthetic-claude'}),
        cpu_millicores=300, pids_limit=80)
    generation_root = Path(broker.mount) / 'generations'
    generation = generation_root / profile.state_generation_id
    generation.mkdir(parents=True, mode=0o700); generation_root.chmod(0o700); generation.chmod(0o700)
    plan = ContainerPlan(name, image, image.runnable_reference, False, profile)
    archive = oci_archive(image, blobs)
    created = []
    observed = container_inspect(image, profile, generation)
    observed['Config']['Labels'] = {'engineering-hub.local-test-owner': broker.owner}
    observed['HostConfig']['PublishAllPorts'] = False
    observed['HostConfig']['Mounts'][0].pop('ReadOnly')
    observed['Mounts'] = observed['Mounts'][:1]; observed['Mounts'][0]['Mode'] = ''
    observed['State'].update(Running=False, Pid=0, StartedAt='0001-01-01T00:00:00Z')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def reply(self, status, value, binary=False):
            data = value if binary else json.dumps(value).encode()
            self.send_response(status); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            path = self.path
            if path.endswith('/info'):
                return self.reply(200, {'ID': 'fixture-daemon', 'OSType': 'linux', 'Architecture': 'amd64',
                                        'DockerRootDir': str(Path(broker.mount).parents[2])})
            if '/volumes/' in path:
                return self.reply(200, {'Name': broker.volume.name, 'Driver': 'local', 'Scope': 'local',
                    'Mountpoint': broker.mount, 'Labels': {'engineering-hub.local-test-owner': broker.owner}, 'Options': {}})
            if '/images/' in path and path.endswith('/json'): return self.reply(200, docker_config(image))
            if '/images/' in path and path.endswith('/get'): return self.reply(200, archive, True)
            if path.endswith('/containers/' + name + '/json'): return self.reply(404, {})
            if path.endswith('/containers/' + 'c' * 64 + '/json'): return self.reply(200, observed)
            if path.endswith('/exec/' + 'f' * 64 + '/json'):
                return self.reply(200, {'ID': 'f' * 64, 'Running': False, 'ExitCode': 1})
            raise AssertionError(path)
        def do_POST(self):
            if self.path == '/v1.41/containers/create?name=' + name:
                received = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert received['Labels'] == {'engineering-hub.local-test-owner': broker.owner
                }
                created.append(received)
                return self.reply(201, {'Id': 'c' * 64, 'Warnings': []})
            if self.path == '/v1.41/containers/' + 'c' * 64 + '/start':
                observed['State'].update(Status='running', Running=True, Pid=123, StartedAt='2026-10-21T00:00:00Z')
                self.send_response(204); self.end_headers(); return
            if self.path == '/v1.41/containers/' + 'c' * 64 + '/exec':
                assert json.loads(self.rfile.read(int(self.headers['Content-Length']))) == broker.exec_bodies[0]
                return self.reply(201, {'Id': 'f' * 64})
            assert self.path == '/v1.41/exec/' + 'f' * 64 + '/start'
            assert json.loads(self.rfile.read(int(self.headers['Content-Length']))) == {'Detach': False, 'Tty': False}
            payload = json.dumps({'expectation_id': 'probe', 'ok': False, 'status': 503}).encode()
            frame = b'\x01\x00\x00\x00' + len(payload).to_bytes(4, 'big') + payload
            self.reply(200, frame, True)
    upstream_path = str(tmp_path / 'engine.sock')
    upstream = socketserver.UnixStreamServer(upstream_path, Handler)
    upstream_thread = threading.Thread(target=upstream.serve_forever); upstream_thread.start()
    monkeypatch.setattr(broker, '_connection', lambda timeout, total: UnixHTTPConnection(upstream_path, timeout, total))
    del broker._attest_volume; del broker._read_bounded
    errors = []
    dispatch = broker.dispatch
    def record_dispatch(*args):
        try: return dispatch(*args)
        except Exception as error:
            errors.append((type(error).__name__, str(error)))
            raise
    monkeypatch.setattr(broker, 'dispatch', record_dispatch)
    path = str(tmp_path / 'broker.sock'); server = LocalTestSocketServer(path, broker)
    worker = threading.Thread(target=server.serve_forever); worker.start()
    backend = DockerEngineBackend(replace(binding(manifest, tmp_path, api_version='1.41'),
        unix_socket_path=path, generation_root=str(generation_root)))
    try:
        try:
            result = backend.create_container(plan, '4c96d3f7-19c5-5ff4-a29e-302f6f661009', 10000)
        except Exception:
            pytest.fail('synthetic broker errors: ' + repr(errors))
        assert result == 'c' * 64
        assert journal.owned('container', 'c' * 64) == (name,) and len(created) == 1
        backend.start_container('c' * 64, 'fa96d3f7-19c5-5ff4-a29e-302f6f661009', 10000)
        assert backend.inspect_container('c' * 64, 3000).status == 'running'
        probe = SimpleNamespace(kind='health_json', path='/health', expected_status=200,
                                expectation_id='probe', max_response_bytes=1024)
        assert backend._one_probe('c' * 64, probe, 1000, time.monotonic() + 5, 'a' * 40) is False
    finally:
        backend.close(); server.shutdown(); worker.join(timeout=3); server.server_close()
        upstream.shutdown(); upstream_thread.join(timeout=3); upstream.server_close()
    assert not worker.is_alive() and not upstream_thread.is_alive()
