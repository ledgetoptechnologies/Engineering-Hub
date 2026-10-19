"""Small synthetic OCI tar fixtures only; no Docker or host effects."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

from scripts.hub_validation_local_smoke import selection_from_archive
from hub_validation_worker.docker_backend import DockerValidationError


INDEX_TYPE = 'application/vnd.oci.image.index.v1+json'
MANIFEST_TYPE = 'application/vnd.oci.image.manifest.v1+json'
CONFIG_TYPE = 'application/vnd.oci.image.config.v1+json'


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def name(identity):
    return 'blobs/sha256/' + identity.removeprefix('sha256:')


def encoded(value):
    return json.dumps(value, separators=(',', ':')).encode()


def chain(*, index_mutator=None, index_padding=0, manifest_padding=0):
    config = encoded({'architecture': 'amd64', 'os': 'linux', 'rootfs': {'type': 'layers', 'diff_ids': []}})
    manifest = encoded({'schemaVersion': 2, 'mediaType': MANIFEST_TYPE,
                        'config': {'mediaType': CONFIG_TYPE, 'digest': digest(config), 'size': len(config)},
                        'layers': []})
    manifest += b' ' * manifest_padding
    index_data = {'schemaVersion': 2, 'mediaType': INDEX_TYPE,
                  'manifests': [{'mediaType': MANIFEST_TYPE, 'digest': digest(manifest),
                                 'size': len(manifest),
                                 'platform': {'os': 'linux', 'architecture': 'amd64'}}]}
    if index_mutator:
        index_mutator(index_data)
    index = encoded(index_data) + b' ' * index_padding
    return index, manifest, config


def archive(tmp_path, entries, filename='fixture.tar'):
    path = tmp_path / filename
    with tarfile.open(path, 'w', format=tarfile.USTAR_FORMAT) as output:
        for entry, raw in entries:
            member = entry if isinstance(entry, tarfile.TarInfo) else tarfile.TarInfo(entry)
            if member.isfile():
                member.size = len(raw)
            output.addfile(member, io.BytesIO(raw) if member.isfile() else None)
    return path


def regular_entries(index, manifest, config):
    return [(name(digest(index)), index), (name(digest(manifest)), manifest),
            (name(digest(config)), config)]


def test_original_oci_metadata_chain_uses_original_bytes(tmp_path):
    index, manifest, config = chain(index_padding=7, manifest_padding=11)
    selected = selection_from_archive(archive(tmp_path, regular_entries(index, manifest, config)), digest(index))
    assert selected.index_bytes == index and selected.manifest_bytes == manifest
    assert selected.runnable_reference == digest(index)
    assert selected.platform_manifest_digest == digest(manifest)
    assert selected.image_config_id == digest(config)
    assert selected.platform == 'linux/amd64'


@pytest.mark.parametrize('which', ['index', 'manifest'])
def test_referenced_blob_hash_mismatch_denied(tmp_path, which):
    index, manifest, config = chain()
    entries = regular_entries(index, manifest, config)
    position = 0 if which == 'index' else 1
    entries[position] = (entries[position][0], entries[position][1] + b' ')
    with pytest.raises(RuntimeError, match='digest mismatch'):
        selection_from_archive(archive(tmp_path, entries), digest(index))


@pytest.mark.parametrize('which', ['index', 'manifest'])
def test_duplicate_referenced_entries_denied_even_identical(tmp_path, which):
    index, manifest, config = chain()
    entries = regular_entries(index, manifest, config)
    entries.append(entries[0 if which == 'index' else 1])
    with pytest.raises(RuntimeError, match='Bounded original OCI blob required'):
        selection_from_archive(archive(tmp_path, entries), digest(index))


@pytest.mark.parametrize('which', ['index', 'manifest'])
@pytest.mark.parametrize('member_type', [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE,
                                        tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE])
def test_referenced_nonregular_and_link_entries_denied(tmp_path, which, member_type):
    index, manifest, config = chain()
    entries = regular_entries(index, manifest, config)
    position = 0 if which == 'index' else 1
    member = tarfile.TarInfo(entries[position][0])
    member.type = member_type
    member.linkname = 'unrelated-original'
    entries[position] = (member, b'')
    entries.append(('unrelated-original', index if which == 'index' else manifest))
    with pytest.raises(RuntimeError, match='Bounded original OCI blob required'):
        selection_from_archive(archive(tmp_path, entries), digest(index))


@pytest.mark.parametrize('size', [0, 65537])
def test_index_blob_size_bound(tmp_path, size):
    raw = b'x' * size
    path = archive(tmp_path, [(name(digest(raw)), raw)])
    with pytest.raises(RuntimeError, match='Bounded original OCI blob required'):
        selection_from_archive(path, digest(raw))


def test_manifest_blob_size_bound(tmp_path):
    index, manifest, config = chain(manifest_padding=65536)
    with pytest.raises(RuntimeError, match='Bounded original OCI blob required'):
        selection_from_archive(archive(tmp_path, regular_entries(index, manifest, config)), digest(index))


def test_maximum_bounded_metadata_blob_sizes_are_accepted(tmp_path):
    _, first_manifest, _ = chain()
    _, manifest, config = chain(manifest_padding=65536 - len(first_manifest))
    # Descriptor size/digest changes alter index length; construct it first.
    index = encoded({'schemaVersion': 2, 'mediaType': INDEX_TYPE,
                     'manifests': [{'mediaType': MANIFEST_TYPE, 'digest': digest(manifest),
                                    'size': len(manifest), 'platform': {'os': 'linux', 'architecture': 'amd64'}}]})
    index += b' ' * (65536 - len(index))
    selected = selection_from_archive(archive(tmp_path, regular_entries(index, manifest, config)), digest(index))
    assert len(selected.index_bytes) == len(selected.manifest_bytes) == 65536


@pytest.mark.parametrize('mutator', [lambda value: value['manifests'].append(deepcopy(value['manifests'][0])),
                                    lambda value: value['manifests'][0]['platform'].update(architecture='arm64'),
                                    lambda value: value.update(manifests=[])])
def test_missing_or_nonunique_platform_denied(tmp_path, mutator):
    index, manifest, config = chain(index_mutator=mutator)
    with pytest.raises(RuntimeError, match='Unique amd64 fixture required'):
        selection_from_archive(archive(tmp_path, regular_entries(index, manifest, config)), digest(index))


def test_member_count_limit(tmp_path):
    index, manifest, config = chain()
    entries = regular_entries(index, manifest, config)
    entries.extend(('unrelated-' + str(position), b'') for position in range(510))
    with pytest.raises(RuntimeError, match='too many entries'):
        selection_from_archive(archive(tmp_path, entries), digest(index))


def test_exact_member_count_limit_accepts_original_chain(tmp_path):
    index, manifest, config = chain()
    entries = regular_entries(index, manifest, config)
    entries.extend(('unrelated-' + str(position), b'') for position in range(509))
    assert selection_from_archive(archive(tmp_path, entries), digest(index)).image_config_id == digest(config)


def test_member_scan_stops_before_materializing_whole_archive(tmp_path, monkeypatch):
    path = tmp_path / 'bounded-scan.tar'
    path.touch()
    scanned = []
    class SyntheticArchive:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def getmembers(self):
            raise AssertionError('unbounded archive member materialization')
        def __iter__(self):
            for position in range(514):
                if position == 513:
                    raise AssertionError('entry limit failed to stop parser')
                scanned.append(position)
                yield tarfile.TarInfo('synthetic-' + str(position))
    monkeypatch.setattr(tarfile, 'open', lambda *args, **kwargs: SyntheticArchive())
    with pytest.raises(RuntimeError, match='too many entries'):
        selection_from_archive(path, 'sha256:' + 'a' * 64)
    assert len(scanned) == 513


def test_archive_total_size_denied_before_open(tmp_path, monkeypatch):
    path = tmp_path / 'oversized.tar'
    path.touch()
    original = Path.stat
    def fake_stat(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self == path:
            values = list(result)
            values[6] = 2 * 1024**3 + 1
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path, 'stat', fake_stat)
    def no_open(*args, **kwargs):
        raise AssertionError('oversized archive was opened')
    monkeypatch.setattr(tarfile, 'open', no_open)
    with pytest.raises(RuntimeError, match='Bounded local fixture archive required'):
        selection_from_archive(path, 'sha256:' + 'a' * 64)


@pytest.mark.parametrize('index', ['a' * 64, 'sha256:' + 'A' * 64, 'sha256:../index', ''])
def test_invalid_expected_index_denied(tmp_path, index):
    with pytest.raises(RuntimeError, match='Invalid fixture index'):
        selection_from_archive(tmp_path / 'unused.tar', index)


def test_nonregular_archive_path_denied(tmp_path):
    with pytest.raises(RuntimeError, match='Bounded local fixture archive required'):
        selection_from_archive(tmp_path, 'sha256:' + 'a' * 64)


def test_invalid_original_metadata_shape_is_not_reserialized_or_fixed(tmp_path):
    index, manifest, config = chain()
    malformed = b'{"schemaVersion":2,"schemaVersion":2,' + index[1:].split(b',', 1)[1]
    with pytest.raises(DockerValidationError):
        selection_from_archive(archive(tmp_path, regular_entries(malformed, manifest, config)), digest(malformed))
