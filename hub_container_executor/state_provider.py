"""Explicit private frozen-backup generation provisioner; no baseline authority.

This source-only provider copies bytes, not health or deployment authorization.
Uncertain interrupted publications and foreign entries require operator review;
they are never automatically adopted, retried, removed or chowned.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
from uuid import UUID, NAMESPACE_URL, uuid5

from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.pilot_ownership import _local_filesystem
from scripts.inspect_hub_image_identity import strict_json
from .backend import BaselineIdentity, StateGeneration
from .state import _check_deadline, _hash_file, _private_dir, _private_file, copy_sqlite_generation


class StateProvisionError(RuntimeError):
    """Safe hold without paths, file contents or arbitrary request data."""


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
_SQLITE_FILES = {"hub.sqlite3", "hub.sqlite3-wal", "hub.sqlite3-shm", "hub.sqlite3-journal"}


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise StateProvisionError("Canonical operation and run identities required")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _identity(info):
    return (info.st_dev, info.st_ino)


def _unchanged(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _entries(path, maximum, deadline=None):
    result = set()
    with os.scandir(path) as entries:
        for entry in entries:
            _check_deadline(deadline)
            result.add(entry.name)
            if len(result) > maximum:
                raise StateProvisionError("Private state entry capacity exceeded")
    return result


class FrozenSQLiteStateProvisioner:
    """One Linux UID/GID-10001 owner; explicit creation or enrolled reopening.

    Existing baseline bytes and an independently supplied BaselineProvider are
    required. The frozen backup is a direct child of generation_root, while
    reservation_root must be separate from all app-mounted generation storage.
    Quotas are checked at admission, not an OS disk quota on a running app.
    Candidate admission reserves one generation and backup-sized byte allowance
    for that run's mandatory rollback. App growth can still consume physical
    capacity afterward; this is not a guarantee of free disk or successful I/O.
    V1 targets ordinary Hub SQLite state, not an OwnedPilotService-enabled
    target: extra pilot-owner lock files intentionally fail the closed inventory.
    Cooperating private-directory ownership is trusted, not hostile same-UID
    administration. Deadlines cannot forcibly cancel an OS disk call.
    """

    def __init__(self, manifest, baseline, *, baseline_provider, frozen_backup,
                 generation_root, reservation_root, baseline_generation_inventory,
                 create: bool, allow_ephemeral_test_storage=False):
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        self._roots = {}
        self._lock_fd = None
        self._closed = True
        try:
            if (sys.platform != "linux" or os.getuid() != 10001 or os.getgid() != 10001
                    or type(create) is not bool or type(allow_ephemeral_test_storage) is not bool
                    or type(manifest) is not PilotManifest or type(baseline) is not BaselineIdentity):
                raise StateProvisionError("Explicit private Linux app-UID state configuration required")
            self.manifest = PilotManifest.from_bytes(manifest.canonical_bytes())
            self._manifest_bytes = self.manifest.canonical_bytes()
            self._manifest_digest = self.manifest.digest()
            self.baseline, self.baseline_provider = baseline, baseline_provider
            self.root, self.reservations = Path(generation_root), Path(reservation_root)
            self.backup = Path(frozen_backup)
            if (self.backup.parent != self.root or not _NAME.fullmatch(self.backup.name)
                    or self.root == self.reservations or self.root in self.reservations.parents
                    or self.reservations in self.root.parents):
                raise StateProvisionError("Separate explicit state and reservation roots required")
            if type(baseline_generation_inventory) is not tuple or not 1 <= len(baseline_generation_inventory) <= 16:
                raise StateProvisionError("Explicit enrolled baseline file inventory required")
            inventory = {}
            for item in baseline_generation_inventory:
                if (type(item) is not tuple or len(item) != 3 or type(item[0]) is not str
                        or not _NAME.fullmatch(item[0]) or type(item[1]) is not int or item[1] < 0
                        or type(item[2]) is not str or not _DIGEST.fullmatch(item[2]) or item[0] in inventory):
                    raise StateProvisionError("Invalid enrolled baseline file inventory")
                inventory[item[0]] = (item[1], item[2])
            if "hub.sqlite3" not in inventory or inventory["hub.sqlite3"][0] <= 0 or len(inventory) > 16:
                raise StateProvisionError("Enrolled flat SQLite baseline required")
            self._baseline_files = inventory
            self._verify_enrollment(self.manifest, baseline)
            for path in (self.root, self.reservations):
                if not path.is_absolute() or ".." in path.parts:
                    raise StateProvisionError("Absolute private state roots required")
                _private_dir(path)
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                self._roots[path] = (fd, _identity(os.fstat(fd)))
                _local_filesystem(fd, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
            self._lock_fd = os.open("owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC |
                                    (os.O_CREAT | os.O_EXCL if create else 0), 0o600,
                                    dir_fd=self._roots[self.reservations][0])
            self._lock_identity = _identity(_private_file(self.reservations / "owner.lock"))
            if self._lock_identity != _identity(os.fstat(self._lock_fd)):
                raise StateProvisionError("State reservation lock changed")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _private_dir(self.root / baseline.state_generation_id)
            self._baseline_inode = _identity((self.root / baseline.state_generation_id).stat(follow_symlinks=False))
            self._backup_inode = _identity(_private_file(self.backup))
            self._header = {"schema": 1, "manifest": self._manifest_digest,
                "baseline": baseline.baseline_id, "backup": baseline.backup_digest,
                "baseline_inode": list(self._baseline_inode), "backup_inode": list(self._backup_inode),
                "root": list(self._roots[self.root][1]), "reservation_root": list(self._roots[self.reservations][1]),
                "lock": list(self._lock_identity),
                "baseline_files": [[name, size, digest] for name, (size, digest) in sorted(inventory.items())]}
            if create:
                if _entries(self.reservations, 2) != {"owner.lock"}:
                    raise StateProvisionError("Fresh private reservation storage required")
                self._write_new("provider.json", self._header)
            elif self._read_json("provider.json") != self._header:
                raise StateProvisionError("State reservation enrollment mismatch")
            self._closed = False
        except BaseException as error:
            self._drop_fds()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise StateProvisionError("Private state provisioner unavailable") from None

    def _verify_enrollment(self, manifest, baseline):
        if (type(manifest) is not PilotManifest or manifest.canonical_bytes() != self._manifest_bytes
                or type(baseline) is not BaselineIdentity or baseline != self.baseline
                or baseline.baseline_id != manifest.state_policy.baseline_id
                or baseline.backup_digest != manifest.state_policy.baseline_backup_digest
                or baseline.image_config_id != manifest.rollback.image_config_id
                or baseline.healthy_at_enrollment is not True or not _CONTAINER.fullmatch(baseline.container_id)
                or not _NAME.fullmatch(baseline.state_generation_id) or not _NAME.fullmatch(baseline.proof_id)
                or self.baseline_provider.enrolled_baseline(manifest) != baseline
                or self.baseline_provider.verify_baseline(baseline, manifest) is not True):
            raise StateProvisionError("Independent enrolled baseline verification required")

    def _check(self, deadline):
        _check_deadline(deadline)
        if self._closed or self._pid != os.getpid():
            raise StateProvisionError("State provisioner is not active")
        for path, (fd, expected) in self._roots.items():
            _private_dir(path)
            if _identity(path.stat(follow_symlinks=False)) != expected or _identity(os.fstat(fd)) != expected:
                raise StateProvisionError("Private state root changed")
        if (_identity(_private_file(self.reservations / "owner.lock")) != self._lock_identity
                or _identity(os.fstat(self._lock_fd)) != self._lock_identity):
            raise StateProvisionError("Private state reservation lock changed")
        if self._read_json("provider.json") != self._header:
            raise StateProvisionError("Private state enrollment changed")
        _check_deadline(deadline)

    def _read_json(self, name):
        path = self.reservations / name
        before = _private_file(path)
        if before.st_size > 65536:
            raise StateProvisionError("State reservation metadata limit exceeded")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=self._roots[self.reservations][0])
        try:
            if _identity(os.fstat(fd)) != _identity(before):
                raise StateProvisionError("State reservation metadata changed")
            raw = os.read(fd, 65537)
            if len(raw) != before.st_size or _identity(_private_file(path)) != _identity(before):
                raise StateProvisionError("State reservation metadata changed")
            return strict_json(raw)
        finally:
            os.close(fd)

    def _write_new(self, name, value, deadline=None):
        _check_deadline(deadline)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self._roots[self.reservations][0])
        try:
            raw = _canonical(value)
            offset = 0
            while offset < len(raw):
                _check_deadline(deadline)
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    raise StateProvisionError("Private state metadata write unavailable")
                offset += written
            os.fsync(fd)
            _check_deadline(deadline)
        finally:
            os.close(fd)
        os.fsync(self._roots[self.reservations][0])
        _check_deadline(deadline)

    def _records(self, deadline):
        values = []
        names = _entries(self.reservations, 4098, deadline)
        for name in names:
            _check_deadline(deadline)
            if name in {"owner.lock", "provider.json"}:
                continue
            if name.endswith(".complete"):
                operation = name.removesuffix(".complete")
                _uuid(operation)
                if operation + ".json" not in names:
                    raise StateProvisionError("Completion lacks state reservation")
                continue
            operation = name.removesuffix(".json")
            if not name.endswith(".json"):
                raise StateProvisionError("Foreign state reservation entry")
            _uuid(operation)
            intent = self._read_json(name)
            if (type(intent) is not dict or set(intent) != {"schema", "manifest", "run", "operation", "role", "generation"}
                    or type(intent["schema"]) is not int or intent["schema"] != 1 or intent["manifest"] != self._manifest_digest
                    or intent["operation"] != operation or intent["role"] not in {"candidate", "rollback"}):
                raise StateProvisionError("Invalid private state reservation")
            _uuid(intent["run"])
            if intent["generation"] != self._generation(intent["run"], operation, intent["role"]):
                raise StateProvisionError("State generation reservation mismatch")
            completion_name = operation + ".complete"
            # Completion metadata has a separate immutable name so a crash
            # cannot replace a durable intent with invented success.
            values.append((intent, completion_name))
        return values

    def _generation(self, run, operation, role):
        return role + "-" + uuid5(NAMESPACE_URL, self._manifest_digest + "/" + run + "/" + operation + "/" + role).hex

    def _baseline_bytes(self, deadline):
        self._check(deadline)
        for suffix in ("-wal", "-shm", "-journal"):
            companion = self.backup.with_name(self.backup.name + suffix)
            if companion.exists() or companion.is_symlink():
                raise StateProvisionError("Frozen baseline backup must have no SQLite companions")
        before = _private_file(self.backup)
        with self.backup.open("rb") as frozen:
            header = frozen.read(20)
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            # Opening a WAL-mode SQLite DB can create shared-memory sidecars
            # even for read-only connections. Admit only quiescent rollback-
            # journal format, without modifying its journal mode ourselves.
            raise StateProvisionError("Frozen backup must be quiescent rollback-journal SQLite")
        digest, size = _hash_file(self.backup, deadline_monotonic=deadline,
                                  maximum_bytes=self.manifest.retention.max_storage_bytes)
        after = _private_file(self.backup)
        if (digest != self.baseline.backup_digest or _unchanged(before) != _unchanged(after)
                or _identity(before) != self._backup_inode or size != before.st_size):
            raise StateProvisionError("Frozen baseline backup changed")
        baseline_dir = self.root / self.baseline.state_generation_id
        _private_dir(baseline_dir)
        if _identity(baseline_dir.stat(follow_symlinks=False)) != self._baseline_inode:
            raise StateProvisionError("Enrolled baseline directory changed")
        if _entries(baseline_dir, 16, deadline) != set(self._baseline_files):
            raise StateProvisionError("Enrolled baseline inventory changed")
        total = size
        for name, (expected_size, expected_digest) in self._baseline_files.items():
            path = baseline_dir / name
            info = _private_file(path)
            if info.st_size != expected_size:
                raise StateProvisionError("Enrolled baseline file size changed")
            actual_digest, actual_size = _hash_file(path, deadline_monotonic=deadline,
                                                   maximum_bytes=min(expected_size, self.manifest.retention.max_storage_bytes))
            if (actual_size != expected_size or actual_digest != expected_digest
                    or _unchanged(_private_file(path)) != _unchanged(info)):
                raise StateProvisionError("Enrolled baseline bytes changed")
            total += actual_size
        return size, total

    def prepare_candidate(self, manifest, run_id, baseline, operation_id, *, deadline_monotonic):
        return self._prepare(manifest, run_id, baseline, operation_id, "candidate", deadline_monotonic)

    def prepare_rollback(self, manifest, run_id, baseline, operation_id, *, deadline_monotonic):
        return self._prepare(manifest, run_id, baseline, operation_id, "rollback", deadline_monotonic)

    def _prepare(self, manifest, run, baseline, operation, role, deadline):
        if self._pid != os.getpid():
            raise StateProvisionError("State provisioner is process bound")
        try:
            valid_deadline = type(deadline) in {int, float} and math.isfinite(deadline)
        except OverflowError:
            valid_deadline = False
        if not valid_deadline:
            raise StateProvisionError("Finite state-copy deadline required")
        with self._mutex:
            try:
                return self._prepare_active(manifest, run, baseline, operation, role, deadline)
            except Exception:
                raise StateProvisionError("State generation held; inspect private reservation storage") from None

    def _inventory_usage(self, deadline):
        backup_size, total = self._baseline_bytes(deadline)
        baseline_total = total
        known = {self.backup.name, self.baseline.state_generation_id}
        count = 1
        entries = self._records(deadline)
        for intent, completion_name in entries:
            # Missing completion is uncertainty, not an opportunity to replay.
            completion = self._read_json(completion_name)
            directory = self.root / intent["generation"]
            _private_dir(directory)
            if (type(completion) is not dict or set(completion) != {"generation", "inode"}
                    or completion["generation"] != intent["generation"]
                    or completion["inode"] != list(_identity(directory.stat(follow_symlinks=False)))):
                raise StateProvisionError("Ambiguous existing state generation")
            names = _entries(directory, 4, deadline)
            if "hub.sqlite3" not in names or not names <= _SQLITE_FILES:
                raise StateProvisionError("Foreign state generation contents")
            for name in names:
                _check_deadline(deadline)
                total += _private_file(directory / name).st_size
            known.add(intent["generation"])
            count += 1
        if _entries(self.root, self.manifest.retention.max_generations + 1, deadline) != known:
            raise StateProvisionError("Foreign generation root entry")
        return backup_size, baseline_total, total, count, entries

    @staticmethod
    def _rollback_reservations(entries):
        candidates, rollbacks = set(), set()
        for intent, _ in entries:
            seen = candidates if intent["role"] == "candidate" else rollbacks
            if intent["run"] in seen:
                raise StateProvisionError("Duplicate per-run state generation reservation")
            seen.add(intent["run"])
        if not rollbacks <= candidates:
            raise StateProvisionError("Rollback lacks its candidate reservation")
        outstanding = candidates - rollbacks
        if len(outstanding) > 1:
            raise StateProvisionError("Multiple outstanding rollback reservations unsupported")
        return candidates, outstanding

    def _prepare_active(self, manifest, run, baseline, operation, role, deadline):
        self._check(deadline)
        _uuid(run)
        _uuid(operation)
        self._verify_enrollment(manifest, baseline)
        backup_size, baseline_total, total, count, entries = self._inventory_usage(deadline)
        candidates, outstanding = self._rollback_reservations(entries)
        if role == "candidate":
            if run in candidates or outstanding:
                raise StateProvisionError("Candidate would consume an outstanding rollback reservation")
            required_generations, required_bytes = 2, 2 * backup_size
        else:
            if run not in outstanding:
                raise StateProvisionError("Rollback must consume the same run's reserved generation")
            required_generations, required_bytes = 1, backup_size
        if (count + required_generations > manifest.retention.max_generations
                or total + required_bytes > manifest.retention.max_storage_bytes):
            raise StateProvisionError("State generation retention quota exhausted")
        generation = self._generation(run, operation, role)
        intent = {"schema": 1, "manifest": self._manifest_digest, "run": run,
                  "operation": operation, "role": role, "generation": generation}
        self._check(deadline)
        self._write_new(operation + ".json", intent, deadline)  # Durable no-replay reservation.
        genpath, temppath = self.root / generation, self.root / ("copy-" + operation + ".sqlite3")
        genfd = None
        genidentity = tempidentity = None
        linked = False
        try:
            os.mkdir(generation, mode=0o700, dir_fd=self._roots[self.root][0])
            genfd = os.open(generation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=self._roots[self.root][0])
            genidentity = _identity(os.fstat(genfd))
            copied = copy_sqlite_generation(self.backup, temppath,
                maximum_bytes=backup_size, deadline_monotonic=deadline)
            tempidentity = _identity(_private_file(temppath))
            if self._baseline_bytes(deadline) != (backup_size, baseline_total):
                raise StateProvisionError("Enrolled baseline changed during state copy")
            self._check(deadline)
            if _identity(genpath.stat(follow_symlinks=False)) != genidentity or _entries(genpath, 1, deadline):
                raise StateProvisionError("New generation directory changed")
            os.link(temppath.name, "hub.sqlite3", src_dir_fd=self._roots[self.root][0],
                    dst_dir_fd=genfd, follow_symlinks=False)
            linked = True
            os.unlink(temppath.name, dir_fd=self._roots[self.root][0])
            os.fsync(genfd)
            os.fsync(self._roots[self.root][0])
            self._check(deadline)
            if _identity(_private_file(genpath / "hub.sqlite3")) != tempidentity:
                raise StateProvisionError("Published SQLite identity changed")
            self._write_new(operation + ".complete", {"generation": generation, "inode": list(genidentity)}, deadline)
            self._check(deadline)
            final_backup, final_baseline, final_total, final_count, final_entries = self._inventory_usage(deadline)
            _, final_reserves = self._rollback_reservations(final_entries)
            if (final_backup != backup_size or final_baseline != baseline_total
                    or final_total + len(final_reserves) * backup_size > manifest.retention.max_storage_bytes
                    or final_count + len(final_reserves) > manifest.retention.max_generations):
                raise StateProvisionError("State bytes or quota changed during publication")
            return StateGeneration(generation, baseline.state_generation_id, baseline.backup_digest,
                                   copied.sha256, copied.size_bytes, True)
        except BaseException:
            # Only current-call inodes are eligible. Intent metadata remains:
            # failures and crashes are never silently replayed or adopted.
            try:
                if genidentity is not None and _identity(genpath.stat(follow_symlinks=False)) == genidentity:
                    if linked and tempidentity is not None:
                        named = genpath / "hub.sqlite3"
                        if _identity(named.stat(follow_symlinks=False)) == tempidentity:
                            os.unlink("hub.sqlite3", dir_fd=genfd)
                    if not _entries(genpath, 4):
                        os.rmdir(generation, dir_fd=self._roots[self.root][0])
                if tempidentity is not None and _identity(temppath.stat(follow_symlinks=False)) == tempidentity:
                    os.unlink(temppath.name, dir_fd=self._roots[self.root][0])
            except OSError:
                pass
            raise
        finally:
            if genfd is not None:
                os.close(genfd)

    def _drop_fds(self):
        self._closed = True
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        for fd, _ in self._roots.values():
            os.close(fd)
        self._roots.clear()

    def close(self):
        if self._pid != os.getpid():
            self._drop_fds()  # Never LOCK_UN or acquire an inherited mutex.
            return
        with self._mutex:
            self._drop_fds()

    def __enter__(self):
        self._check(None)
        return self

    def __exit__(self, *args):
        self.close()
