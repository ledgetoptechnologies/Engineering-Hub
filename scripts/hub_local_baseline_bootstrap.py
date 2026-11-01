"""Local-test-only child baseline preparation using the actual owner and observer.

Synthetic fixture input and trusted local operator only. The child must run as
UID/GID 10001 with exactly one task; it owns a separately enrolled Hub DB. No
retry, cleanup or publication occurs after any uncertain Docker effect.
"""
from dataclasses import dataclass
import os
import stat
import math
import sys
import json
from pathlib import Path
import time
from uuid import UUID

from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.backend import ContainerPlan, ContainerObservation, ProbeObservation
from hub_container_executor.baseline_observer import BaselineObserver
from hub_container_executor.baseline_snapshot import (freeze_baseline_snapshot, _NAME, _RESERVED,
    _directory, _guard, _root_entries, _operator_process_gate, _inventory)
from hub_container_executor.build_record import verify_image_record
from hub_container_executor.daemon_backend import DockerEngineBackend
from hub_container_executor.operation_journal import SQLiteOperationJournal
from hub_container_executor.runtime_profile import derive_runtime_profile, probe_set_digest

@dataclass(frozen=True)
class BootstrapResult:
    manifest: PilotManifest
    snapshot: object
    draft: object
    container_id: str


def _preflight_snapshot(root, generation, frozen, maximum, deadline, *, initialize_empty=False):
    root_fd = gen_fd = None
    try:
        root_fd, root_identity = _directory(Path(root), allow_ephemeral_test_storage=True)
        gen_fd, gen_identity = _directory(Path(root) / generation, allow_ephemeral_test_storage=True)
        if root_identity[0] != gen_identity[0]: raise ValueError('Snapshot filesystem mismatch')
        _root_entries(root_fd, generation)
        if initialize_empty:
            with os.scandir('/proc/self/task') as tasks:
                if sum(1 for _ in tasks) != 1 or time.monotonic() >= deadline:
                    raise ValueError('Dedicated baseline process required')
            with os.scandir(gen_fd) as entries:
                if next(entries, None) is not None:
                    raise ValueError('Initial generation must be empty')
            # The synthetic fixture starts with a private empty database;
            # SQLite otherwise creates it under the application's umask.
            _guard(Path(root), root_fd, root_identity)
            _guard(Path(root) / generation, gen_fd, gen_identity)
            if time.monotonic() >= deadline:
                raise ValueError('Initial generation deadline exceeded')
            db_fd = os.open('hub.sqlite3', os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                            os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=gen_fd)
            try:
                opened = os.fstat(db_fd)
                named = os.stat('hub.sqlite3', dir_fd=gen_fd, follow_symlinks=False)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != 10001 or
                        opened.st_gid != 10001 or stat.S_IMODE(opened.st_mode) != 0o600 or
                        opened.st_nlink != 1 or opened.st_size != 0 or
                        (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
                    raise ValueError('Initial database identity unavailable')
                os.fsync(db_fd)
                os.fsync(gen_fd)
            finally:
                os.close(db_fd)
        else:
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


def _exited_baseline(backend, container_id, deadline):
    """Verify the enrolled container and its daemon-reported stopped process."""
    observation = backend._known_container(container_id, 'exited', deadline)
    backend._assert_runner(deadline)
    raw = backend._json('GET', backend._route('/containers/' + container_id + '/json'),
                        deadline, expected_status=200)
    state = raw.get('State') if type(raw) is dict else None
    if (type(state) is not dict or raw.get('Id') != container_id or state.get('Status') != 'exited'
            or state.get('Running') is not False or type(state.get('Pid')) is not int or state['Pid'] != 0
            or state.get('Paused') is not False or state.get('Restarting') is not False or state.get('Dead') is not False):
        raise RuntimeError('Baseline stopped process is unverified')
    backend._assert_runner(deadline)
    if backend._known_container(container_id, 'exited', deadline) != observation:
        raise RuntimeError('Baseline stopped container changed')
    return observation


def _restart_baseline(backend, container_id, operation_id, timeout_ms):
    """One journaled local-test restart; normal backend start stays unchanged."""
    deadline = backend._deadline(timeout_ms)
    backend._operation_id(operation_id)
    before = _exited_baseline(backend, container_id, deadline)
    backend._journal_begin(operation_id, 'start')
    backend._assert_runner(deadline)
    backend._read_response('POST', backend._route('/containers/' + container_id + '/start'),
                           deadline, expected_status=204)
    backend._journal_complete(operation_id, 'start', container_id)
    backend._assert_runner(deadline)
    after = backend.inspect_container(container_id, backend._remaining_ms(deadline))
    if (type(after) is not ContainerObservation or after.status != 'running'
            or after.selected_image != before.selected_image or after.profile != before.profile):
        raise RuntimeError('Baseline restart is unverified')


def bootstrap_baseline(*, owner_database, backend, manifest, record_store, metadata_store,
                       verifier, secret_resolver, owner_id, generation_root,
                       source_generation_id, frozen_leaf, create_id, start_id, observe_id,
                       maximum_bytes, deadline_monotonic, quiescent=False,
                       preflight_probe_id=None, stop_id=None, restart_id=None, startup_wait_ms=0,
                       initialize_empty=False):
    """Run once in the actual child while holding its real PilotOwner.

    Inputs are synthetic local fixture facts. Explicit empty initialization
    requires the quiescent path and still fails unless the application creates
    a valid database and passes the later checks. A returned draft is not an
    independently accepted baseline receipt.
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
            or backend.binding.generation_root != generation_root or type(quiescent) is not bool
            or type(startup_wait_ms) is not int or not 0 <= startup_wait_ms <= 30000
            or type(initialize_empty) is not bool or (initialize_empty and not quiescent)
            or (not quiescent and (preflight_probe_id is not None or stop_id is not None or restart_id is not None
                                   or startup_wait_ms != 0))
            or (quiescent and any(probe.startup_grace_ms for probe in manifest.probes.rollback))):
        raise ValueError('Explicit local baseline child required')
    ids = (owner_id, create_id, start_id, observe_id) + ((preflight_probe_id, stop_id, restart_id) if quiescent else ())
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
        # Refuse a reused later-stage ID before creating any container. This
        # is a preflight under the fixture's exclusive-operator assumption;
        # each journal begin remains the authoritative one-shot reservation.
        operation_ids = (create_id, start_id, observe_id) + (
            (preflight_probe_id, stop_id, restart_id) if quiescent else ())
        for operation_id in operation_ids:
            check()
            if backend.operation_journal.inspect(operation_id) is not None:
                raise RuntimeError('Baseline operation identity already used')
            check()
        _preflight_snapshot(generation_root, source_generation_id, frozen_leaf, maximum_bytes, deadline_monotonic,
                            initialize_empty=initialize_empty)
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
        if quiescent:
            if time.monotonic() + startup_wait_ms / 1000 >= deadline_monotonic:
                raise RuntimeError('Baseline startup budget unavailable')
            if startup_wait_ms:
                time.sleep(startup_wait_ms / 1000)
            check()
            probes = manifest.probes.rollback
            budgets = tuple(probe.timeout_ms for probe in probes)
            if time.monotonic() + sum(budgets) / 1000 >= deadline_monotonic:
                raise RuntimeError('Baseline probe budget unavailable')
            health = backend.run_compiled_probes(container_id, probes, preflight_probe_id, budgets)
            if (type(health) is not ProbeObservation or health.container_id != container_id
                    or health.image_config_id != image.image_config_id or health.profile_digest != probe_set_digest(probes)
                    or health.health != 'healthy' or health.evidence_verified is not True):
                raise RuntimeError('Baseline health is unverified')
            check(); backend.stop_container(container_id,stop_id,timeout()); check()
            _exited_baseline(backend, container_id, deadline_monotonic); check()
        if initialize_empty:
            # Only this explicitly disposable fixture has an independently
            # bounded tmpfs that can hold the private sidecar copy.
            from scripts.hub_local_scratch_snapshot import freeze_copy
            snapshot=freeze_copy(generation_root,source_generation_id,frozen_leaf,
                                 maximum=maximum_bytes,deadline=deadline_monotonic)
        else:
            snapshot=freeze_baseline_snapshot(generation_root=generation_root,source_generation_id=source_generation_id,
                frozen_leaf=frozen_leaf,maximum_bytes=maximum_bytes,deadline_monotonic=deadline_monotonic,
                allow_ephemeral_test_storage=True)
        check()
        if quiescent:
            _exited_baseline(backend, container_id, deadline_monotonic); check()
            _restart_baseline(backend, container_id, restart_id, timeout()); check()
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
