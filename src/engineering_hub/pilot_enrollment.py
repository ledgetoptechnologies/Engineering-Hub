"""Explicit offline enrollment of a cold, existing Hub ledger; never startup.

No command stops writers, backs up data, repairs state, changes permissions or
activates a runtime. A newly acquired lock is not proof legacy writers stopped.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import uuid

from .pilot_ownership import _EPOCH_SCHEMA, _SCHEMA, _local_filesystem
from .private_browser_socket import _check_parent, _private_parent


class EnrollmentError(RuntimeError):
    def __init__(self):
        super().__init__('offline Hub enrollment unavailable; preserve artifacts for inspection')


_DDL = {
    'hub_entities': 'CREATE TABLE hub_entities (id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL)',
    'hub_requests': 'CREATE TABLE hub_requests (actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, entity_id TEXT NOT NULL, PRIMARY KEY(actor,key))',
    'hub_audit': 'CREATE TABLE hub_audit (sequence INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_id TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL)',
    'hub_outbox': 'CREATE TABLE hub_outbox (sequence INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, entity_id TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL)',
    'hub_events': 'CREATE TABLE hub_events (run_id TEXT NOT NULL, command_id TEXT NOT NULL, event TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(run_id,command_id,event))',
    'hub_run_event_receipts': 'CREATE TABLE hub_run_event_receipts (run_id TEXT NOT NULL, event_id TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(run_id,event_id))',
    'hub_target_fences': 'CREATE TABLE hub_target_fences (target TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation > 0))',
    'hub_run_requests': 'CREATE TABLE hub_run_requests (actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(actor,key))',
    'sqlite_sequence': 'CREATE TABLE sqlite_sequence(name,seq)',
    'auth_sessions': 'CREATE TABLE auth_sessions (token_hash TEXT PRIMARY KEY, github_id INTEGER NOT NULL, csrf_token TEXT NOT NULL, expires REAL NOT NULL)',
    'auth_states': 'CREATE TABLE auth_states (state_hash TEXT PRIMARY KEY, verifier TEXT NOT NULL, expires REAL NOT NULL)',
    'auth_access_sessions': 'CREATE TABLE auth_access_sessions (token_hash TEXT PRIMARY KEY, binding TEXT NOT NULL, actor TEXT NOT NULL, csrf_token TEXT NOT NULL, expires REAL NOT NULL)',
    'auth_access_identities': 'CREATE TABLE auth_access_identities (identity_key TEXT PRIMARY KEY, binding TEXT NOT NULL, actor TEXT NOT NULL)',
    'hub_work_jobs': 'CREATE TABLE hub_work_jobs (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, action_key TEXT NOT NULL, environment TEXT NOT NULL, machine_id TEXT NOT NULL, project TEXT NOT NULL, approval_id TEXT, target TEXT NOT NULL, requires_execution_proof INTEGER NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, worker TEXT, fence INTEGER, lease_until REAL, created_at REAL NOT NULL, UNIQUE(actor,request_id))',
    'hub_work_machines': 'CREATE TABLE hub_work_machines (machine_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, job_id TEXT)',
    'hub_work_policy': 'CREATE TABLE hub_work_policy (id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0)',
    'hub_delivery_namespace': 'CREATE TABLE hub_delivery_namespace (id INTEGER PRIMARY KEY CHECK(id=1), namespace TEXT NOT NULL)',
    'hub_delivery_receipts': 'CREATE TABLE hub_delivery_receipts (sequence INTEGER NOT NULL, recipient TEXT NOT NULL, request_json TEXT NOT NULL, message_id TEXT, PRIMARY KEY(sequence, recipient))',
    'hub_delivery_sender': 'CREATE TABLE hub_delivery_sender (id INTEGER PRIMARY KEY CHECK(id=1), principal TEXT NOT NULL)',
}
_CORE = frozenset(list(_DDL)[:9])
_GROUPS = (
    frozenset({'auth_sessions', 'auth_states'}),
    frozenset({'auth_access_sessions', 'auth_access_identities'}),
    frozenset({'hub_work_jobs', 'hub_work_machines', 'hub_work_policy'}),
    frozenset({'hub_delivery_namespace', 'hub_delivery_receipts', 'hub_delivery_sender'}),
)
_OWNER_TABLES = frozenset({'hub_pilot_owner', 'hub_pilot_owner_epochs'})
_MAX_DB_BYTES = 256 * 1024 * 1024
_MAX_ROWS = 1_000_000
_MAX_CELL_BYTES = 1024 * 1024
_MAX_LOGICAL_BYTES = 512 * 1024 * 1024


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def plan_digest(plan):
    return hashlib.sha256(canonical_json(plan).encode()).hexdigest()


def _normal(sql):
    normal = re.sub(r'\s+', ' ', sql.replace('IF NOT EXISTS ', '')).strip()
    return re.sub(r'\s*([(),])\s*', r'\1', normal)


def _stat(value, *, parent=False):
    fields = dict(device=value.st_dev, inode=value.st_ino, uid=value.st_uid,
                  gid=value.st_gid, mode=stat.S_IMODE(value.st_mode))
    if not parent:
        fields.update(nlink=value.st_nlink, size=value.st_size,
                      mtime_ns=value.st_mtime_ns, ctime_ns=value.st_ctime_ns)
    return fields


class _ColdFile:
    def __init__(self, path):
        if sys.platform != 'linux' or not isinstance(path, (str, Path)):
            raise EnrollmentError()
        self.path = Path(path)
        if not self.path.is_absolute() or str(self.path) != os.path.abspath(self.path):
            raise EnrollmentError()
        if self.path.parent.resolve(strict=True) != self.path.parent:
            raise EnrollmentError()
        self.parent_fd, _ = _private_parent(self.path)
        try:
            self.parent = _stat(os.fstat(self.parent_fd), parent=True)
            _local_filesystem(self.parent_fd)
            self.check()
        except BaseException:
            os.close(self.parent_fd)
            raise

    def close(self):
        os.close(self.parent_fd)

    def check_parent(self):
        expected = (self.parent['device'], self.parent['inode'])
        _check_parent(self.path, self.parent_fd, expected)
        # Rewalk all ancestors without following symlinks, not just the leaf.
        fresh_fd, identity = _private_parent(self.path)
        try:
            if identity != expected or _stat(os.fstat(fresh_fd), parent=True) != self.parent:
                raise EnrollmentError()
        finally:
            os.close(fresh_fd)

    def check(self, *, lock_expected=None):
        self.check_parent()
        opened = os.fstat(self.parent_fd)
        named = os.stat(self.path.parent, follow_symlinks=False)
        if (_stat(opened, parent=True) != self.parent or _stat(named, parent=True) != self.parent
                or not stat.S_ISDIR(named.st_mode) or named.st_uid != os.getuid()
                or stat.S_IMODE(named.st_mode) != 0o700
                or self.path.parent.resolve(strict=True) != self.path.parent):
            raise EnrollmentError()
        value = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                or value.st_dev != opened.st_dev or stat.S_IMODE(value.st_mode) != 0o600
                or value.st_nlink != 1 or not 0 < value.st_size <= _MAX_DB_BYTES):
            raise EnrollmentError()
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                os.stat(self.path.name + suffix, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise EnrollmentError()
        lock_name = self.path.name + '.pilot-owner.lock'
        try:
            lock = os.stat(lock_name, dir_fd=self.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if lock_expected is not None:
                raise EnrollmentError()
        else:
            if lock_expected is None or _stat(lock) != lock_expected:
                raise EnrollmentError()
        return _stat(value)

    def content_hash(self):
        before = self.check()
        fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.parent_fd)
        try:
            if _stat(os.fstat(fd)) != before:
                raise EnrollmentError()
            _local_filesystem(fd)
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            if _stat(os.fstat(fd)) != before or self.check() != before:
                raise EnrollmentError()
            return before, digest.hexdigest()
        finally:
            # Only before SQLite is opened. Closing a raw existing-database FD
            # during a transaction could release that process's POSIX locks.
            os.close(fd)


def _typed(value):
    if value is None:
        return ['null']
    if type(value) is bytes:
        if len(value) > _MAX_CELL_BYTES:
            raise EnrollmentError()
        return ['blob', value.hex()]
    if type(value) in {str, int, float}:
        if type(value) is str and len(value.encode('utf-8')) > _MAX_CELL_BYTES:
            raise EnrollmentError()
        return [type(value).__name__, value]
    raise EnrollmentError()


def _snapshot(db, *, enrolled=False):
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_CELL_BYTES)
    if db.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0] > 128:
        raise EnrollmentError()
    if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
        raise EnrollmentError()
    objects = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
    if enrolled:
        objects = [row for row in objects if row[2] not in _OWNER_TABLES]
    tables = {row[1] for row in objects if row[0] == 'table'}
    if not _CORE <= tables or not tables <= _DDL.keys():
        raise EnrollmentError()
    for group in _GROUPS:
        if tables & group and not group <= tables:
            raise EnrollmentError()
    for kind, name, table, sql in objects:
        if kind == 'table':
            if _normal(sql) != _normal(_DDL[name]):
                raise EnrollmentError()
        elif kind != 'index' or sql is not None or not name.startswith('sqlite_autoindex_') or table not in tables:
            raise EnrollmentError()
    # No controller ownership or incomplete external effects may be adopted.
    for (raw,) in db.execute("SELECT data FROM hub_entities WHERE kind='run'"):
        run = json.loads(raw)
        if type(run) is not dict or run.get('status') not in {'succeeded', 'rolled_back'}:
            raise EnrollmentError()
    if 'hub_work_jobs' in tables:
        if db.execute("SELECT 1 FROM hub_work_jobs WHERE state NOT IN ('succeeded','failed','cancelled') LIMIT 1").fetchone():
            raise EnrollmentError()
        if db.execute('SELECT 1 FROM hub_work_machines WHERE job_id IS NOT NULL LIMIT 1').fetchone():
            raise EnrollmentError()
    if 'hub_delivery_receipts' in tables:
        for (message_id,) in db.execute('SELECT message_id FROM hub_delivery_receipts'):
            # A NULL or malformed ID may represent an ambiguous bus acceptance;
            # enrollment must not turn that into permission to resend mail.
            if type(message_id) is not str or str(uuid.UUID(message_id)) != message_id:
                raise EnrollmentError()
    result = {'schema_sha256': hashlib.sha256(canonical_json(objects).encode()).hexdigest(),
              'user_version': db.execute('PRAGMA user_version').fetchone()[0],
              'application_id': db.execute('PRAGMA application_id').fetchone()[0], 'tables': {}}
    total_rows = total_bytes = 0
    for table in sorted(tables):
        digest = hashlib.sha256()
        count = 0
        for row in db.execute('SELECT rowid,* FROM "' + table + '" ORDER BY rowid'):
            encoded = canonical_json([_typed(value) for value in row]).encode()
            total_rows += 1
            total_bytes += len(encoded)
            if total_rows > _MAX_ROWS or total_bytes > _MAX_LOGICAL_BYTES:
                raise EnrollmentError()
            digest.update(str(len(encoded)).encode() + b':' + encoded)
            count += 1
        result['tables'][table] = {'rows': count, 'sha256': digest.hexdigest()}
    return result


@contextmanager
def _guard(path):
    cold = None
    try:
        cold = _ColdFile(path)
        yield cold
    except Exception:
        raise EnrollmentError() from None
    finally:
        if cold is not None:
            try:
                cold.close()
            except Exception:
                raise EnrollmentError() from None


def _inspect(cold):
    before, content_hash = cold.content_hash()
    # immutable avoids journal recovery/SHM creation; admissible only after the
    # operator stopped writers and strict absence of all SQLite sidecars.
    db = sqlite3.connect(cold.path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=0)
    try:
        db.execute('PRAGMA temp_store=MEMORY')
        snapshot = _snapshot(db)
    finally:
        db.close()
    after, after_hash = cold.content_hash()
    if before != after or content_hash != after_hash:
        raise EnrollmentError()
    return dict(schema_version=1, operation='enroll_cold_hub_database', database=str(cold.path),
                parent=cold.parent, file=before, content_sha256=content_hash, snapshot=snapshot,
                lock_name=cold.path.name + '.pilot-owner.lock')


def inspect_plan(database_path):
    """Return only fingerprints of an already cold database; performs no writes."""
    with _guard(database_path) as cold:
        return _inspect(cold)


def apply_plan(plan, expected_digest, *, writers_stopped=False, backup_verified=False):
    """Explicit operator-only enrollment, not proof of quiescence or backup.

    After lock creation any failure leaves artifacts intact for inspection.
    Never call this from a server, constructor, agent endpoint or retry loop.
    """
    if (type(plan) is not dict or type(expected_digest) is not str
            or not re.fullmatch('[0-9a-f]{64}', expected_digest)
            or writers_stopped is not True or backup_verified is not True):
        raise EnrollmentError()
    try:
        if plan_digest(plan) != expected_digest:
            raise EnrollmentError()
        database_path = plan['database']
    except Exception:
        raise EnrollmentError() from None
    with _guard(database_path) as cold:
        if _inspect(cold) != plan:
            raise EnrollmentError()
        import fcntl
        lock_fd = os.open(plan['lock_name'], os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=cold.parent_fd)
        db = None
        try:
            lock = os.fstat(lock_fd)
            if (not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.getuid()
                    or stat.S_IMODE(lock.st_mode) != 0o600 or lock.st_nlink != 1
                    or lock.st_dev != plan['file']['device']):
                raise EnrollmentError()
            lock_identity = _stat(lock)
            _local_filesystem(lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if cold.check(lock_expected=lock_identity) != plan['file']:
                raise EnrollmentError()
            # Make the new lock inode/name durable before any enrollment row
            # can commit. Interrupted enrollment still requires inspection.
            os.fsync(lock_fd)
            os.fsync(cold.parent_fd)
            db = sqlite3.connect(cold.path.as_uri() + '?mode=rw', uri=True, timeout=0)
            db.execute('PRAGMA temp_store=MEMORY')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            if _snapshot(db) != plan['snapshot']:
                raise EnrollmentError()
            database_id, epoch = str(uuid.uuid4()), str(uuid.uuid4())
            db.execute(_SCHEMA)
            db.execute(_EPOCH_SCHEMA)
            row = (1, 1, database_id, plan['file']['device'], plan['file']['inode'],
                   lock.st_dev, lock.st_ino, epoch, None)
            db.execute('INSERT INTO hub_pilot_owner VALUES(?,?,?,?,?,?,?,?,?)', row)
            db.execute('INSERT INTO hub_pilot_owner_epochs VALUES(?,?)', (epoch, None))
            if _snapshot(db, enrolled=True) != plan['snapshot']:
                raise EnrollmentError()
            # WAL/journal creation by this transaction is expected here. Check
            # anchored path/FD identities directly without rejecting our journal.
            value = os.stat(cold.path.name, dir_fd=cold.parent_fd, follow_symlinks=False)
            cold.check_parent()
            if (any(_stat(value)[key] != plan['file'][key]
                    for key in ('device', 'inode', 'uid', 'gid', 'mode', 'nlink'))
                    or _stat(os.fstat(lock_fd)) != lock_identity
                    or _stat(os.stat(plan['lock_name'], dir_fd=cold.parent_fd, follow_symlinks=False)) != lock_identity
                    or _stat(os.stat(cold.path.parent, follow_symlinks=False), parent=True) != plan['parent']
                    or _stat(os.fstat(cold.parent_fd), parent=True) != plan['parent']
                    or cold.path.parent.resolve(strict=True) != cold.path.parent):
                raise EnrollmentError()
            db.commit()
            os.fsync(lock_fd)
            os.fsync(cold.parent_fd)
            return dict(schema_version=1, status='enrolled', plan_digest=expected_digest,
                        database_id=database_id, epoch=epoch, database_device=value.st_dev,
                        database_inode=value.st_ino, lock_device=lock.st_dev, lock_inode=lock.st_ino,
                        prior_snapshot=plan['snapshot'], runtime_activated=False)
        finally:
            try:
                if db is not None:
                    db.close()
            finally:
                os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--database', required=True)
    apply = sub.add_parser('apply')
    apply.add_argument('--plan', required=True)
    apply.add_argument('--expected-digest', required=True)
    apply.add_argument('--writers-stopped', action='store_true')
    apply.add_argument('--backup-verified', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'inspect':
            plan = inspect_plan(args.database)
            result = {'plan': plan, 'digest': plan_digest(plan)}
        else:
            # Read a bounded operator-supplied plan; no secrets belong in it.
            with Path(args.plan).open('rb') as source:
                raw = source.read(65537)
            if len(raw) > 65536:
                raise EnrollmentError()
            def pairs(values):
                result = {}
                for key, value in values:
                    if key in result:
                        raise EnrollmentError()
                    result[key] = value
                return result

            document = json.loads(raw, object_pairs_hook=pairs)
            if canonical_json(document).encode() != raw.strip():
                raise EnrollmentError()
            if type(document) is not dict or set(document) != {'plan', 'digest'} or document['digest'] != args.expected_digest:
                raise EnrollmentError()
            result = apply_plan(document['plan'], args.expected_digest,
                                writers_stopped=args.writers_stopped, backup_verified=args.backup_verified)
        print(canonical_json(result))
        return 0
    except Exception:
        print(str(EnrollmentError()), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
