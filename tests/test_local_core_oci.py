import hashlib
import json

import pytest

from scripts.verify_local_core_oci import verify_layout


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def make_layout(tmp_path, *, revision='a' * 40, user='568:568', arch='amd64'):
    root = tmp_path / 'oci'
    blobs = root / 'blobs' / 'sha256'
    blobs.mkdir(parents=True)
    (root / 'oci-layout').write_bytes(_raw({'imageLayoutVersion': '1.0.0'}))

    def blob(data, media):
        digest = hashlib.sha256(data).hexdigest()
        (blobs / digest).write_bytes(data)
        return {'mediaType': media, 'digest': 'sha256:' + digest, 'size': len(data)}

    layer = blob(b'synthetic-compressed-layer', 'application/vnd.oci.image.layer.v1.tar+gzip')
    config = blob(_raw({'architecture': arch, 'os': 'linux',
                        'config': {'User': user, 'Labels': {'org.opencontainers.image.revision': revision}}}),
                  'application/vnd.oci.image.config.v1+json')
    manifest = blob(_raw({'schemaVersion': 2, 'config': config, 'layers': [layer]}),
                    'application/vnd.oci.image.manifest.v1+json')
    manifest['platform'] = {'os': 'linux', 'architecture': 'amd64'}
    (root / 'index.json').write_bytes(_raw({'schemaVersion': 2, 'manifests': [manifest]}))
    args = dict(layout=root, core_image='local.test/hub@' + manifest['digest'],
                context_digest=manifest['digest'], source_commit=revision)
    return args, layer, manifest


def test_verified_layout_requires_matching_config_and_all_layer_bytes(tmp_path):
    args, layer, _ = make_layout(tmp_path)
    value = verify_layout(**args)
    assert value['context_digest'] == args['context_digest'] and value['layers'] == 1
    (args['layout'] / 'blobs' / 'sha256' / layer['digest'][7:]).write_bytes(b'X' * layer['size'])
    with pytest.raises(ValueError, match='differs from descriptor'):
        verify_layout(**args)


def test_distinct_core_and_named_context_digests_fail_closed(tmp_path):
    args, _, _ = make_layout(tmp_path)
    args['context_digest'] = 'sha256:' + '0' * 64
    with pytest.raises(ValueError, match='binding'):
        verify_layout(**args)


@pytest.mark.parametrize('changes', [{'user': 'root'}, {'arch': 'arm64'}])
def test_manifest_config_is_not_accepted_for_wrong_identity(tmp_path, changes):
    args, _, _ = make_layout(tmp_path, **changes)
    with pytest.raises(ValueError, match='config identity'):
        verify_layout(**args)


def test_duplicate_selected_manifest_is_not_accepted(tmp_path):
    args, _, manifest = make_layout(tmp_path)
    (args['layout'] / 'index.json').write_bytes(_raw({'schemaVersion': 2, 'manifests': [manifest, manifest]}))
    with pytest.raises(ValueError, match='uniquely present'):
        verify_layout(**args)


def test_wrong_source_revision_is_not_accepted(tmp_path):
    args, _, _ = make_layout(tmp_path)
    args['source_commit'] = 'b' * 40
    with pytest.raises(ValueError, match='identity'):
        verify_layout(**args)


def test_manifest_inside_pinned_nested_index_is_checked(tmp_path):
    args, _, manifest = make_layout(tmp_path)
    raw = _raw({'schemaVersion': 2, 'manifests': [manifest]})
    digest = hashlib.sha256(raw).hexdigest()
    (args['layout'] / 'blobs' / 'sha256' / digest).write_bytes(raw)
    descriptor = {'mediaType': 'application/vnd.oci.image.index.v1+json',
                  'digest': 'sha256:' + digest, 'size': len(raw)}
    (args['layout'] / 'index.json').write_bytes(_raw({'schemaVersion': 2, 'manifests': [descriptor]}))
    assert verify_layout(**args)['context_digest'] == manifest['digest']
    (args['layout'] / 'blobs' / 'sha256' / digest).write_bytes(b'X' * len(raw))
    with pytest.raises(ValueError, match='differs from descriptor'):
        verify_layout(**args)


def test_missing_referenced_layer_fails(tmp_path):
    args, layer, _ = make_layout(tmp_path)
    (args['layout'] / 'blobs' / 'sha256' / layer['digest'][7:]).unlink()
    with pytest.raises(OSError):
        verify_layout(**args)
