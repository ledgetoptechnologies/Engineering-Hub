"""Exclusive journaled leases for an operator-provisioned scratch filesystem.

This module never provisions, mounts or resizes storage. Capacity observations
are admission facts, not a reservation against other filesystem writers.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .core import Rejected, ensure


def _identity(value):
    return [value.st_dev, value.st_ino]


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


def filesystem_facts(path):
    """Read actual Linux mount/statvfs facts; no external program or fallback."""
    ensure(os.name == 'posix', 'scratch_linux_required')
    rows = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields, tail = left.split(), right.split()
        if _unescape(fields[4]) == str(path):
            rows.append((fields, tail))
    ensure(len(rows) == 1, 'scratch_dedicated_mount_required')
    fields, tail = rows[0]
    fs = os.statvfs(path)
    return {'mount_id': int(fields[0]), 'mount_root': _unescape(fields[3]),
            'mount_device': fields[2], 'filesystem': tail[0],
            'options': sorted(set(fields[5].split(',')) | set(tail[2].split(','))),
            'capacity_bytes': fs.f_blocks * fs.f_frsize,
            'free_bytes': fs.f_bavail * fs.f_frsize,
            'capacity_inodes': fs.f_files, 'free_inodes': fs.f_favail,
            'block_size': fs.f_frsize, 'blocks': fs.f_blocks, 'filesystem_id': fs.f_fsid}


@dataclass(frozen=True)
class ScratchSlot:
    path: Path
    filesystem_uuid: str
    mount_device: str
    ceiling_bytes: int
    ceiling_inodes: int
    free_floor_bytes: int
    free_floor_inodes: int
    container_uid: int
    container_gid: int
    mapped_uid: int
    mapped_gid: int

    @classmethod
    def from_config(cls, value):
        fields = set(cls.__dataclass_fields__)
        ensure(type(value) is dict and set(value) == fields, 'scratch_config_schema')
        path = value['path']
        ensure(type(path) is str and path.startswith('/') and not any(c in path for c in ('\\', ',', '\x00', '\n', '\r'))
               and all(part not in ('', '.', '..') for part in path[1:].split('/')),
               'scratch_config_path')
        ensure(type(value['filesystem_uuid']) is str and
               str(uuid.UUID(value['filesystem_uuid'])) == value['filesystem_uuid'], 'scratch_config_uuid')
        ensure(type(value['mount_device']) is str and
               re.fullmatch(r'[0-9]+:[0-9]+', value['mount_device']), 'scratch_config_device')
        for key in fields - {'path', 'filesystem_uuid', 'mount_device'}:
            ensure(type(value[key]) is int and 0 < value[key] < 2 ** 63, 'scratch_config_integer')
        ensure(value['free_floor_bytes'] < value['ceiling_bytes']
               and value['free_floor_inodes'] < value['ceiling_inodes'], 'scratch_config_floor')
        return cls(**{**value, 'path': Path(path)})

    def acquire(self, worker_root, db):
        """Lifetime kernel exclusion across workers sharing this exact slot."""
        import fcntl
        captured = self.verify()
        ensure(captured['root_identity'][0] != Path(worker_root).stat().st_dev,
               'scratch_journal_filesystem_must_differ')
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            ensure(_identity(os.fstat(fd)) == captured['root_identity'], 'scratch_root_drift')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.bind_journal(worker_root, db, fd, captured)
            return fd
        except Exception:
            os.close(fd)
            raise Rejected('scratch_slot_busy_or_changed', 409) from None

    def bind_journal(self, worker_root, db, root_fd, captured):
        """Permanent slot ownership, independent of process/DB-local locks."""
        worker_root = Path(worker_root)
        journal = worker_root / 'journal.sqlite3'
        journal_info = journal.stat(follow_symlinks=False)
        ensure(stat.S_ISREG(journal_info.st_mode) and journal_info.st_nlink == 1,
               'scratch_journal_identity')
        expected = {'schema': 1, 'worker_root': _identity(worker_root.stat(follow_symlinks=False)),
                    'journal': _identity(journal_info), 'slot_root': captured['root_identity'],
                    'filesystem_uuid': self.filesystem_uuid,
                    'policy_sha256': hashlib.sha256(json.dumps(
                        {**self.__dict__, 'path': str(self.path)}, sort_keys=True).encode()).hexdigest()}
        name = '.worker-journal.json'
        entries = list(self.path.iterdir())
        if not any(entry.name == name for entry in entries):
            ensure(not entries and not db.execute("SELECT 1 FROM scratch_leases WHERE state!='released'").fetchone(),
                   'scratch_unowned_existing_state', 409)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
            try:
                os.fchmod(fd, 0o600)
                data = json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()
                with os.fdopen(fd, 'wb', closefd=False) as stream:
                    stream.write(data); stream.flush(); os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(root_fd)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            info = os.fstat(fd)
            ensure(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                   and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 4096,
                   'scratch_owner_marker_identity')
            raw = os.read(fd, 4097)
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    ensure(key not in result, 'scratch_owner_marker_invalid')
                    result[key] = value
                return result
            actual = json.loads(raw, object_pairs_hook=unique)
            # Canonical equality also rejects bool-for-int and unknown keys.
            ensure(json.dumps(actual, sort_keys=True, separators=(',', ':')) ==
                   json.dumps(expected, sort_keys=True, separators=(',', ':')), 'scratch_foreign_journal', 409)
            after = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            ensure(_identity(after) == _identity(info) and after.st_size == info.st_size,
                   'scratch_owner_marker_identity')
        finally:
            os.close(fd)
        self.check_inventory(db)

    def check_inventory(self, db):
        expected = {'.worker-journal.json': None}
        for row in db.execute("SELECT command_id,capture FROM scratch_leases WHERE state!='released'"):
            record = json.loads(row['capture'])
            name = 'job-' + str(uuid.UUID(row['command_id']))
            ensure(record['name'] == name, 'scratch_binding_drift')
            expected[name] = record['directory_identity']
        for path in self.path.iterdir():
            ensure(path.name in expected, 'scratch_unknown_entry', 409)
            if path.name != '.worker-journal.json':
                info = path.stat(follow_symlinks=False)
                # A mkdir-before-journal-capture crash is unknown; never adopt.
                ensure(expected[path.name] is not None and stat.S_ISDIR(info.st_mode)
                       and _identity(info) == expected[path.name], 'scratch_directory_drift', 409)

    def verify(self, captured=None, *, admission=True):
        # Every ancestor is held open for the observation, with final-component
        # NOFOLLOW at each step. Backend repeats this before source commands.
        descriptors = []
        try:
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(fd)
            for component in self.path.parts[1:]:
                fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                descriptors.append(fd)
            info = os.fstat(fd)
            ensure(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
                   'scratch_private_root_required')
            ensure(_identity(self.path.stat(follow_symlinks=False)) == _identity(info), 'scratch_root_drift')
            facts = filesystem_facts(self.path)
            ensure(facts['mount_root'] == '/' and facts['mount_device'] == self.mount_device
                   and facts['filesystem'] == 'ext4', 'scratch_filesystem_drift')
            options = set(facts['options'])
            ensure({'rw', 'nosuid', 'nodev'} <= options and not {'ro', 'noexec'} & options,
                   'scratch_mount_policy')
            major, minor = (int(value) for value in self.mount_device.split(':'))
            ensure(info.st_dev == os.makedev(major, minor), 'scratch_device_drift')
            named = Path('/dev/disk/by-uuid') / self.filesystem_uuid
            target = named.stat()
            ensure(stat.S_ISBLK(target.st_mode) and target.st_rdev == info.st_dev, 'scratch_uuid_drift')
            ensure(0 < facts['capacity_bytes'] <= self.ceiling_bytes
                   and 0 < facts['capacity_inodes'] <= self.ceiling_inodes,
                   'scratch_capacity_unverified')
            if admission:
                ensure(facts['free_bytes'] >= self.free_floor_bytes
                       and facts['free_inodes'] >= self.free_floor_inodes, 'scratch_capacity_exhausted', 409)
            stable = {key: facts[key] for key in ('mount_id', 'mount_root', 'mount_device',
                      'filesystem', 'capacity_bytes', 'capacity_inodes', 'block_size', 'blocks', 'filesystem_id')}
            stable['root_identity'] = _identity(info)
            if captured is not None:
                ensure(stable == captured, 'scratch_filesystem_drift')
            return stable
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def reserve(self, db, command_id, job_id, resource_ids):
        """Caller owns the SAME transaction that inserts accepted operation."""
        ensure(not db.execute("SELECT 1 FROM scratch_leases WHERE state!='released'").fetchone(),
               'scratch_occupied', 409)
        captured = self.verify()
        self.recheck_journal(db, captured)
        name = 'job-' + str(uuid.UUID(command_id))
        ensure(not (self.path / name).exists() and not (self.path / name).is_symlink(), 'scratch_path_conflict', 409)
        record = {'slot': {**self.__dict__, 'path': str(self.path)}, 'captured': captured,
                  'name': name, 'resource_ids': resource_ids, 'directory_identity': None}
        db.execute('INSERT INTO scratch_leases VALUES (?,?,?,?,?,?)',
                   (command_id, job_id, 'occupied', json.dumps(record, sort_keys=True), '{}', '{}'))

    def recheck_journal(self, db, captured):
        root_fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            ensure(_identity(os.fstat(root_fd)) == captured['root_identity'], 'scratch_root_drift')
            # Recheck durable ownership and unknown entries under the Service's
            # lifetime flock, not just during process construction.
            journal_path = db.execute('PRAGMA database_list').fetchone()[2]
            self.bind_journal(Path(journal_path).parent, db, root_fd, captured)
        finally:
            os.close(root_fd)

    def lease(self, db, command_id):
        row = db.execute('SELECT * FROM scratch_leases WHERE command_id=?', (command_id,)).fetchone()
        ensure(row is not None and row['state'] == 'occupied', 'scratch_lease_missing', 409)
        record = json.loads(row['capture'])
        ensure(type(record) is dict and set(record) == {'slot', 'captured', 'name', 'resource_ids', 'directory_identity'}
               and record['name'] == 'job-' + str(uuid.UUID(command_id)), 'scratch_binding_drift')
        ensure(record['slot'] == {**self.__dict__, 'path': str(self.path)}, 'scratch_config_drift')
        operation = db.execute('SELECT job_id,payload FROM operations WHERE command_id=?', (command_id,)).fetchone()
        ensure(operation is not None and operation['job_id'] == row['job_id']
               and json.loads(operation['payload']).get('resource_ids') == record['resource_ids'],
               'scratch_binding_drift')
        return ScratchLease(self, db, command_id, row['job_id'], record)


class ScratchLease:
    """Private worker capability; never constructed from a source request."""
    def __init__(self, slot, db, command_id, job_id, record):
        self.slot, self._db = slot, db
        self.command_id, self.job_id, self._record = command_id, job_id, record
        self.path = slot.path / record['name']
        self._absent = False

    def __getattr__(self, name):
        if name in ScratchSlot.__dataclass_fields__ and name != 'path':
            return getattr(self.slot, name)
        if name in ('device', 'inode'):
            identity = self._record['directory_identity']
            ensure(identity is not None, 'scratch_not_materialized')
            return identity[0 if name == 'device' else 1]
        raise AttributeError(name)

    @property
    def resource_ids(self):
        # Return a copy; source requests cannot modify the captured binding.
        return dict(self._record['resource_ids'])

    def materialize(self):
        self.slot.verify(self._record['captured'])
        ensure(self._record['directory_identity'] is None, 'scratch_already_materialized')
        # No chown or capabilities: private0700 parent; only this directory is
        # bound into the source namespace. Unknown/hostile descendants hold.
        self.path.mkdir(mode=0o777)
        self.path.chmod(0o777)
        self._record['directory_identity'] = _identity(self.path.stat(follow_symlinks=False))
        self._db.execute('UPDATE scratch_leases SET capture=? WHERE command_id=?',
                         (json.dumps(self._record, sort_keys=True), self.command_id))
        self._db.commit()
        self.verify()

    def verify(self, *, admission=True):
        facts = self.slot.verify(self._record['captured'], admission=admission)
        value = self.path.stat(follow_symlinks=False)
        ensure(stat.S_ISDIR(value.st_mode) and _identity(value) == self._record['directory_identity']
               and value.st_dev == facts['root_identity'][0]
               and value.st_uid == os.getuid() and stat.S_IMODE(value.st_mode) == 0o777,
               'scratch_directory_drift')
        return {**facts, 'directory_identity': _identity(value)}

    def confirm_backend_absent(self, command_id):
        """Backend's scoped Engine/owned-container/visible-mount proof only.

        Not proof about arbitrary privileged host processes/namespaces. The
        dedicated slot must be exclusively provisioned to this serial worker.
        """
        ensure(command_id == self.command_id, 'scratch_command_drift')
        self.verify(admission=False)
        self._absent = True

    def absence_receipt(self):
        ensure(self._absent, 'scratch_backend_absence_unproven', 409)
        return {'command_id': self.command_id, 'job_id': self.job_id,
                'scope': 'owned_containers_engine_binds_worker_visible_mounts',
                'directory_identity': self._record['directory_identity']}

    @property
    def backend_absent(self):
        return self._absent

    def release(self):
        """Terminal receipt already committed; no retry after unknown cleanup."""
        ensure(self._absent, 'scratch_backend_absence_unproven', 409)
        row = self._db.execute('SELECT state FROM operations WHERE command_id=?', (self.command_id,)).fetchone()
        ensure(row is not None and row['state'] in ('succeeded', 'failed'), 'scratch_receipt_not_terminal', 409)
        stored = self._db.execute('SELECT * FROM scratch_leases WHERE command_id=?', (self.command_id,)).fetchone()
        ensure(stored is not None and stored['state'] == 'occupied' and stored['job_id'] == self.job_id
               and json.loads(stored['capture']) == self._record
               and json.loads(stored['absence']) == self.absence_receipt(), 'scratch_binding_drift')
        self.verify(admission=False)
        # The trusted, mapped-UID backend sweeper already removed contents.
        # Host worker never recursively traverses source-owned private trees.
        self._db.execute("UPDATE scratch_leases SET state='cleaning' WHERE command_id=?", (self.command_id,))
        self._db.commit()
        fd = os.open(self.slot.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            ensure(_identity(os.fstat(fd)) == self._record['captured']['root_identity'], 'scratch_root_drift')
            current = os.stat(self._record['name'], dir_fd=fd, follow_symlinks=False)
            ensure(_identity(current) == self._record['directory_identity'] and stat.S_ISDIR(current.st_mode),
                   'scratch_directory_drift')
            os.rmdir(self._record['name'], dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        receipt = {'command_id': self.command_id, 'job_id': self.job_id,
                   'directory_identity': self._record['directory_identity'], 'absent': True}
        self._db.execute("UPDATE scratch_leases SET state='released',receipt=? WHERE command_id=?",
                         (json.dumps(receipt, sort_keys=True), self.command_id))
        self._db.commit()
