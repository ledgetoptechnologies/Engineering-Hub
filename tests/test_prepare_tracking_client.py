import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from scripts.prepare_tracking_client import _FILES, prepare_archive

REVISION = 'a' * 40


def archive(*, commit=REVISION, omit=None, extra=()):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w', format=tarfile.PAX_FORMAT,
                      pax_headers={'comment': commit}) as tar:
        for name in _FILES:
            if name == omit:
                continue
            raw = ('content:' + name).encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        for name in extra:
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b'x'))
    return data.getvalue()


def test_allowlisted_context_and_provenance(tmp_path):
    raw = archive()
    output = prepare_archive(raw, revision=REVISION, output_directory=tmp_path / 'context')
    provenance = json.loads((output / 'src/engineering_hub/_tracking_client_source.json').read_text())
    assert provenance['source_commit'] == REVISION
    assert provenance['source_archive_sha256'] == hashlib.sha256(raw).hexdigest()
    assert set(provenance['source_files']) == set(_FILES.values())
    for name, digest in provenance['source_files'].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError):
        prepare_archive(raw, revision=REVISION, output_directory=output)


@pytest.mark.parametrize('raw,revision', [
    (archive(commit='b' * 40), REVISION),
    (archive(omit='src/engineering_hub/tracking_client.py'), REVISION),
    (archive(extra=('../escape',)), REVISION),
    (archive(extra=('src/engineering_hub/tracking_client.py',)), REVISION),
    (archive(), 'HEAD'),
], ids=['wrong-commit', 'missing-file', 'unsafe-path', 'duplicate', 'invalid-revision'])
def test_rejects_bad_inputs_without_output(tmp_path, raw, revision):
    output = tmp_path / 'context'
    with pytest.raises(ValueError):
        prepare_archive(raw, revision=revision, output_directory=output)
    assert not output.exists()


def test_rejects_symlink_destination(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    link = tmp_path / 'context'
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(ValueError):
        prepare_archive(archive(), revision=REVISION, output_directory=link)
