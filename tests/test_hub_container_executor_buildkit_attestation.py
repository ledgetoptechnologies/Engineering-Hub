"""BuildKit attestation descriptors remain opaque to runnable selection."""
import json
from dataclasses import replace

import pytest

from engineering_hub.container_pilot_manifest import Platform
from hub_container_executor.build_record import BuildRecordError, _verify_descriptor_chain
from hub_container_executor.daemon_backend import DaemonBackendError, _archive_image
from tests.test_hub_container_executor_build_record import make_record, canonical, digest, MANIFEST_TYPE, INDEX_TYPE, CONFIG_TYPE
from tests.test_hub_container_executor_daemon_backend import verified_image, json_bytes


def fixture(modify=None):
    _, record_raw, blobs = make_record()
    record = json.loads(record_raw)
    old_root = record['selection']['runnable_reference']
    index = json.loads(blobs.pop(old_root))
    target = index['manifests'][0]
    artifact = {'schemaVersion': 2, 'mediaType': MANIFEST_TYPE,
                'artifactType': 'application/vnd.docker.attestation.manifest.v1+json',
                'subject': {k: target[k] for k in ('mediaType', 'digest', 'size')},
                'config': {'mediaType': 'application/vnd.oci.empty.v1+json',
                           'digest': 'sha256:' + '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
                           'size': 2, 'data': 'e30='},
                'layers': [{'mediaType': 'application/vnd.in-toto+json',
                            'digest': 'sha256:' + 'c8e36816fb9dc811599fce1d7a81770b9df6151c863bd26d8ed06af6fc7cad53',
                            'size': 1499, 'annotations': {
                                'in-toto.io/predicate-type': 'https://slsa.dev/provenance/v1'}}]}
    if modify == 'subject':
        artifact['subject']['digest'] = 'sha256:' + 'f' * 64
    att_raw = canonical(artifact)
    att = {'mediaType': MANIFEST_TYPE, 'digest': digest(att_raw), 'size': len(att_raw),
           'platform': {'os': 'unknown', 'architecture': 'unknown'},
           'annotations': {'vnd.docker.reference.type': 'attestation-manifest',
                           'vnd.docker.reference.digest': target['digest']}}
    att_id = att['digest']
    if modify == 'platform':
        att['platform']['os'] = 'linux'
    if modify == 'reference':
        att['annotations']['vnd.docker.reference.digest'] = 'sha256:' + 'e' * 64
    if modify and modify.startswith('missing_'):
        att.pop(modify.removeprefix('missing_'))
    if modify == 'duplicate_target':
        index['manifests'].append(dict(target))
    index['manifests'].append(att)
    root_raw = canonical(index)
    root = digest(root_raw)
    blobs[root] = root_raw
    blobs[att_id] = att_raw
    record['selection']['runnable_reference'] = root
    media = {root: INDEX_TYPE, att_id: MANIFEST_TYPE,
             target['digest']: MANIFEST_TYPE, record['selection']['image_config_id']: CONFIG_TYPE}
    return record, blobs, media


def run_record(record, blobs, media):
    return _verify_descriptor_chain(record, Platform.model_validate(record['platform']), blobs, media)


def run_archive(record, blobs):
    _, image, _ = verified_image()
    image = replace(image, runnable_reference=record['selection']['runnable_reference'],
                    platform_manifest_digest=record['selection']['platform_manifest_digest'],
                    image_config_id=record['selection']['image_config_id'])
    root_raw = blobs[image.runnable_reference]
    entries = {'oci-layout': json_bytes({'imageLayoutVersion': '1.0.0'}),
               'index.json': json_bytes({'schemaVersion': 2, 'manifests': [
                   {'mediaType': INDEX_TYPE, 'digest': image.runnable_reference, 'size': len(root_raw)}]})}
    entries.update({'blobs/sha256/' + k[7:]: v for k, v in blobs.items()})
    return _archive_image(entries, image)


def test_buildkit_oci_artifact_is_opaque_to_runnable_selection():
    record, blobs, media = fixture()
    assert run_record(record, blobs, media)[0] == record['selection']['platform_manifest_digest']
    assert run_archive(record, blobs)[0]['architecture'] == 'amd64'


@pytest.mark.parametrize('change', ['subject', 'platform', 'reference', 'duplicate_target',
                                    'missing_digest', 'missing_size', 'missing_mediaType',
                                    'missing_platform'])
def test_malicious_or_ambiguous_attestation_is_rejected(change):
    record, blobs, media = fixture(change)
    with pytest.raises(BuildRecordError):
        run_record(record, blobs, media)
    with pytest.raises(DaemonBackendError):
        run_archive(record, blobs)


def test_tampered_attestation_blob_is_rejected_in_archive():
    record, blobs, media = fixture()
    att = next(k for k in blobs if media[k] == MANIFEST_TYPE and k != record['selection']['platform_manifest_digest'])
    blobs[att] += b' '
    with pytest.raises(DaemonBackendError):
        run_archive(record, blobs)

@pytest.mark.parametrize('architecture, accepted', [('arm64', True), ('amd64', False)])
def test_multiple_runnable_platforms_preserve_unique_selection(architecture, accepted):
    record, blobs, media = fixture()
    old_root = record['selection']['runnable_reference']
    index = json.loads(blobs.pop(old_root))
    old_manifest = json.loads(blobs[record['selection']['platform_manifest_digest']])
    old_config = json.loads(blobs[record['selection']['image_config_id']])
    old_config['architecture'] = architecture
    old_config['config']['Env'].append('SECOND_IMAGE=1')
    config_raw = canonical(old_config)
    config_id = digest(config_raw)
    blobs[config_id] = config_raw
    media[config_id] = CONFIG_TYPE
    old_manifest['config']['digest'] = config_id
    old_manifest['config']['size'] = len(config_raw)
    manifest_raw = canonical(old_manifest)
    manifest_id = digest(manifest_raw)
    blobs[manifest_id] = manifest_raw
    media[manifest_id] = MANIFEST_TYPE
    index['manifests'].append({'mediaType': MANIFEST_TYPE, 'digest': manifest_id,
                               'size': len(manifest_raw),
                               'platform': {'os': 'linux', 'architecture': architecture}})
    root_raw = canonical(index)
    root_id = digest(root_raw)
    blobs[root_id] = root_raw
    media.pop(old_root)
    media[root_id] = INDEX_TYPE
    record['selection']['runnable_reference'] = root_id
    if accepted:
        assert run_record(record, blobs, media)[0] == record['selection']['platform_manifest_digest']
        run_archive(record, blobs)
    else:
        with pytest.raises(BuildRecordError):
            run_record(record, blobs, media)
        with pytest.raises(DaemonBackendError):
            run_archive(record, blobs)


def test_signed_record_metadata_contains_artifact_and_rejects_tampering():
    from engineering_hub.container_pilot_manifest import PilotManifest
    from tests.test_hub_container_executor_build_record import FakeTrust, verify_fixture
    from tests.test_hub_container_pilot_manifest import make_manifest_data

    record, blobs, media = fixture()
    record['metadata_blobs'] = [
        {'digest': key, 'media_type': media[key], 'size_bytes': len(raw)}
        for key, raw in blobs.items()
    ]
    record_raw = canonical(record)
    data = make_manifest_data()
    data['candidate']['image_config_id'] = record['selection']['image_config_id']
    data['candidate']['provenance']['attestation_digest'] = digest(record_raw)
    manifest = PilotManifest.from_bytes(canonical(data))
    verified = verify_fixture(manifest, record_raw, blobs, FakeTrust())
    assert verified.platform_manifest_digest == record['selection']['platform_manifest_digest']
    att_id = next(key for key in blobs if media[key] == MANIFEST_TYPE
                  and key != record['selection']['platform_manifest_digest'])
    altered = dict(blobs)
    altered[att_id] = blobs[att_id] + b' '
    with pytest.raises(BuildRecordError, match='metadata blob'):
        verify_fixture(manifest, record_raw, altered, FakeTrust())
