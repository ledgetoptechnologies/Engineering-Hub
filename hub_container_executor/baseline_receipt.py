"""Read-only historical baseline receipts, NOT a health enroller or signer.

The independent operator must observe the real runner/container/probes/backup,
protect the receipt in PrivateContentStore, and independently pin its digest,
campaign, verified rollback image and private runtime-profile digest. Hashing a
caller-authored healthy flag does not establish that authority. Nothing here
probes, writes, snapshots, discovers credentials or activates the adapter.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import math
import time
from uuid import UUID

from engineering_hub.container_pilot_manifest import PilotManifest
from .backend import BaselineIdentity
from .build_record import VerifiedImage
from .runtime_profile import probe_set_digest


class BaselineReceiptError(RuntimeError):
    """Safe denial without receipt bytes, profile environment or paths."""


_SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
_OPAQUE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_CONTAINER = re.compile(r'[a-f0-9]{64}\Z')
_COMMIT = re.compile(r'[a-f0-9]{40}\Z')
_FILES = {'hub.sqlite3', 'hub.sqlite3-wal', 'hub.sqlite3-shm', 'hub.sqlite3-journal'}
_FIELDS = {'schema', 'campaign_id', 'proof_id', 'manifest_digest', 'baseline_id', 'runner', 'rollback',
           'container_id', 'state_generation_id', 'backup', 'runtime_profile_digest', 'probe_set_digest',
           'observation', 'baseline_inventory'}


def _require(value):
    if not value:
        raise BaselineReceiptError('Protected baseline receipt unavailable')


def _sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _uuid(value):
    return type(value) is str and str(UUID(value)) == value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def _strict(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def reject(value):
        raise ValueError
    result = json.loads(raw, object_pairs_hook=unique, parse_float=reject, parse_constant=reject)
    _require(_canonical(result) == raw)
    return result


class ProtectedBaselineProvider:
    """Explicit immutable composition over independently protected receipt bytes.

    Use PrivateContentStore(maximum_bytes=65536) in production. The injected
    store's get() contract permits pure synthetic unit tests, not trust in an
    arbitrary peer provider. Caller owns store lifecycle; close() revokes this
    reader only. No automatic renewal or live-health/expiry claim is made.
    """

    def __init__(self, store, *, receipt_digest, campaign_id, verified_rollback,
                 expected_runtime_profile_digest):
        try:
            _require(callable(getattr(store, 'get', None)) and _sha(receipt_digest)
                     and _uuid(campaign_id) and _sha(expected_runtime_profile_digest)
                     and type(verified_rollback) is VerifiedImage and verified_rollback.role == 'rollback')
            image = verified_rollback
            _require(all(_sha(value) for value in (image.runnable_reference, image.platform_manifest_digest,
                         image.image_config_id, image.record_digest))
                     and type(image.source_commit) is str and _COMMIT.fullmatch(image.source_commit) is not None
                     and image.runnable_media_type in {'application/vnd.oci.image.index.v1+json',
                         'application/vnd.oci.image.manifest.v1+json',
                         'application/vnd.docker.distribution.manifest.list.v2+json',
                         'application/vnd.docker.distribution.manifest.v2+json'})
            self._rollback = dict(runnable_reference=image.runnable_reference,
                                 runnable_media_type=image.runnable_media_type,
                                 platform_manifest_digest=image.platform_manifest_digest,
                                 image_config_id=image.image_config_id, source_commit=image.source_commit,
                                 record_digest=image.record_digest)
            self._platform = _strict(_canonical(image.platform.model_dump(mode='json')))
            self._store, self._receipt_digest, self._campaign = store, receipt_digest, campaign_id
            self._profile = expected_runtime_profile_digest
            self._closed, self._pid = False, os.getpid()
        except Exception:
            raise BaselineReceiptError('Independent baseline enrollment pins required') from None

    def _read(self, manifest):
        try:
            _require(not self._closed and self._pid == os.getpid() and type(manifest) is PilotManifest)
            manifest = PilotManifest.from_bytes(manifest.canonical_bytes())
            _require(manifest.campaign_id == self._campaign
                     and self._rollback['record_digest'] == manifest.rollback.provenance.attestation_digest
                     and self._rollback['image_config_id'] == manifest.rollback.image_config_id
                     and self._rollback['source_commit'] == manifest.rollback.source_commit
                     and manifest.runner_binding.platform.model_dump(mode='json') == self._platform)
            raw = self._store.get(self._receipt_digest)
            _require(type(raw) is bytes and 1 <= len(raw) <= 65536
                     and 'sha256:' + hashlib.sha256(raw).hexdigest() == self._receipt_digest)
            row = _strict(raw)
            _require(type(row) is dict and set(row) == _FIELDS
                     and row['schema'] == 'hub_baseline_receipt_v1'
                     and row['campaign_id'] == self._campaign and _uuid(row['proof_id'])
                     and row['manifest_digest'] == manifest.digest()
                     and row['baseline_id'] == manifest.state_policy.baseline_id
                     and row['runner'] == manifest.runner_binding.model_dump(mode='json')
                     and row['rollback'] == self._rollback
                     and type(row['container_id']) is str and _CONTAINER.fullmatch(row['container_id']) is not None
                     and type(row['state_generation_id']) is str and _OPAQUE.fullmatch(row['state_generation_id']) is not None
                     and row['runtime_profile_digest'] == self._profile
                     and row['probe_set_digest'] == probe_set_digest(manifest.probes.rollback))
            backup, observation, inventory = row['backup'], row['observation'], row['baseline_inventory']
            maximum = manifest.retention.max_storage_bytes
            now = time.time()
            _require(type(backup) is dict and set(backup) == {'digest', 'size_bytes'}
                     and backup['digest'] == manifest.state_policy.baseline_backup_digest
                     and type(backup['size_bytes']) is int and 1 <= backup['size_bytes'] <= maximum
                     and type(observation) is dict and set(observation) == {'health', 'evidence_verified', 'observed_at'}
                     and observation['health'] == 'healthy' and observation['evidence_verified'] is True
                     and type(observation['observed_at']) is int and 0 <= observation['observed_at'] <= 2**53
                     and type(now) in {int, float} and math.isfinite(now) and observation['observed_at'] <= now
                     and type(inventory) is list and 1 <= len(inventory) <= 4)
            names, total = [], backup['size_bytes']
            for entry in inventory:
                _require(type(entry) is dict and set(entry) == {'name', 'size_bytes', 'digest'}
                         and type(entry['name']) is str and entry['name'] in _FILES
                         and type(entry['size_bytes']) is int and 0 <= entry['size_bytes'] <= maximum
                         and _sha(entry['digest']))
                _require(entry['name'] != 'hub.sqlite3' or entry['size_bytes'] > 0)
                names.append(entry['name'])
                total += entry['size_bytes']
            _require(names == sorted(set(names)) and 'hub.sqlite3' in names and total <= maximum)
            return row
        except Exception:
            raise BaselineReceiptError('Protected baseline receipt unavailable') from None

    @staticmethod
    def _identity(row):
        return BaselineIdentity(row['baseline_id'], row['backup']['digest'], row['container_id'],
                                row['rollback']['image_config_id'], row['state_generation_id'], True, row['proof_id'])

    def enrolled_baseline(self, manifest):
        return self._identity(self._read(manifest))

    def verify_baseline(self, baseline, manifest):
        try:
            return type(baseline) is BaselineIdentity and baseline == self._identity(self._read(manifest))
        except Exception:
            return False

    def baseline_inventory(self, manifest):
        """Immutable file triples from the same validated, re-read receipt."""
        return tuple((entry['name'], entry['size_bytes'], entry['digest'])
                     for entry in self._read(manifest)['baseline_inventory'])

    def close(self):
        """Explicitly revoke this reader, without closing caller-owned storage."""
        self._closed = True
