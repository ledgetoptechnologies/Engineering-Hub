"""Explicit one-attempt baseline observation, not receipt publication/activation.

Only trusted local operator composition supplies the real backend, protected
build stores, test-secret resolver and live owner guard. The baseline and frozen
copy must already exist in an isolated campaign. No API, CLI, listener, discovery,
container creation, retry or automatic enrollment is provided here.
"""
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
import hashlib
import inspect
import json
import math
import os
import re
import sys
import sqlite3
import time
from uuid import UUID

from engineering_hub.container_pilot_manifest import PilotManifest
from .adapter import HostAdapter
from .backend import ActualImageSelection, ContainerObservation, ImageDefaults, OperationObservation, ProbeObservation, RunnerObservation
from .baseline_snapshot import FrozenBaselineSnapshot, _directory, _guard, _inventory, _file, _hash, _operator_process_gate
from .build_record import verify_image_record
from .daemon_backend import DockerEngineBackend
from .operation_journal import SQLiteOperationJournal
from .runtime_profile import derive_runtime_profile, probe_set_digest, runtime_profile_digest


class BaselineObservationError(RuntimeError):
    """Sanitized failure; uncertain probe intents remain in the private journal."""


@dataclass(frozen=True)
class ObservedBaselineDraft:
    receipt_bytes: bytes
    receipt_digest: str
    runtime_profile_digest: str
    proof_id: str


_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


def _require(value):
    if not value:
        raise BaselineObservationError('baseline_observation_unverified')


class BaselineObserver:
    """Dedicated operator process, separate from the baseline SQLite writer.

    Construction starts nothing. observe() may execute ONLY the fixed compiled
    probes, with their durable operation journal. Returned private draft bytes
    must still be protected and independently pinned; their hash is not approval.
    A historical observation does not prove current health or driver identity.
    """
    def __init__(self, manifest, *, backend, record_store, metadata_store,
                 provenance_verifier, secret_resolver, snapshot, generation_root,
                 source_generation_id, frozen_leaf, container_id, owner_guard,
                 allow_ephemeral_test_storage=False):
        try:
            _require(type(manifest) is PilotManifest and type(backend) is DockerEngineBackend
                     and backend._using_test_transport is False
                     and type(backend.operation_journal) is SQLiteOperationJournal
                     and type(snapshot) is FrozenBaselineSnapshot and callable(owner_guard)
                     and type(snapshot.sha256) is str and re.fullmatch('sha256:[a-f0-9]{64}', snapshot.sha256)
                     and type(snapshot.size_bytes) is int and snapshot.size_bytes > 0
                     and type(allow_ephemeral_test_storage) is bool
                     and type(container_id) is str and re.fullmatch('[a-f0-9]{64}', container_id)
                     and type(source_generation_id) is str and _NAME.fullmatch(source_generation_id)
                     and type(frozen_leaf) is str and _NAME.fullmatch(frozen_leaf)
                     and source_generation_id != frozen_leaf)
            _require(type(snapshot.source_inventory) is tuple and 1 <= len(snapshot.source_inventory) <= 4
                     and all(type(item) is tuple and len(item) == 3 and type(item[0]) is str
                             and type(item[1]) is int and item[1] >= 0 and type(item[2]) is str
                             and re.fullmatch('sha256:[a-f0-9]{64}', item[2])
                             for item in snapshot.source_inventory)
                     and type(snapshot.source_identities) is tuple and len(snapshot.source_identities) == len(snapshot.source_inventory)
                     and all(type(item) is tuple and len(item) == 5 and type(item[0]) is str
                             and all(type(value) is int and value >= 0 for value in item[1:])
                             for item in snapshot.source_identities))
            self.manifest = PilotManifest.from_bytes(manifest.canonical_bytes())
            self.root = Path(generation_root)
            _require(self.root.is_absolute() and '..' not in self.root.parts
                     and backend.binding.generation_root == str(self.root)
                     and not Path(backend.operation_journal.path).is_relative_to(self.root))
            self.backend, self.snapshot = backend, snapshot
            self.record_store, self.metadata_store = record_store, metadata_store
            self.provenance_verifier, self.secret_resolver = provenance_verifier, secret_resolver
            self.generation_id, self.frozen_leaf, self.container_id = source_generation_id, frozen_leaf, container_id
            self.owner_guard, self._test_storage = owner_guard, allow_ephemeral_test_storage
            self._pid, self._attempted = os.getpid(), False
        except Exception:
            raise BaselineObservationError('explicit_baseline_observer_configuration_required') from None

    def _scope(self, deadline):
        _require(self._pid == os.getpid() and time.monotonic() < deadline)
        checked = self.owner_guard()
        if inspect.iscoroutine(checked):
            checked.close()
        _require(checked is None and time.monotonic() < deadline)

    def _budget(self, deadline, maximum):
        self._scope(deadline)
        remaining = int((deadline - time.monotonic()) * 1000)
        _require(remaining >= 1)
        return min(maximum, remaining)

    def _call(self, deadline, callback, *args):
        timeout = self._budget(deadline, self.manifest.timeouts.inspect_ms)
        result = callback(*args, timeout)
        self._scope(deadline)
        return result

    def _storage(self, root_fd, root_identity, generation_fd, generation_identity, deadline):
        self._scope(deadline)
        generation = self.root / self.generation_id
        _guard(self.root, root_fd, root_identity)
        _guard(generation, generation_fd, generation_identity)
        _operator_process_gate(generation_fd, deadline)
        # The dedicated-process rule covers the frozen SQLite inode too. Never
        # close a raw FD while this process already owns a SQLite connection to
        # that inode: POSIX SQLite locks are process-scoped.
        named = _file(os.stat(self.frozen_leaf, dir_fd=root_fd, follow_symlinks=False))
        _require(named[:2] not in {(item[1], item[2]) for item in self.snapshot.source_identities})
        with os.scandir('/proc/self/fd') as descriptors:
            for entry in descriptors:
                self._scope(deadline)
                info = os.fstat(int(entry.name))
                _require((info.st_dev, info.st_ino) != named[:2])
        # Only a prepared first-baseline campaign, never arbitrary app storage.
        with os.scandir(root_fd) as entries:
            _require({entry.name for entry in entries} == {self.generation_id, self.frozen_leaf})
        observed = _inventory(generation_fd, maximum=self.manifest.retention.max_storage_bytes, deadline=deadline)
        _require(observed == (self.snapshot.source_inventory, self.snapshot.source_identities))
        fd = os.open(self.frozen_leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root_fd)
        try:
            before = _file(os.fstat(fd))
            header = os.read(fd, 20)
            _require(header[:16] == b'SQLite format 3\x00' and header[18:20] == b'\x01\x01')
            digest, size = _hash(fd, self.manifest.retention.max_storage_bytes, deadline)
            _require(_file(os.fstat(fd)) == before
                     and _file(os.stat(self.frozen_leaf, dir_fd=root_fd, follow_symlinks=False)) == before
                     and digest == self.snapshot.sha256 == self.manifest.state_policy.baseline_backup_digest
                     and type(self.snapshot.size_bytes) is int and 0 < size == self.snapshot.size_bytes
                     and size + sum(item[1] for item in observed[0]) <= self.manifest.retention.max_storage_bytes)
        finally:
            os.close(fd)
        # Only the already frozen, rollback-journal copy is queried. Immutable
        # read-only mode creates no companions and never changes journal mode.
        with closing(sqlite3.connect((self.root / self.frozen_leaf).as_uri()
                + '?mode=ro&immutable=1', uri=True, timeout=0)) as db:
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute('PRAGMA trusted_schema=OFF')
            _require(db.execute('PRAGMA integrity_check').fetchone() == ('ok',))
            _require(db.execute('PRAGMA page_count').fetchone()[0]
                     * db.execute('PRAGMA page_size').fetchone()[0] <= self.manifest.retention.max_storage_bytes)
        _require(_file(os.stat(self.frozen_leaf, dir_fd=root_fd, follow_symlinks=False)) == before)
        self._scope(deadline)
        return before

    def observe(self, operation_id):
        root_fd = generation_fd = None
        try:
            _require(not self._attempted and self._pid == os.getpid())
            self._attempted = True
            _require(sys.platform == 'linux' and os.getuid() == os.getgid() == 10001
                     and type(operation_id) is str and str(UUID(operation_id)) == operation_id)
            deadline = time.monotonic() + self.manifest.timeouts.total_command_ms / 1000
            self._scope(deadline)
            root_fd, root_identity = _directory(self.root, allow_ephemeral_test_storage=self._test_storage)
            generation_fd, generation_identity = _directory(self.root / self.generation_id,
                allow_ephemeral_test_storage=self._test_storage)
            frozen_identity = self._storage(root_fd, root_identity, generation_fd, generation_identity, deadline)
            self._scope(deadline)
            _require(self.backend.operation_journal.inspect(operation_id) is None)
            self._scope(deadline)
            image = verify_image_record(self.manifest, 'rollback', self.record_store,
                                        self.metadata_store, self.provenance_verifier)
            self._scope(deadline)
            runner = self._call(deadline, self.backend.inspect_runner)
            binding = self.manifest.runner_binding
            _require(type(runner) is RunnerObservation and runner == RunnerObservation(
                binding.runner_id, binding.daemon_identity, binding.platform))
            defaults = self._call(deadline, self.backend.inspect_image, image)
            _require(type(defaults) is ImageDefaults and defaults == HostAdapter._defaults_from_record(image))
            secrets = {}
            slots = self.manifest.runtime.secret_bindings
            for name, slot in (('CODEX_TOKEN', slots.codex), ('HERMES_TOKEN', slots.hermes), ('CLAUDE_TOKEN', slots.claude)):
                self._scope(deadline)
                value = self.secret_resolver.resolve_test_secret(slot.slot_id, slot.version)
                _require(type(value) is str and 0 < len(value) <= 4096
                         and not any(ord(char) < 32 or ord(char) == 127 for char in value))
                secrets[name] = value
                self._scope(deadline)
            profile = derive_runtime_profile(self.manifest, defaults, self.generation_id, secrets)
            expected_image = ActualImageSelection(image.runnable_reference, image.platform_manifest_digest,
                                                  image.image_config_id, image.platform)
            def container():
                actual = self._call(deadline, self.backend.inspect_container, self.container_id)
                _require(type(actual) is ContainerObservation and actual.container_id == self.container_id
                         and actual.selected_image == expected_image and actual.profile == profile
                         and actual.state_generation_id == self.generation_id and actual.status == 'running')
            container()
            _require(self._storage(root_fd, root_identity, generation_fd, generation_identity, deadline) == frozen_identity)
            probes = self.manifest.probes.rollback
            budgets = tuple(probe.timeout_ms for probe in probes)
            _require(sum(budgets) <= self._budget(deadline, 120000))
            outcome = self.backend.run_compiled_probes(self.container_id, probes, operation_id, budgets)
            self._scope(deadline)
            _require(type(outcome) is ProbeObservation and outcome.container_id == self.container_id
                     and outcome.image_config_id == image.image_config_id
                     and outcome.profile_digest == probe_set_digest(probes)
                     and outcome.health == 'healthy' and outcome.evidence_verified is True)
            receipt = self._call(deadline, self.backend.inspect_operation, operation_id)
            _require(type(receipt) is OperationObservation and receipt.operation_id == operation_id
                     and receipt.state == 'completed' and receipt.terminal is True
                     and receipt.container_id == self.container_id)
            container()
            _require(self._storage(root_fd, root_identity, generation_fd, generation_identity, deadline) == frozen_identity)
            now = time.time()
            _require(type(now) in {int, float} and math.isfinite(now) and 0 <= now < 2**53)
            profile_hash = runtime_profile_digest(profile)
            row = dict(schema='hub_baseline_receipt_v1', campaign_id=self.manifest.campaign_id,
                proof_id=operation_id, manifest_digest=self.manifest.digest(),
                baseline_id=self.manifest.state_policy.baseline_id,
                runner=binding.model_dump(mode='json'),
                rollback=dict(runnable_reference=image.runnable_reference, runnable_media_type=image.runnable_media_type,
                              platform_manifest_digest=image.platform_manifest_digest, image_config_id=image.image_config_id,
                              source_commit=image.source_commit, record_digest=image.record_digest),
                container_id=self.container_id, state_generation_id=self.generation_id,
                backup=dict(digest=self.snapshot.sha256, size_bytes=self.snapshot.size_bytes),
                runtime_profile_digest=profile_hash, probe_set_digest=probe_set_digest(probes),
                observation=dict(health='healthy', evidence_verified=True, observed_at=int(now)),
                baseline_inventory=[dict(name=name, size_bytes=size, digest=digest)
                                    for name, size, digest in self.snapshot.source_inventory])
            raw = json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
            _require(len(raw) <= 65536)
            self._scope(deadline)
            return ObservedBaselineDraft(raw, 'sha256:' + hashlib.sha256(raw).hexdigest(), profile_hash, operation_id)
        except Exception:
            raise BaselineObservationError('baseline_observation_unverified') from None
        finally:
            for fd in (generation_fd, root_fd):
                if fd is not None:
                    os.close(fd)
