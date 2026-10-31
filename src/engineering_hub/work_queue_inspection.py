"""Non-owning queue reservation inspection on an OS-enforced read-only view.

This deliberately refuses ordinary writable mounts, even with mode0600 files.
It never constructs a Repository/WorkQueue or acquires a pilot owner. A result
is one SQLite snapshot of this queue, not host availability or reconciliation.
The caller must independently select/authorize the exact database and machine.
"""
from contextlib import contextmanager
import argparse
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time


class QueueInspectionUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('queue inspection unavailable')


def _identity(value):
    return value.st_dev, value.st_ino


def _readonly(fd):
    if not os.fstatvfs(fd).f_flag & getattr(os, 'ST_RDONLY', 1):
        raise QueueInspectionUnavailable()


@contextmanager
def _pinned_view(path):
    """Pin trusted no-follow ancestors and files; never create WAL sidecars."""
    fds = []
    try:
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fds.append(directory)
        for part in path.parent.parts[1:]:
            info = os.fstat(directory)
            if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o022:
                raise QueueInspectionUnavailable()
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=directory)
            fds.append(directory)
        info = os.fstat(directory)
        if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o022:
            raise QueueInspectionUnavailable()
        _readonly(directory)
        pins = {}
        for suffix, maximum, required in (('', 64 * 1024**2, True), ('-wal', 64 * 1024**2, False),
                                           ('-shm', 1024**2, False), ('-journal', 0, False)):
            try:
                fd = os.open(path.name + suffix, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             dir_fd=directory)
            except FileNotFoundError:
                if required:
                    raise
                pins[suffix] = None
                continue
            fds.append(fd)
            current = os.fstat(fd)
            if (suffix == '-journal' or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or current.st_uid not in (0, os.geteuid()) or stat.S_IMODE(current.st_mode) & 0o022
                    or not 0 <= current.st_size <= maximum):
                raise QueueInspectionUnavailable()
            _readonly(fd)
            pins[suffix] = (fd, _identity(current))
        header = os.pread(pins[''][0], 20, 0)
        if header[:16] != b'SQLite format 3\x00' or header[18:20] not in (b'\x01\x01', b'\x02\x02'):
            raise QueueInspectionUnavailable()
        if header[18:20] == b'\x02\x02' and (pins['-wal'] is None or pins['-shm'] is None):
            raise QueueInspectionUnavailable()

        def check():
            _readonly(directory)
            if _identity(os.stat(path.parent, follow_symlinks=False)) != _identity(os.fstat(directory)):
                raise QueueInspectionUnavailable()
            for suffix, pin in pins.items():
                try:
                    named = os.stat(path.name + suffix, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    if pin is None:
                        continue
                    raise
                if pin is None or _identity(named) != pin[1] or _identity(os.fstat(pin[0])) != pin[1]:
                    raise QueueInspectionUnavailable()
                _readonly(pin[0])
        check()
        yield check
        check()
    finally:
        failed = False
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                failed = True
        if failed:
            raise QueueInspectionUnavailable()


def _reservation(machine, jobs, now):
    if machine is None:
        return 'unknown'
    generation, job_id = machine
    if type(generation) is not int or generation < 0:
        return 'unknown'
    active = [row for row in jobs if row[2] in ('claimed', 'held')]
    if any(row[0] != job_id for row in active):
        return 'unknown'
    if job_id is None:
        return 'not_reserved'
    matching = [row for row in jobs if row[0] == job_id]
    if len(matching) != 1:
        return 'unknown'
    _, _, state, fence, lease = matching[0]
    if type(fence) is not int or fence <= 0 or fence != generation:
        return 'unknown'
    if state == 'held':
        return 'held'
    if state == 'claimed' and type(lease) in (int, float) and math.isfinite(lease):
        return 'held' if lease <= now else 'active'
    return 'unknown'


def inspect_work_queue(path, *, machine_id, timeout_seconds=1):
    """Return a bounded reservation snapshot, or a fixed unavailable error.

    Requires Linux and an already provisioned read-only filesystem view with
    readable existing WAL/SHM when WAL mode is in use. No mounts are made here.
    Missing machine rows are unknown: the action registry is not independently
    available to this reader. No eligibility/host-free conclusion is returned.
    """
    db = None
    try:
        if (sys.platform != 'linux' or sqlite3.sqlite_version_info < (3, 22, 0)
                or type(path) is not str or not path.startswith('/') or str(Path(path)) != path
                or path == '/' or path.startswith('//') or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or '..' in Path(path).parts
                or type(machine_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', machine_id)
                or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 2):
            raise QueueInspectionUnavailable()
        started = time.monotonic()
        deadline = started + timeout_seconds
        with _pinned_view(Path(path)) as check:
            db = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True,
                                 timeout=min(.25, timeout_seconds), isolation_level=None)
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.set_progress_handler(lambda: int(not started <= time.monotonic() < deadline), 100)
            db.execute('BEGIN')
            for table, columns in (('hub_work_machines', {'machine_id', 'generation', 'job_id'}),
                                   ('hub_work_jobs', {'id', 'machine_id', 'state', 'fence', 'lease_until'})):
                kind = db.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchone()
                if kind != ('table',):
                    raise QueueInspectionUnavailable()
                actual = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
                if not columns <= actual:
                    raise QueueInspectionUnavailable()
            machine_rows = db.execute('SELECT generation,job_id FROM hub_work_machines WHERE machine_id=? LIMIT 2',
                                      (machine_id,)).fetchall()
            if len(machine_rows) > 1:
                raise QueueInspectionUnavailable()
            machine = machine_rows[0] if machine_rows else None
            jobs = db.execute("SELECT id,machine_id,state,fence,lease_until FROM hub_work_jobs "
                              "WHERE machine_id=? AND (state IN ('claimed','held') OR id=?) LIMIT 101",
                              (machine_id, machine[1] if machine else None)).fetchall()
            if len(jobs) > 100:
                raise QueueInspectionUnavailable()
            wall = time.time()
            if not math.isfinite(wall) or wall < 0:
                raise QueueInspectionUnavailable()
            state = _reservation(machine, jobs, wall)
            db.execute('ROLLBACK')
            db.close()
            db = None
            check()
            if not started <= time.monotonic() < deadline:
                raise QueueInspectionUnavailable()
            return {'machine_id': machine_id, 'queue_reservation': state,
                    'requires_reconciliation': None if state == 'unknown' else state == 'held',
                    'queue_only': True, 'observed_at': wall}
    except Exception:
        raise QueueInspectionUnavailable() from None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                raise QueueInspectionUnavailable() from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Never echo an invalid argument: it could contain credential material.
        raise QueueInspectionUnavailable()


def main(argv=None):
    """Explicit one-shot CLI; provision a read-only view separately first.

    python -m engineering_hub.work_queue_inspection --database /explicit/queue.db
        --machine-id staging-pc --timeout-ms 1000

    This makes no mount, changes no owner, and never reports host availability.
    """
    try:
        parser = _Parser(description='Inspect one queue reservation on an existing read-only view',
                         allow_abbrev=False)
        parser.add_argument('--database', required=True, action='append')
        parser.add_argument('--machine-id', required=True, action='append')
        parser.add_argument('--timeout-ms', required=True, action='append')
        options = parser.parse_args(argv)
        if any(len(value) != 1 for value in (options.database, options.machine_id, options.timeout_ms)):
            raise QueueInspectionUnavailable()
        timeout = options.timeout_ms[0]
        if not re.fullmatch(r'[1-9][0-9]{2,3}', timeout) or not 100 <= int(timeout) <= 2000:
            raise QueueInspectionUnavailable()
        result = inspect_work_queue(options.database[0], machine_id=options.machine_id[0],
                                    timeout_seconds=int(timeout) / 1000)
        raw = json.dumps(dict(status='observed', **result), sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(raw.encode('utf-8')) > 1024:
            raise QueueInspectionUnavailable()
        print(raw)
        return 0
    except Exception:
        print('{"queue_only":true,"status":"unavailable"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
