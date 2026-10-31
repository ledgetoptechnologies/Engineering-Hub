"""Synthetic-only exact OCI fixture enrollment; no Docker or release trust.

Caller exclusively controls the private output directory and supplies the
source/recipe assertions. Archive integrity does not prove build provenance.
"""
from dataclasses import dataclass
import hashlib
import os
import stat
from pathlib import Path
from types import MappingProxyType

from scripts.prepare_local_oci_layout import prepare
from scripts.prepare_hub_unenrolled_record import prepare_record
from hub_container_executor.build_record import verify_image_record


def _digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SyntheticImageFixture:
    record: bytes
    inputs: bytes
    manifest_digest: str
    config_digest: str
    blobs: object

    @property
    def record_digest(self):
        return _digest(self.record)

    @property
    def inputs_digest(self):
        return _digest(self.inputs)


class _Store:
    def __init__(self, values): self._values = values
    def get(self, digest): return self._values[digest]


class _SyntheticTrust:
    def __init__(self, raw): self._raw = raw
    def verify_build_record(self, record_bytes, record, manifest, role):
        return type(record_bytes) is bytes and record_bytes == self._raw


def prepare_fixture_image(stream, output_dir, exact_manifest_digest, *, source_commit,
                          context_digest, recipe_digest, observations, preparation_id):
    """Prepare exactly one OCI graph and a synthetic, explicitly asserted record."""
    outcome = prepare(stream, output_dir, exact_manifest_digest)
    path = Path(output_dir) / 'blobs' / 'sha256'
    def read_metadata(digest):
        fd = os.open(path / digest[7:], os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise ValueError('Prepared metadata exceeds limit')
            data = b''
            while len(data) <= 1024 * 1024:
                block = os.read(fd, min(65536, 1024 * 1024 + 1 - len(data)))
                if not block: break
                data += block
            if len(data) != info.st_size: raise ValueError('Prepared metadata changed')
            return data
        finally:
            os.close(fd)
    manifest_raw = read_metadata(outcome['manifest'])
    config_raw = read_metadata(outcome['config'])
    if (_digest(manifest_raw) != exact_manifest_digest
            or _digest(config_raw) != outcome['config']):
        raise ValueError('Prepared metadata changed')
    draft = prepare_record(source_commit=source_commit, context_digest=context_digest,
        recipe_digest=recipe_digest, observations=observations, manifest_raw=manifest_raw,
        config_raw=config_raw, preparation_id=preparation_id)
    blobs = MappingProxyType({outcome['manifest']:manifest_raw, outcome['config']:config_raw})
    return SyntheticImageFixture(draft.record, draft.inputs, outcome['manifest'], outcome['config'], blobs)


def verify_fixture_image(manifest, role, fixture):
    """Return production VerifiedImage, stores and explicitly synthetic verifier."""
    if type(fixture) is not SyntheticImageFixture:
        raise ValueError('Synthetic image fixture required')
    record_store = _Store({fixture.record_digest: fixture.record})
    metadata_store = _Store(fixture.blobs)
    synthetic_verifier = _SyntheticTrust(fixture.record)
    image = verify_image_record(manifest, role, record_store, metadata_store,
                                synthetic_verifier)
    if image.platform_manifest_digest != fixture.manifest_digest or image.image_config_id != fixture.config_digest:
        raise ValueError('Fixture identity mismatch')
    return image, record_store, metadata_store, synthetic_verifier
