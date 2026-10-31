"""Authenticated post-exit handoff for a trusted local test fixture only.

The receipt does not prove host-wide custody or exclude other daemon clients.
The launcher must independently verify coordinator exit/removal, all container
IDs absent, daemon/volume identity and one-shot removal before deleting volume.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat

from scripts.hub_local_acceptance_components import LocalSource, LocalSuccessor
from scripts.hub_local_native_coordinator import LocalNativeCoordinator
from scripts.hub_local_test_engine_broker import CleanedBrokerSnapshot
from scripts.hub_local_test_volume import TMPFS_OPTIONS

DOMAIN = b'engineering-hub.local-cleanup-receipt.v1\0'
SCHEMA = 'engineering-hub.local-cleanup-receipt.v1'


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')


def write_cleanup_receipt(*, directory, key, nonce, coordinator_id, coordinator_image_id,
                          initial_manifest_digest, baseline_backup_digest, manifest_digest,
                          coordinator, source, successor, cleaned):
    """Write exactly once after all in-process owners have successfully drained."""
    if (type(key) is not bytes or len(key) != 32 or type(nonce) is not str or not re.fullmatch('[a-f0-9]{64}',nonce)
            or type(coordinator_id) is not str or not re.fullmatch('[a-f0-9]{64}',coordinator_id)
            or type(coordinator_image_id) is not str or not re.fullmatch('sha256:[a-f0-9]{64}',coordinator_image_id)
            or type(initial_manifest_digest) is not str or not re.fullmatch('sha256:[a-f0-9]{64}',initial_manifest_digest)
            or type(baseline_backup_digest) is not str or not re.fullmatch('sha256:[a-f0-9]{64}',baseline_backup_digest)
            or type(manifest_digest) is not str or not re.fullmatch('sha256:[a-f0-9]{64}',manifest_digest)
            or type(coordinator) is not LocalNativeCoordinator or coordinator._pid != os.getpid() or not coordinator._closed
            or type(source) is not LocalSource or source._pid != os.getpid() or source._disposed is not True or source.thread.is_alive()
            or type(successor) is not LocalSuccessor or successor._pid != os.getpid() or successor._closed is not True
            or successor.thread.is_alive() or type(cleaned) is not CleanedBrokerSnapshot
            or successor._cleaned_snapshot is not cleaned or coordinator.client.adapter is not successor._adapter
            or successor.source_broker is not source.broker or source.volume != successor.volume
            or source.manifest.digest() != initial_manifest_digest
            or successor.manifest.state_policy.baseline_backup_digest != baseline_backup_digest
            or successor.manifest.digest() != manifest_digest
            or cleaned.owner != successor.volume.owner or cleaned.daemon_id != successor.volume.daemon_id
            or cleaned.volume_name != successor.volume.name or cleaned.volume_mount != successor.volume.mountpoint):
        raise ValueError('Local cleanup handoff unavailable')
    if successor.volume.options not in ((),TMPFS_OPTIONS):
        raise ValueError('Local cleanup storage unavailable')
    from scripts.hub_local_baseline_bootstrap import _final_manifest
    if _final_manifest(source.manifest,baseline_backup_digest).canonical_bytes() != successor.manifest.canonical_bytes():
        raise ValueError('Local cleanup manifest mismatch')
    root = Path(cleaned.volume_mount).parents[2]
    entries = [list(item) for item in cleaned.containers]
    if (not 1 <= len(entries) <= 3 or entries != sorted(entries) or
            any(len(item) != 2 or type(item[0]) is not str or not re.fullmatch('[a-f0-9]{64}',item[0])
                or type(item[1]) is not str or not item[1].startswith('hub-pilot-') for item in entries)):
        raise ValueError('Local cleanup inventory invalid')
    payload = {'schema':SCHEMA,'task_id':cleaned.owner,'daemon_id':cleaned.daemon_id,
        'docker_root':str(root),'volume_name':cleaned.volume_name,'volume_mount':cleaned.volume_mount,
        'volume_storage':'tmpfs-512m-v1' if successor.volume.options else 'local-v1',
        'volume_options':dict(successor.volume.options),
        'coordinator_id':coordinator_id,'coordinator_image_id':coordinator_image_id,
        'initial_manifest_digest':initial_manifest_digest,'baseline_backup_digest':baseline_backup_digest,
        'manifest_digest':manifest_digest,'nonce':nonce,'containers':entries}
    envelope = {'payload':payload,'signature':hmac.new(key,DOMAIN+_canonical(payload),hashlib.sha256).hexdigest()}
    raw = _canonical(envelope)
    path = Path(directory)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('Local cleanup directory unavailable')
    parent = os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    fd = None
    try:
        info = os.fstat(parent)
        named_parent = os.stat(path,follow_symlinks=False)
        if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
                or not stat.S_ISDIR(named_parent.st_mode)
                or (info.st_dev,info.st_ino) != (named_parent.st_dev,named_parent.st_ino)):
            raise ValueError('Local cleanup directory unavailable')
        fd = os.open('cleanup-receipt.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,
                     0o600,dir_fd=parent)
        offset = 0
        while offset < len(raw):
            written = os.write(fd,raw[offset:])
            if written <= 0: raise OSError('short receipt write')
            offset += written
        os.fsync(fd)
        held = os.fstat(fd); named = os.stat('cleanup-receipt.json',dir_fd=parent,follow_symlinks=False)
        if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid() or held.st_nlink != 1
                or stat.S_IMODE(held.st_mode) != 0o600 or (held.st_dev,held.st_ino) != (named.st_dev,named.st_ino)):
            raise ValueError('Local cleanup receipt changed')
        os.fsync(parent)
        named_parent = os.stat(path,follow_symlinks=False)
        if (info.st_dev,info.st_ino) != (named_parent.st_dev,named_parent.st_ino):
            raise ValueError('Local cleanup directory changed')
    finally:
        try:
            if fd is not None: os.close(fd)
        finally:
            os.close(parent)
    return payload
