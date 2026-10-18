"""Synthetic receipt reader vectors; no observed health or actual enrollment."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from uuid import uuid4
from contextlib import closing
import os
import sqlite3
import time

import pytest

pytest.importorskip('fcntl', reason='Optional host package requires Linux')

from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.backend import BaselineIdentity
from hub_container_executor.baseline_receipt import ProtectedBaselineProvider, BaselineReceiptError
from hub_container_executor.local_providers import PrivateContentStore
from tests.test_hub_container_executor_build_record import FakeStore, FakeTrust, make_record, verify_fixture


PROFILE = 'sha256:' + 'e' * 64


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


@pytest.fixture
def rig():
    manifest, record, blobs = make_record('rollback')
    image = verify_fixture(manifest, record, blobs, FakeTrust(), role='rollback')
    probes = [probe.model_dump(mode='json') for probe in manifest.probes.rollback]
    row = dict(schema='hub_baseline_receipt_v1', campaign_id=manifest.campaign_id, proof_id=str(uuid4()),
               manifest_digest=manifest.digest(), baseline_id=manifest.state_policy.baseline_id,
               runner=manifest.runner_binding.model_dump(mode='json'),
               rollback=dict(runnable_reference=image.runnable_reference, runnable_media_type=image.runnable_media_type,
                             platform_manifest_digest=image.platform_manifest_digest, image_config_id=image.image_config_id,
                             source_commit=image.source_commit, record_digest=image.record_digest),
               container_id='c' * 64, state_generation_id='baseline-g1',
               backup=dict(digest=manifest.state_policy.baseline_backup_digest, size_bytes=4096),
               runtime_profile_digest=PROFILE, probe_set_digest=digest(canonical(probes)),
               observation=dict(health='healthy', evidence_verified=True, observed_at=1_700_000_000),
               baseline_inventory=[dict(name='hub.sqlite3', size_bytes=4096, digest=digest(b'synthetic-db')),
                                   dict(name='hub.sqlite3-wal', size_bytes=0, digest=digest(b''))])

    def build(value=None, raw=None, **changes):
        raw = canonical(row if value is None else value) if raw is None else raw
        pin = digest(raw)
        store = FakeStore({pin: raw})
        params = dict(receipt_digest=pin, campaign_id=manifest.campaign_id, verified_rollback=image,
                      expected_runtime_profile_digest=PROFILE)
        params.update(changes)
        return ProtectedBaselineProvider(store, **params), store, pin

    return manifest, image, row, build


def test_exact_protected_receipt_derives_identity_and_rechecks_each_call(rig):
    manifest, _, row, build = rig
    provider, store, pin = build()
    expected = BaselineIdentity(row['baseline_id'], row['backup']['digest'], row['container_id'],
                                row['rollback']['image_config_id'], row['state_generation_id'], True, row['proof_id'])
    assert provider.enrolled_baseline(manifest) == expected
    assert provider.verify_baseline(expected, manifest) is True
    assert provider.baseline_inventory(manifest) == tuple((item['name'], item['size_bytes'], item['digest'])
                                                        for item in row['baseline_inventory'])
    store.values[pin] = b'changed but still at requested digest'
    assert provider.verify_baseline(expected, manifest) is False
    with pytest.raises(BaselineReceiptError):
        provider.enrolled_baseline(manifest)
    with pytest.raises(BaselineReceiptError):
        provider.baseline_inventory(manifest)


@pytest.mark.parametrize('field,value', [('schema', 'other'), ('campaign_id', str(uuid4())),
    ('proof_id', 'not-a-uuid'), ('manifest_digest', 'sha256:' + '9' * 64), ('container_id', 'f' * 63),
    ('state_generation_id', '../escape'), ('baseline_id', 'different'), ('runtime_profile_digest', 'sha256:' + '9' * 64),
    ('probe_set_digest', 'sha256:' + '9' * 64), ('environment', {'TOKEN': 'private-sentinel'})])
def test_fully_pinned_but_wrong_schema_or_manifest_binding_denied(rig, field, value):
    manifest, _, row, build = rig
    row = deepcopy(row)
    row[field] = value
    provider, _, _ = build(row)
    with pytest.raises(BaselineReceiptError) as error:
        provider.enrolled_baseline(manifest)
    assert 'private-sentinel' not in str(error.value)


@pytest.mark.parametrize('part,field,value', [
    ('rollback', 'record_digest', 'sha256:' + '9' * 64), ('rollback', 'source_commit', 'f' * 40),
    ('rollback', 'runnable_reference', 'sha256:' + '9' * 64), ('rollback', 'platform_manifest_digest', 'sha256:' + '9' * 64),
    ('rollback', 'image_config_id', 'sha256:' + '9' * 64), ('runner', 'daemon_identity', 'foreign'),
    ('backup', 'digest', 'sha256:' + '9' * 64), ('backup', 'size_bytes', True),
    ('backup', 'size_bytes', 0), ('observation', 'health', 'unhealthy'),
    ('observation', 'evidence_verified', 1), ('observation', 'observed_at', True),
    ('observation', 'observed_at', -1), ('observation', 'command', 'private-sentinel')])
def test_nested_observed_and_identity_fields_are_exact_and_closed(rig, part, field, value):
    manifest, _, row, build = rig
    row = deepcopy(row)
    row[part][field] = value
    provider, _, _ = build(row)
    with pytest.raises(BaselineReceiptError):
        provider.enrolled_baseline(manifest)


@pytest.mark.parametrize('case', ['unsorted', 'duplicate', 'foreign', 'missing_db', 'empty_db', 'negative', 'bool', 'quota', 'extra'])
def test_inventory_closed_sorted_unique_and_bounded(rig, case):
    manifest, _, row, build = rig
    row = deepcopy(row)
    inventory = row['baseline_inventory']
    if case == 'unsorted': inventory.reverse()
    elif case == 'duplicate': inventory.append(deepcopy(inventory[-1]))
    elif case == 'foreign': inventory[-1]['name'] = 'hub.sqlite3.pilot-owner.lock'
    elif case == 'missing_db': inventory.pop(0)
    elif case == 'empty_db': inventory[0]['size_bytes'] = 0
    elif case == 'negative': inventory[-1]['size_bytes'] = -1
    elif case == 'bool': inventory[-1]['size_bytes'] = True
    elif case == 'quota': inventory[0]['size_bytes'] = manifest.retention.max_storage_bytes
    elif case == 'extra': inventory[0]['path'] = '/private/path'
    provider, _, _ = build(row)
    with pytest.raises(BaselineReceiptError):
        provider.enrolled_baseline(manifest)


@pytest.mark.parametrize('case', ['pretty', 'duplicate', 'float', 'nan', 'oversized', 'nonbytes'])
def test_encoding_strict_bounded_canonical(rig, case):
    manifest, _, row, build = rig
    raw = canonical(row)
    if case == 'pretty': raw = json.dumps(row, indent=2).encode()
    elif case == 'duplicate': raw = b'{"schema":"hub_baseline_receipt_v1",' + raw[1:]
    elif case == 'float': raw = raw.replace(b'1700000000', b'1700000000.0')
    elif case == 'nan': raw = raw.replace(b'1700000000', b'NaN')
    elif case == 'oversized': raw = b' ' * 65537
    if case == 'nonbytes':
        provider, store, pin = build()
        store.values[pin] = raw.decode()
    else:
        provider, _, _ = build(raw=raw)
    with pytest.raises(BaselineReceiptError):
        provider.enrolled_baseline(manifest)


def test_constructor_pins_verified_image_not_mutable_caller_alias(rig):
    manifest, image, _, build = rig
    provider, _, _ = build()
    object.__setattr__(image, 'source_commit', 'b' * 40)
    assert provider.enrolled_baseline(manifest).healthy_at_enrollment is True
    with pytest.raises(BaselineReceiptError):
        build(verified_rollback=replace(image, role='candidate'))
    with pytest.raises(BaselineReceiptError):
        build(expected_runtime_profile_digest='untrusted')


def test_changed_manifest_attestation_or_campaign_never_rebinds_receipt(rig):
    manifest, _, _, build = rig
    provider, _, _ = build()
    original = provider.enrolled_baseline(manifest)
    for field in ('attestation', 'campaign'):
        data = manifest.model_dump(mode='json', by_alias=True)
        if field == 'attestation': data['rollback']['provenance']['attestation_digest'] = 'sha256:' + '9' * 64
        else: data['campaign_id'] = str(uuid4())
        changed = PilotManifest.from_bytes(canonical(data))
        assert provider.verify_baseline(original, changed) is False
    assert provider.verify_baseline(replace(original, healthy_at_enrollment=False), manifest) is False
    assert provider.verify_baseline({'healthy_at_enrollment': True}, manifest) is False
    provider.close()
    provider.close()
    assert provider.verify_baseline(original, manifest) is False


def test_actual_private_store_read_reopen_and_tamper_denial(rig, tmp_path):
    manifest, image, row, _ = rig
    root = tmp_path / 'receipts'
    root.mkdir(mode=0o700)
    raw = canonical(row)
    pin = digest(raw)
    path = root / pin[7:]
    path.write_bytes(raw)
    path.chmod(0o600)
    params = dict(receipt_digest=pin, campaign_id=manifest.campaign_id, verified_rollback=image,
                  expected_runtime_profile_digest=PROFILE)
    with PrivateContentStore(root, maximum_bytes=65536, allow_ephemeral_test_storage=True) as store:
        provider = ProtectedBaselineProvider(store, **params)
        baseline = provider.enrolled_baseline(manifest)
        assert provider.verify_baseline(baseline, manifest) is True
    with PrivateContentStore(root, maximum_bytes=65536, allow_ephemeral_test_storage=True) as store:
        provider = ProtectedBaselineProvider(store, **params)
        assert provider.enrolled_baseline(manifest) == baseline
        path.write_bytes(raw + b' ')
        assert provider.verify_baseline(baseline, manifest) is False


def test_future_observation_is_not_historical_enrollment(rig):
    manifest, _, row, build = rig
    row = deepcopy(row)
    row['observation']['observed_at'] = int(time.time()) + 3600
    provider, _, _ = build(row)
    with pytest.raises(BaselineReceiptError):
        provider.enrolled_baseline(manifest)


@pytest.mark.skipif(os.getuid() != 10001 or os.getgid() != 10001, reason='State provider requires actual app UID/GID10001')
def test_protected_reader_feeds_actual_state_provider_without_unbound_inventory(rig, tmp_path):
    """Real private files/copies, synthetic receipt authority—not measured health."""
    from hub_container_executor.state_provider import FrozenSQLiteStateProvisioner
    manifest, image, row, _ = rig
    root, reservations, receipts = (tmp_path / name for name in ('generations', 'reservations', 'receipts'))
    for path in (root, reservations, receipts):
        path.mkdir(mode=0o700)
    live_root = root / row['state_generation_id']
    live_root.mkdir(mode=0o700)
    live = live_root / 'hub.sqlite3'
    with closing(sqlite3.connect(live)) as db:
        db.execute('CREATE TABLE fixture(value TEXT)')
        db.execute("INSERT INTO fixture VALUES ('synthetic private data')")
        db.commit()
    live.chmod(0o600)
    before = live.read_bytes()
    frozen = root / 'frozen-baseline.sqlite3'
    frozen.write_bytes(before)
    frozen.chmod(0o600)
    data = manifest.model_dump(mode='json', by_alias=True)
    data['state_policy']['baseline_backup_digest'] = digest(before)
    manifest = PilotManifest.from_bytes(canonical(data))
    row = deepcopy(row)
    row['manifest_digest'] = manifest.digest()
    row['backup'] = dict(digest=digest(before), size_bytes=len(before))
    row['baseline_inventory'] = [dict(name='hub.sqlite3', size_bytes=len(before), digest=digest(before))]
    raw = canonical(row)
    pin = digest(raw)
    receipt_path = receipts / pin[7:]
    receipt_path.write_bytes(raw)
    receipt_path.chmod(0o600)
    with PrivateContentStore(receipts, maximum_bytes=65536, allow_ephemeral_test_storage=True) as store:
        provider = ProtectedBaselineProvider(store, receipt_digest=pin, campaign_id=manifest.campaign_id,
                                             verified_rollback=image, expected_runtime_profile_digest=PROFILE)
        baseline = provider.enrolled_baseline(manifest)
        state = FrozenSQLiteStateProvisioner(manifest, baseline, baseline_provider=provider,
                    frozen_backup=frozen, generation_root=root, reservation_root=reservations,
                    baseline_generation_inventory=provider.baseline_inventory(manifest),
                    create=True, allow_ephemeral_test_storage=True)
        try:
            run = str(uuid4())
            candidate = state.prepare_candidate(manifest, run, baseline, str(uuid4()),
                                                deadline_monotonic=time.monotonic() + 5)
            rollback = state.prepare_rollback(manifest, run, baseline, str(uuid4()),
                                              deadline_monotonic=time.monotonic() + 5)
            assert candidate.previous_generation_id == rollback.previous_generation_id == baseline.state_generation_id
            for generation in (candidate, rollback):
                path = root / generation.generation_id / 'hub.sqlite3'
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as copied:
                    assert copied.execute('PRAGMA integrity_check').fetchone() == ('ok',)
                    assert copied.execute('SELECT value FROM fixture').fetchall() == [('synthetic private data',)]
                assert generation.generation_digest == digest(path.read_bytes())
            assert live.read_bytes() == frozen.read_bytes() == before
        finally:
            state.close()
