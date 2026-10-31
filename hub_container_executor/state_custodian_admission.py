"""Provider-side signed one-shot reservation and private quarantine copy.

Root-owned enrollment and journal are independent of the executor. A successful
reservation is only a point-in-time admission record, not continuing custody or
permission to publish a generation. Restart never turns an old reservation into
permission to retry. Host isolation and effect integration remain disabled.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import select
import struct
import subprocess
import sys
import sqlite3
import stat
import time
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .launch_supervisor import _canonical, _root_file, _root_parent, _path, _Process, _boot_id, _mapped_id

PROTOCOL = 'engineering-hub.state-custodian-admission.v1'
DOMAIN = b'engineering-hub.state-custodian-admission.v1\x00'
FIELDS = frozenset({'protocol', 'operation_id', 'command_id', 'run_id', 'target_id',
                    'fencing_generation', 'manifest_digest', 'role', 'source_backup_digest',
                    'provider_key_digest', 'nonce', 'issued_at_ms', 'expires_at_ms'})
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_SHA = re.compile(r'sha256:[0-9a-f]{64}\Z')
_SCHEMA = 'CREATE TABLE admissions (operation_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, payload BLOB NOT NULL, signature BLOB NOT NULL)'
_HOLD_SCHEMA = 'CREATE TABLE custody_holds (target_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, admission_digest TEXT NOT NULL, fence INTEGER NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL, host_uid INTEGER NOT NULL, host_gid INTEGER NOT NULL, owner_policy_digest TEXT NOT NULL)'
_TRANSPORT = r'''
import array, os, socket, struct, sys
s = socket.socket(fileno=int(sys.argv[1]))
request = sys.stdin.buffer.readline(4097)
if not request.endswith(b'\n') or len(request) > 4096: sys.exit(2)
if s.send(request[:-1]) != len(request)-1: sys.exit(2)
raw, controls, flags, _ = s.recvmsg(16385, socket.CMSG_SPACE(256), socket.MSG_CMSG_CLOEXEC)
if controls:
    for level, kind, data in controls:
        if level == socket.SOL_SOCKET and kind in (socket.SCM_RIGHTS, 4):
            fds = array.array('i'); fds.frombytes(data[:len(data)-len(data)%fds.itemsize])
            for fd in fds:
                try: os.close(fd)
                except OSError: pass
    sys.exit(2)
if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(raw) > 16384: sys.exit(2)
sys.stdout.buffer.write(struct.pack('!I', len(raw)) + raw); sys.stdout.buffer.flush()
sys.stdin.buffer.read(1)
'''


class CustodianAdmissionError(RuntimeError):
    def __init__(self):
        super().__init__('custodian admission unavailable; preserve original evidence')


class CustodianAdmissionJournal:
    """Consume signed observations once in a root-owned, single-owner journal.

    Enrollment is canonical root-protected JSON pinning supervisor public key,
    manifest and backup digests, and provider identity. Schema 2 also pins the
    exact journal path and is required for private ownership preparation; schema
    1 never upgrades itself to ownership authority.
    Root and its administrators remain trusted; no anti-rollback storage is
    claimed. This module can copy a backup into root-private quarantine; it
    provides no network listener, ownership handoff or application publication.
    """
    def __init__(self, *, enrollment_path, journal_path, create):
        self._fd = None
        self._db = None
        self._pid = os.getpid()
        self._fresh = {}
        self._staged = {}
        try:
            if os.geteuid() != 0 or type(create) is not bool:
                raise ValueError()
            enrollment_path, journal_path = Path(enrollment_path), Path(journal_path)
            raw, self._enrollment_pin = _root_file(enrollment_path, 4096)
            policy = json.loads(raw)
            base = {'schema', 'supervisor_public_key', 'manifest_digest', 'source_backup_digest',
                    'provider_key_digest', 'provider_uid', 'provider_gid'}
            if (type(policy) is not dict or type(policy.get('schema')) is not int or policy['schema'] not in (1, 2)
                    or set(policy) != base | ({'journal_path'} if policy['schema'] == 2 else set()) or _canonical(policy) != raw
                    or (policy['schema'] == 2 and (type(policy['journal_path']) is not str
                        or _path(policy['journal_path']) != journal_path))
                    or any(type(policy[k]) is not int or not 0 < policy[k] < 2**32 - 1 for k in ('provider_uid', 'provider_gid'))
                    or not _HEX.fullmatch(policy['supervisor_public_key'])
                    or any(not isinstance(policy[k], str) or not _SHA.fullmatch(policy[k])
                           for k in ('manifest_digest', 'source_backup_digest', 'provider_key_digest'))):
                raise ValueError()
            self._policy, self._enrollment_path = policy, enrollment_path
            self._key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(policy['supervisor_public_key']))
            parent = _root_parent(journal_path, private=True)
            try:
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT | os.O_EXCL if create else 0)
                self._fd = os.open(journal_path.name, flags, 0o600, dir_fd=parent)
                info = os.fstat(self._fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or (info.st_dev, info.st_ino) != (os.stat(journal_path.name, dir_fd=parent, follow_symlinks=False).st_dev,
                                                           os.stat(journal_path.name, dir_fd=parent, follow_symlinks=False).st_ino)):
                    raise ValueError()
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if create:
                    os.fsync(self._fd); os.fsync(parent)
            finally:
                os.close(parent)
            self._journal_path = journal_path
            self._journal_pin = (info.st_dev, info.st_ino)
            if any(os.path.lexists(str(journal_path) + suffix) for suffix in ('-journal', '-wal', '-shm')):
                raise ValueError()
            self._db = sqlite3.connect(str(journal_path), timeout=2, isolation_level=None)
            self._holds_supported = create and policy['schema'] == 2
            if not create:
                objects = self._db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
                indexes = self._db.execute("PRAGMA index_list('admissions')").fetchall()
                has_holds = ('table', 'custody_holds', _HOLD_SCHEMA) in objects
                self._holds_supported = has_holds and policy['schema'] == 2
                expected = [('table', 'admissions', _SCHEMA)] + ([('table', 'custody_holds', _HOLD_SCHEMA)] if has_holds else [])
                if (sorted(objects) != sorted(expected) or len(indexes) != 2
                        or any(row[2] != 1 or row[3] not in ('pk', 'u') or row[4] != 0 for row in indexes)
                        or {tuple(item[2] for item in self._db.execute('PRAGMA index_info(' + repr(row[1]) + ')'))
                            for row in indexes} != {('operation_id',), ('nonce',)}):
                    raise ValueError()
                if has_holds:
                    holds = self._db.execute("PRAGMA index_list('custody_holds')").fetchall()
                    if (len(holds) != 2 or any(row[2] != 1 or row[3] not in ('pk', 'u') or row[4] != 0 for row in holds)
                            or {tuple(item[2] for item in self._db.execute('PRAGMA index_info(' + repr(row[1]) + ')'))
                                for row in holds} != {('target_id',), ('operation_id',)}): raise ValueError()
            self._db.execute('PRAGMA journal_mode=DELETE')
            self._db.execute('PRAGMA synchronous=FULL')
            if create:
                self._db.execute(_SCHEMA)
                self._db.execute(_HOLD_SCHEMA)
            self._check()
        except Exception:
            self.close()
            raise CustodianAdmissionError() from None

    def _check(self):
        if os.getpid() != self._pid or self._db is None:
            raise ValueError()
        _, pin = _root_file(self._enrollment_path, 4096)
        info = os.stat(self._journal_path, follow_symlinks=False)
        if (pin != self._enrollment_pin or (info.st_dev, info.st_ino) != self._journal_pin
                or (os.fstat(self._fd).st_dev, os.fstat(self._fd).st_ino) != self._journal_pin
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError()

    def reserve(self, payload, signature):
        """Verify and durably reserve; every repeated ID or nonce is rejected."""
        try:
            started = time.monotonic()
            self._check()
            if type(payload) is not dict or set(payload) != FIELDS or type(signature) is not bytes or len(signature) != 64:
                raise ValueError()
            payload = json.loads(_canonical(payload))
            for name in ('operation_id', 'command_id', 'run_id'):
                if type(payload[name]) is not str or str(UUID(payload[name])) != payload[name]: raise ValueError()
            if (payload['protocol'] != PROTOCOL or payload['role'] not in ('candidate', 'rollback')
                    or payload['target_id'] != 'engineering-hub:staging'
                    or type(payload['fencing_generation']) is not int or not 0 < payload['fencing_generation'] < 2**63
                    or type(payload['nonce']) is not str or not _HEX.fullmatch(payload['nonce'])
                    or any(payload[k] != self._policy[k] for k in ('manifest_digest', 'source_backup_digest', 'provider_key_digest'))
                    or any(type(payload[k]) is not int for k in ('issued_at_ms', 'expires_at_ms'))):
                raise ValueError()
            now = time.time_ns() // 1_000_000
            if not payload['issued_at_ms'] <= now < payload['expires_at_ms'] <= payload['issued_at_ms'] + 5000:
                raise ValueError()
            last_wall = now
            budget = (payload['expires_at_ms'] - now) / 1000
            raw = _canonical(payload)
            self._key.verify(signature, DOMAIN + raw)
            self._check()
            self._db.execute('BEGIN IMMEDIATE')
            try:
                now = time.time_ns() // 1_000_000
                elapsed = time.monotonic() - started
                if not last_wall <= now < payload['expires_at_ms'] or not 0 <= elapsed < budget: raise ValueError()
                last_wall = now
                if self._db.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] >= 4096: raise ValueError()
                self._db.execute('INSERT INTO admissions VALUES (?,?,?,?)',
                                 (payload['operation_id'], payload['nonce'], raw, signature))
                self._db.execute('COMMIT')
            except BaseException:
                if self._db.in_transaction: self._db.execute('ROLLBACK')
                raise
            # An error here is uncertain: the committed one-shot reservation is
            # intentionally retained and can never be replayed.
            self._check()
            now = time.time_ns() // 1_000_000
            elapsed = time.monotonic() - started
            if not last_wall <= now < payload['expires_at_ms'] or not 0 <= elapsed < budget:
                raise ValueError()
            digest = hashlib.sha256(raw).hexdigest()
            self._fresh[payload['operation_id']] = (digest, payload['expires_at_ms'], started + budget, now, started + elapsed)
            return digest
        except Exception:
            raise CustodianAdmissionError() from None

    def stage_quarantined_backup(self, *, operation_id, admission_digest, copy_policy_path, timeout_seconds):
        """Copy one enrolled frozen backup to private quarantine; never publish.

        Only an admission freshly committed by this process may start a copy.
        A durable exclusive destination is created before bytes are copied; any
        failure keeps it in quarantine and permanently prevents automatic retry.
        The backup must be independently approved as a coherent, quiescent
        SQLite backup. Root administrators and filesystem/mount isolation remain
        trusted. Accounting excludes writes by other privileged principals.
        Prepared or uncertain files remain held, and count against capacity.
        """
        source_fd = output_fd = source_parent = stage_fd = None
        try:
            self._check()
            if (type(operation_id) is not str or str(UUID(operation_id)) != operation_id
                    or type(admission_digest) is not str or not _HEX.fullmatch(admission_digest)
                    or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 120):
                raise ValueError()
            fresh = self._fresh.pop(operation_id, None)
            if fresh is None or fresh[0] != admission_digest:
                raise ValueError()
            last_mono = time.monotonic()
            last_wall = time.time_ns() // 1_000_000
            if last_wall < fresh[3] or last_mono < fresh[4]: raise ValueError()
            deadline = min(last_mono + timeout_seconds, fresh[2])
            def current():
                nonlocal last_mono, last_wall
                self._check()
                mono, wall = time.monotonic(), time.time_ns() // 1_000_000
                if not last_mono <= mono < deadline or not last_wall <= wall < fresh[1]: raise ValueError()
                last_mono, last_wall = mono, wall
            policy_path = _path(str(copy_policy_path))
            raw, policy_pin = _root_file(policy_path, 4096)
            policy = json.loads(raw)
            if (type(policy) is not dict or set(policy) != {'schema', 'source', 'source_digest', 'staging_directory', 'maximum_bytes', 'maximum_total_bytes', 'minimum_free_bytes'}
                    or _canonical(policy) != raw or type(policy['schema']) is not int or policy['schema'] != 1
                    or policy['source_digest'] != self._policy['source_backup_digest']
                    or type(policy['maximum_bytes']) is not int or not 0 < policy['maximum_bytes'] <= 512 * 1024 * 1024
                    or type(policy['maximum_total_bytes']) is not int or not policy['maximum_bytes'] <= policy['maximum_total_bytes'] <= 16 * 1024**3
                    or type(policy['minimum_free_bytes']) is not int or not 0 < policy['minimum_free_bytes'] <= 16 * 1024**3):
                raise ValueError()
            source, stage = _path(policy['source']), _path(policy['staging_directory'])
            if source.parent == stage or source == stage or stage in source.parents or source.parent in stage.parents:
                raise ValueError()
            source_parent = _root_parent(source, private=True)
            stage_fd = _root_parent(stage / 'entry', private=True)
            fcntl.flock(stage_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current()
            source_fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=source_parent)
            info = os.fstat(source_fd)
            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= policy['maximum_bytes']
                    or identity(info) != identity(os.stat(source.name, dir_fd=source_parent, follow_symlinks=False))
                    or any(os.path.lexists(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
                raise ValueError()
            header = os.pread(source_fd, 20, 0)
            if header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x01\x01': raise ValueError()
            used = 0
            inventory = {}
            with os.scandir(stage_fd) as entries:
                for entry in entries:
                    current()
                    suffix = next((s for s in ('.quarantine', '.ownership-intent') if entry.name.endswith(s)), None)
                    if suffix is None: raise ValueError()
                    operation = entry.name.removesuffix(suffix)
                    if str(UUID(operation)) != operation: raise ValueError()
                    item = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or stat.S_IMODE(item.st_mode) != 0o600: raise ValueError()
                    inventory[entry.name] = item
                    used += item.st_size
                    if used + info.st_size > policy['maximum_total_bytes']: raise ValueError()
            for name, item in inventory.items():
                if name.endswith('.ownership-intent'):
                    operation = name.removesuffix('.ownership-intent')
                    counterpart = inventory.get(operation + '.quarantine')
                    if item.st_uid != 0 or item.st_size > 4096 or counterpart is None: raise ValueError()
                    marker = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=stage_fd)
                    try:
                        marker_info = os.fstat(marker)
                        if (marker_info.st_dev, marker_info.st_ino) != (item.st_dev, item.st_ino): raise ValueError()
                        raw_marker = os.read(marker, 4097)
                    finally:
                        os.close(marker)
                    evidence = json.loads(raw_marker)
                    if (type(evidence) is not dict or set(evidence) != {'operation_id', 'admission_digest', 'device', 'inode', 'host_uid', 'host_gid', 'size', 'digest'}
                            or _canonical(evidence) != raw_marker or evidence['operation_id'] != operation
                            or type(evidence['admission_digest']) is not str or not _HEX.fullmatch(evidence['admission_digest'])
                            or type(evidence['digest']) is not str or not _SHA.fullmatch(evidence['digest'])
                            or any(type(evidence[k]) is not int or evidence[k] < 0 for k in ('device', 'inode', 'host_uid', 'host_gid', 'size'))
                            or (counterpart.st_dev, counterpart.st_ino) != (evidence['device'], evidence['inode'])
                            or counterpart.st_size != evidence['size']
                            or (counterpart.st_uid, counterpart.st_gid) not in ((0, 0), (evidence['host_uid'], evidence['host_gid']))):
                        raise ValueError()
                    record = self._db.execute('SELECT payload,signature FROM admissions WHERE operation_id=?', (operation,)).fetchone()
                    if (record is None or hashlib.sha256(record[0]).hexdigest() != evidence['admission_digest']): raise ValueError()
                    self._key.verify(record[1], DOMAIN + record[0])
                elif item.st_uid != 0 and name.removesuffix('.quarantine') + '.ownership-intent' not in inventory:
                    raise ValueError()
            space = os.fstatvfs(stage_fd)
            if used + info.st_size > policy['maximum_total_bytes'] or space.f_bavail * space.f_frsize < info.st_size + policy['minimum_free_bytes']:
                raise ValueError()
            name = operation_id + '.quarantine'
            output_fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                0o600, dir_fd=stage_fd)
            os.fsync(output_fd); os.fsync(stage_fd)
            digest = hashlib.sha256(); total = 0
            while True:
                current()
                block = os.read(source_fd, 1024 * 1024)
                if not block: break
                total += len(block)
                if total > info.st_size: raise ValueError()
                digest.update(block)
                view = memoryview(block)
                while view:
                    current()
                    written = os.write(output_fd, view)
                    if written <= 0: raise ValueError()
                    view = view[written:]
            os.fsync(output_fd); os.fsync(stage_fd)
            out = os.fstat(output_fd)
            named = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
            os.lseek(output_fd, 0, os.SEEK_SET)
            output_digest = hashlib.sha256(); output_size = 0
            while True:
                current()
                block = os.read(output_fd, 1024 * 1024)
                if not block: break
                output_size += len(block)
                if output_size > total: raise ValueError()
                output_digest.update(block)
            if (total != info.st_size or 'sha256:' + digest.hexdigest() != policy['source_digest']
                    or output_size != total or output_digest.digest() != digest.digest()
                    or identity(info) != identity(os.fstat(source_fd))
                    or identity(info) != identity(os.stat(source.name, dir_fd=source_parent, follow_symlinks=False))
                    or not stat.S_ISREG(out.st_mode) or out.st_uid != 0 or out.st_nlink != 1
                    or stat.S_IMODE(out.st_mode) != 0o600 or identity(out) != identity(os.fstat(output_fd))
                    or identity(out) != identity(named) or out.st_size != total
                    or any(os.path.lexists(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))
                    or _root_file(policy_path, 4096)[1] != policy_pin):
                raise ValueError()
            current()
            self._staged[operation_id] = (admission_digest, stage, (out.st_dev, out.st_ino),
                                           policy['source_digest'], total, deadline, last_wall, last_mono, fresh[1])
            return {'operation_id': operation_id, 'admission_digest': admission_digest,
                    'digest': 'sha256:' + digest.hexdigest(), 'size_bytes': total}
        except Exception:
            raise CustodianAdmissionError() from None
        finally:
            close_failed = False
            for fd in (output_fd, source_fd, source_parent, stage_fd):
                if fd is not None:
                    try: os.close(fd)
                    except OSError: close_failed = True
            if close_failed:
                self._staged.pop(operation_id, None)
                raise CustodianAdmissionError() from None

    def prepare_private_owner(self, *, operation_id, admission_digest, owner_policy_path):
        """Change a fresh quarantine file's owner without making it accessible.

        This is private preparation, not a handoff, publication, current-fence
        grant or mount approval. The root-private parent stays inaccessible to
        the app, and an intent marker and file remain on all failures. The root
        enrollment must independently identify the actual daemon process; this
        method checks its PID/start/maps but does not identify it as a daemon.
        Trusted root/daemon administrators can override this boundary.
        Preparation leaves the file and a root-owned intent marker held; later
        copies may account for them but cannot adopt, retry or publish them.
        A durable per-target hold is committed before ownership mutation and
        never expires or automatically releases, including after owner death.
        This protects only cooperating users of this enrolled journal; it does
        not establish that other host writers or older databases are excluded.
        """
        stage_fd = file_fd = marker_fd = None
        peer = None
        try:
            self._check()
            if type(operation_id) is not str or type(admission_digest) is not str or not self._holds_supported: raise ValueError()
            saved = self._staged.pop(operation_id, None)
            if saved is None or saved[0] != admission_digest: raise ValueError()
            _, stage, inode, digest, size, deadline, last_wall, last_mono, expires = saved
            policy_path = _path(str(owner_policy_path))
            raw, pin = _root_file(policy_path, 16384)
            policy = json.loads(raw)
            if (type(policy) is not dict or set(policy) != {'schema', 'boot_id', 'daemon_pid', 'daemon_start_ticks',
                    'uid_map', 'gid_map', 'app_uid', 'app_gid'} or _canonical(policy) != raw
                    or type(policy['schema']) is not int or policy['schema'] != 1
                    or type(policy['daemon_pid']) is not int or policy['daemon_pid'] <= 0
                    or type(policy['daemon_start_ticks']) is not int or policy['daemon_start_ticks'] <= 0
                    or any(type(policy[k]) is not str or len(policy[k]) > 65536 for k in ('boot_id', 'uid_map', 'gid_map'))
                    or any(type(policy[k]) is not int or not 0 < policy[k] < 2**32 - 1 or policy[k] == 10001
                           for k in ('app_uid', 'app_gid'))):
                raise ValueError()
            peer = _Process(policy['daemon_pid'], expected_start=policy['daemon_start_ticks'])
            def current():
                nonlocal last_wall, last_mono
                self._check(); peer.check()
                if _boot_id() != policy['boot_id'] or _root_file(policy_path, 16384)[1] != pin: raise ValueError()
                uid_map = Path(f"/proc/{peer.pid}/uid_map").read_text()
                gid_map = Path(f"/proc/{peer.pid}/gid_map").read_text()
                if uid_map != policy['uid_map'] or gid_map != policy['gid_map']: raise ValueError()
                peer.check()
                mono, wall = time.monotonic(), time.time_ns() // 1_000_000
                if not last_mono <= mono < deadline or not last_wall <= wall < expires: raise ValueError()
                last_mono, last_wall = mono, wall
                return _mapped_id(uid_map, policy['app_uid']), _mapped_id(gid_map, policy['app_gid'])
            host_uid, host_gid = current()
            if (not 0 < host_uid < 2**32 - 1 or not 0 < host_gid < 2**32 - 1
                    or host_uid in (10001, self._policy['provider_uid'], _mapped_id(policy['uid_map'], 10001))
                    or host_gid in (10001, self._policy['provider_gid'], _mapped_id(policy['gid_map'], 10001))):
                raise ValueError()
            stage_fd = _root_parent(stage / 'entry', private=True)
            fcntl.flock(stage_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            name = operation_id + '.quarantine'
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=stage_fd)
            before = os.fstat(file_fd)
            named = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size != size
                    or (before.st_dev, before.st_ino) != inode or (named.st_dev, named.st_ino) != inode): raise ValueError()
            hashed = hashlib.sha256()
            while True:
                current(); block = os.read(file_fd, 1024 * 1024)
                if not block: break
                hashed.update(block)
            if 'sha256:' + hashed.hexdigest() != digest or current() != (host_uid, host_gid): raise ValueError()
            self._db.execute('BEGIN IMMEDIATE')
            try:
                row = self._db.execute('SELECT payload,signature FROM admissions WHERE operation_id=?', (operation_id,)).fetchone()
                if row is None or hashlib.sha256(row[0]).hexdigest() != admission_digest: raise ValueError()
                self._key.verify(row[1], DOMAIN + row[0])
                admitted = json.loads(row[0])
                if (type(admitted) is not dict or _canonical(admitted) != row[0]
                        or admitted.get('operation_id') != operation_id
                        or admitted.get('target_id') != 'engineering-hub:staging'
                        or type(admitted.get('fencing_generation')) is not int
                        or not 0 < admitted['fencing_generation'] < 2**63
                        or current() != (host_uid, host_gid)):
                    raise ValueError()
                hold = (admitted['target_id'], operation_id, admission_digest,
                        admitted['fencing_generation'], inode[0], inode[1], host_uid, host_gid, pin[1])
                self._db.execute('INSERT INTO custody_holds VALUES (?,?,?,?,?,?,?,?,?)', hold)
                self._db.execute('COMMIT')
            except BaseException:
                if self._db.in_transaction: self._db.execute('ROLLBACK')
                raise
            # From this point an interruption, failed check, or uncertain write
            # leaves the target held; no automatic release or takeover exists.
            if (self._db.execute('SELECT * FROM custody_holds WHERE target_id=?', (admitted['target_id'],)).fetchone() != hold
                    or current() != (host_uid, host_gid)): raise ValueError()
            marker_fd = os.open(operation_id + '.ownership-intent', os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=stage_fd)
            marker = _canonical({'operation_id': operation_id, 'admission_digest': admission_digest,
                                 'device': inode[0], 'inode': inode[1], 'host_uid': host_uid, 'host_gid': host_gid,
                                 'size': size, 'digest': digest})
            if os.write(marker_fd, marker) != len(marker): raise ValueError()
            os.fsync(marker_fd); os.fsync(stage_fd)
            if (self._db.execute('SELECT * FROM custody_holds WHERE target_id=?', (admitted['target_id'],)).fetchone() != hold
                    or current() != (host_uid, host_gid)): raise ValueError()
            os.fchown(file_fd, host_uid, host_gid)
            os.fsync(file_fd); os.fsync(stage_fd)
            after = os.fstat(file_fd)
            named = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
            if ((after.st_dev, after.st_ino) != inode or (named.st_dev, named.st_ino) != inode
                    or after.st_uid != host_uid or after.st_gid != host_gid or after.st_nlink != 1
                    or stat.S_IMODE(after.st_mode) != 0o600 or after.st_size != size
                    or self._db.execute('SELECT * FROM custody_holds WHERE target_id=?', (admitted['target_id'],)).fetchone() != hold
                    or current() != (host_uid, host_gid)): raise ValueError()
            return {'operation_id': operation_id, 'admission_digest': admission_digest,
                    'state': 'private_owner_prepared', 'device': inode[0], 'inode': inode[1]}
        except Exception:
            raise CustodianAdmissionError() from None
        finally:
            failed = False
            for fd in (marker_fd, file_fd, stage_fd):
                if fd is not None:
                    try: os.close(fd)
                    except OSError: failed = True
            if peer is not None:
                try: peer.close()
                except OSError: failed = True
            if failed: raise CustodianAdmissionError() from None

    def exchange(self, channel, *, operation_id, timeout_seconds, provider_uid, provider_gid):
        """Request and reserve one observation on an owned connected seqpacket.

        The caller owns the connected channel and its host custody. The signed
        response, fresh nonce and independent enrollment authenticate the issuer;
        this does not prove peer isolation or authorize an effect.
        """
        child = None
        try:
            if (type(channel) is not socket.socket or channel.family != socket.AF_UNIX
                    or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                    or type(operation_id) is not str or str(UUID(operation_id)) != operation_id
                    or not os.path.isabs(sys.executable)
                    or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 5
                    or type(provider_uid) is not int or not 0 < provider_uid < 2**32 - 1
                    or type(provider_gid) is not int or not 0 < provider_gid < 2**32 - 1):
                raise ValueError()
            if provider_uid != self._policy['provider_uid'] or provider_gid != self._policy['provider_gid']:
                raise ValueError()
            deadline = time.monotonic() + timeout_seconds
            nonce = secrets.token_hex(32)
            request = _canonical({'protocol': 'engineering-hub.state-custodian-request.v1',
                                  'operation_id': operation_id, 'nonce': nonce})
            child = subprocess.Popen([sys.executable, '-I', '-c', _TRANSPORT, str(channel.fileno())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=(channel.fileno(),), close_fds=True, user=provider_uid, group=provider_gid,
                extra_groups=(), cwd='/', env={'LANG': 'C.UTF-8'})
            child.stdin.write(request + b'\n'); child.stdin.flush()
            def read_exact(size):
                result = b''
                while len(result) < size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]: raise ValueError()
                    chunk = os.read(child.stdout.fileno(), size - len(result))
                    if not chunk: raise ValueError()
                    result += chunk
                return result
            size = struct.unpack('!I', read_exact(4))[0]
            if not 0 < size <= 16384: raise ValueError()
            raw = read_exact(size)
            response = json.loads(raw)
            if (type(response) is not dict or set(response) != {'payload', 'signature'}
                    or _canonical(response) != raw or type(response['payload']) is not dict
                    or response['payload'].get('operation_id') != operation_id
                    or response['payload'].get('nonce') != nonce or type(response['signature']) is not str
                    or not re.fullmatch(r'[0-9a-f]{128}', response['signature'])):
                raise ValueError()
            if time.monotonic() >= deadline: raise ValueError()
            result = self.reserve(response['payload'], bytes.fromhex(response['signature']))
            if time.monotonic() >= deadline: raise ValueError()
        except BaseException as error:
            self._fresh.pop(operation_id, None)
            if not isinstance(error, Exception): raise
            raise CustodianAdmissionError() from None
        finally:
            try:
                if child is not None:
                    if child.stdin is not None: child.stdin.close()
                    try: child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        child.kill(); child.wait(timeout=1)
                    if child.stdout is not None: child.stdout.close()
            except BaseException:
                self._fresh.pop(operation_id, None)
                raise
        if time.monotonic() >= deadline:
            self._fresh.pop(operation_id, None)
            raise CustodianAdmissionError()
        return result

    def close(self):
        if self._db is not None:
            self._db.close(); self._db = None
        if self._fd is not None:
            os.close(self._fd); self._fd = None
