"""Local-test-only child baseline preparation using the actual owner and observer.

Synthetic fixture input and trusted local operator only. The child must run as
UID/GID 10001 with exactly one task; it owns a separately enrolled Hub DB. No
retry, cleanup or publication occurs after any uncertain Docker effect.
"""
from dataclasses import dataclass
import os
import math
import sys
import json
from pathlib import Path
import time
from uuid import UUID

from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.backend import ContainerPlan
from hub_container_executor.baseline_observer import BaselineObserver
from hub_container_executor.baseline_snapshot import (freeze_baseline_snapshot, _NAME, _RESERVED,
    _directory, _guard, _root_entries, _operator_process_gate, _inventory)
from hub_container_executor.build_record import verify_image_record
from hub_container_executor.daemon_backend import DockerEngineBackend
from hub_container_executor.operation_journal import SQLiteOperationJournal
from hub_container_executor.runtime_profile import derive_runtime_profile

@dataclass(frozen=True)
class BootstrapResult:
    manifest: PilotManifest
    snapshot: object
    draft: object
    container_id: str


def _preflight_snapshot(root, generation, frozen, maximum, deadline):
    root_fd = gen_fd = None
    try:
        root_fd, root_identity = _directory(Path(root), allow_ephemeral_test_storage=True)
        gen_fd, gen_identity = _directory(Path(root) / generation, allow_ephemeral_test_storage=True)
        if root_identity[0] != gen_identity[0]: raise ValueError('Snapshot filesystem mismatch')
        _root_entries(root_fd, generation)
        _operator_process_gate(gen_fd, deadline)
        _inventory(gen_fd, maximum=maximum, deadline=deadline)
        _guard(Path(root), root_fd, root_identity)
        _guard(Path(root) / generation, gen_fd, gen_identity)
    finally:
        failed = sys.exc_info()[0] is not None
        cleanup_error = False
        for fd in (gen_fd, root_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    cleanup_error = True
        if cleanup_error and not failed:
            raise RuntimeError('Snapshot preflight cleanup failed') from None


def _final_manifest(manifest, digest):
    data = json.loads(manifest.canonical_bytes())
    data['state_policy']['baseline_backup_digest'] = digest
    return PilotManifest.from_bytes(json.dumps(data,separators=(',',':')).encode())


def bootstrap_baseline(*, owner_database, backend, manifest, record_store, metadata_store,
                       verifier, secret_resolver, owner_id, generation_root,
                       source_generation_id, frozen_leaf, create_id, start_id, observe_id,
                       maximum_bytes, deadline_monotonic):
    """Run once in the actual child while holding its real PilotOwner.

    The pre-created source SQLite and all inputs are synthetic local fixture
    facts. A returned draft is not an independently accepted baseline receipt.
    """
    if (os.getuid() != 10001 or os.getgid() != 10001 or type(backend) is not DockerEngineBackend
            or type(backend.operation_journal) is not SQLiteOperationJournal
            or type(manifest) is not PilotManifest or type(deadline_monotonic) not in (int,float)
            or not math.isfinite(deadline_monotonic) or time.monotonic() >= deadline_monotonic
            or sys.platform != 'linux' or type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 2 * 1024**3
            or type(source_generation_id) is not str or not _NAME.fullmatch(source_generation_id)
            or type(frozen_leaf) is not str or not _NAME.fullmatch(frozen_leaf)
            or source_generation_id == frozen_leaf or source_generation_id in _RESERVED or frozen_leaf in _RESERVED
            or source_generation_id.startswith('snapshot-') or frozen_leaf.startswith('snapshot-')
            or type(generation_root) is not str or not Path(generation_root).is_absolute()
            or '..' in Path(generation_root).parts
            or backend.binding.generation_root != generation_root):
        raise ValueError('Explicit local baseline child required')
    ids = (owner_id, create_id, start_id, observe_id)
    try:
        if any(type(v) is not str or str(UUID(v)) != v for v in ids) or len(set(ids)) != len(ids):
            raise ValueError
    except (ValueError,TypeError,AttributeError):
        raise ValueError('Distinct canonical identities required') from None
    owner=PilotOwner(owner_database, require_existing=True, allow_ephemeral_test_storage=True)
    try:
        def check():
            owner.assert_active(owner_database)
            if time.monotonic() >= deadline_monotonic:
                raise RuntimeError('Local baseline deadline exceeded')
        def timeout():
            check()
            return max(1, min(manifest.timeouts.inspect_ms, int((deadline_monotonic-time.monotonic())*1000)))
        check()
        _preflight_snapshot(generation_root, source_generation_id, frozen_leaf, maximum_bytes, deadline_monotonic)
        check()
        image=verify_image_record(manifest,'rollback',record_store,metadata_store,verifier)
        defaults=backend.inspect_image(image,timeout())
        if defaults != HostAdapter._defaults_from_record(image):
            raise RuntimeError('Local baseline image mismatch')
        bindings=manifest.runtime.secret_bindings; secrets={}
        for name,slot in (('CODEX_TOKEN',bindings.codex),('HERMES_TOKEN',bindings.hermes),('CLAUDE_TOKEN',bindings.claude)):
            value=secret_resolver.resolve_test_secret(slot.slot_id,slot.version)
            if type(value) is not str or not value or len(value)>4096 or any(ord(c)<32 or ord(c)==127 for c in value):
                raise ValueError('Invalid synthetic fixture credential')
            secrets[name]=value
        profile=derive_runtime_profile(manifest,defaults,source_generation_id,secrets)
        resource='hub-pilot-'+UUID(owner_id).hex[:24]+'-00000000-000-rollback'
        plan=ContainerPlan(resource_name=resource,image=image,immutable_reference=image.runnable_reference,pull=False,profile=profile)
        container_id=backend.create_container(plan,create_id,timeout())
        check(); backend.start_container(container_id,start_id,timeout()); check()
        snapshot=freeze_baseline_snapshot(generation_root=generation_root,source_generation_id=source_generation_id,
            frozen_leaf=frozen_leaf,maximum_bytes=maximum_bytes,deadline_monotonic=deadline_monotonic,
            allow_ephemeral_test_storage=True)
        check()
        final=_final_manifest(manifest, snapshot.sha256)
        observer=BaselineObserver(final,backend=backend,record_store=record_store,metadata_store=metadata_store,
            provenance_verifier=verifier,secret_resolver=secret_resolver,snapshot=snapshot,
            generation_root=generation_root,source_generation_id=source_generation_id,frozen_leaf=frozen_leaf,
            container_id=container_id,owner_guard=check,allow_ephemeral_test_storage=True)
        draft=observer.observe(observe_id); check()
        return BootstrapResult(final,snapshot,draft,container_id)
    finally:
        failed = sys.exc_info()[0] is not None
        try:
            owner.close()
        except Exception:
            if not failed:
                raise RuntimeError('Local baseline owner cleanup failed') from None
