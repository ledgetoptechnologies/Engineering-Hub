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
