import hashlib
import io
import json
import tarfile

import pytest
from scripts.prepare_local_oci_layout import prepare


def encoded(value):
    return json.dumps(value, separators=(',', ':')).encode()


def fixture():
    blobs = {}
    def put(data, media):
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        blobs[digest[7:]] = data
        return {'mediaType': media, 'digest': digest, 'size': len(data)}
    config = put(encoded({'os': 'linux', 'architecture': 'amd64'}), 'application/vnd.oci.image.config.v1+json')
    layer = put(b'synthetic-layer', 'application/vnd.oci.image.layer.v1.tar+gzip')
    manifest = put(encoded({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                            'config': config, 'layers': [layer]}), 'application/vnd.oci.image.manifest.v1+json')
    rows = [('oci-layout', encoded({'imageLayoutVersion': '1.0.0'})),
            ('index.json', encoded({'schemaVersion': 2, 'manifests': [manifest]}))]
    rows += [('blobs/sha256/' + digest, data) for digest, data in blobs.items()]
    return rows, manifest['digest']


def archive(rows):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode='w') as tar:
        for name, data in rows:
            info = tarfile.TarInfo(name)
            if data is None or data == 'hardlink':
                info.type = tarfile.LNKTYPE if data == 'hardlink' else tarfile.SYMTYPE
                info.linkname = '/tmp/escape'
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    result.seek(0)
    return result


def test_prepares_exact_manifest(tmp_path):
    rows, digest = fixture()
    output = tmp_path / 'new'
    assert prepare(archive(rows), output, digest)['layers'] == 1
    index = json.loads((output / 'index.json').read_bytes())
    assert index['manifests'][0]['digest'] == digest


@pytest.mark.parametrize('unsupported', ['platform', 'dir_fd', 'nofollow', 'directory', 'follow_symlinks'])
def test_unsupported_capabilities_fail_before_output_creation(tmp_path, monkeypatch, unsupported):
    from scripts import prepare_local_oci_layout as helper
    rows, digest = fixture()
    if unsupported == 'platform':
        monkeypatch.setattr(helper.sys, 'platform', 'win32')
    elif unsupported == 'dir_fd':
        monkeypatch.setattr(helper.os, 'supports_dir_fd', set())
    elif unsupported == 'nofollow':
        monkeypatch.delattr(helper.os, 'O_NOFOLLOW')
    elif unsupported == 'directory':
        monkeypatch.delattr(helper.os, 'O_DIRECTORY')
    else:
        monkeypatch.setattr(helper.os, 'supports_follow_symlinks', set())
    output = tmp_path / 'new'
    with pytest.raises(RuntimeError, match='no output created'):
        prepare(archive(rows), output, digest)
    assert not output.exists()


@pytest.mark.parametrize('change', ['traversal', 'duplicate', 'symlink', 'hardlink', 'corrupt', 'missing', 'wrong_digest'])
def test_rejects_untrusted_archive_and_preserves_partial_output(tmp_path, change):
    rows, digest = fixture()
    if change == 'traversal': rows.append(('../escape', b'x'))
    elif change == 'duplicate': rows.append(rows[0])
    elif change == 'symlink': rows.append(('link', None))
    elif change == 'hardlink': rows.append(('link', 'hardlink'))
    elif change == 'corrupt': rows[-1] = (rows[-1][0], b'altered')
    elif change == 'missing': rows.pop()
    else: digest = 'sha256:' + '0' * 64
    output = tmp_path / 'new'
    with pytest.raises((ValueError, KeyError)):
        prepare(archive(rows), output, digest)
    assert output.is_dir()
    assert not (tmp_path / 'escape').exists()


def test_never_adopts_existing_output(tmp_path):
    rows, digest = fixture()
    marker = tmp_path / 'marker'
    marker.write_text('keep')
    with pytest.raises(FileExistsError):
        prepare(archive(rows), tmp_path, digest)
    assert marker.read_text() == 'keep'


def test_unspecified_platform_among_multiple_entries_is_rejected(tmp_path):
    rows, digest = fixture()
    index = json.loads(rows[1][1])
    index['manifests'].append(dict(index['manifests'][0], digest='sha256:' + '0' * 64))
    rows[1] = ('index.json', encoded(index))
    with pytest.raises(ValueError):
        prepare(archive(rows), tmp_path / 'new', digest)


def test_conflicting_ancestor_platform_is_rejected(tmp_path):
    rows, digest = fixture()
    nested = rows[1][1]
    nested_digest = 'sha256:' + hashlib.sha256(nested).hexdigest()
    rows[1] = ('index.json', encoded({'schemaVersion': 2, 'manifests': [{
        'mediaType': 'application/vnd.oci.image.index.v1+json', 'digest': nested_digest,
        'size': len(nested), 'platform': {'os': 'linux', 'architecture': 'arm64'}}]}))
    rows.append(('blobs/sha256/' + nested_digest[7:], nested))
    with pytest.raises(ValueError):
        prepare(archive(rows), tmp_path / 'new', digest)


def test_oversized_pax_header_rejected_before_payload_read(tmp_path):
    info = tarfile.TarInfo('extension')
    info.type = tarfile.XHDTYPE
    info.size = 1024 * 1024 + 1
    raw = io.BytesIO(info.tobuf() + b'x' * 1024)
    with pytest.raises(ValueError, match='extension too large'):
        prepare(raw, tmp_path / 'new', 'sha256:' + '0' * 64)


def test_metadata_collision_does_not_replace_existing_file(tmp_path, monkeypatch):
    import os
    rows, digest = fixture()
    output = tmp_path / 'new'
    original = os.open
    def inject(path, flags, *args, **kwargs):
        if path == 'oci-layout':
            (output / 'oci-layout').write_bytes(b'preserve')
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', inject)
    with pytest.raises(FileExistsError):
        prepare(archive(rows), output, digest)
    assert (output / 'oci-layout').read_bytes() == b'preserve'


@pytest.mark.parametrize('target', ['manifest', 'config', 'layer'])
def test_same_size_blob_replacement_before_return_is_rejected(tmp_path, monkeypatch, target):
    import os
    rows, digest = fixture()
    index = json.loads(rows[1][1])
    image = json.loads(next(data for name, data in rows if name.endswith(digest[7:])))
    selected = {'manifest': digest, 'config': image['config']['digest'],
                'layer': image['layers'][0]['digest']}[target]
    output = tmp_path / 'new'
    original = os.open
    def replace_before_index(path, flags, *args, **kwargs):
        if path == 'index.json':
            blob = output / 'blobs' / 'sha256' / selected[7:]
            size = blob.stat().st_size
            replacement = output / 'replacement'
            replacement.write_bytes(b'x' * size)
            os.replace(replacement, blob)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', replace_before_index)
    with pytest.raises(ValueError, match='blob changed'):
        prepare(archive(rows), output, digest)
