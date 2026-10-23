"""Read-only classification/parity of explicitly selected, already cold ledgers.

No writer exclusion, ACL acceptance, backup, copying, enrollment or startup is
performed. Shared source modes are observed, never repaired or treated as safe.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

from . import pilot_enrollment as enrollment
from .pilot_ownership import _local_filesystem


class PreparationError(RuntimeError):
    def __init__(self):
        super().__init__('offline ledger preparation unavailable; preserve artifacts for inspection')


class _Source:
    def __init__(self, path):
        self.stack = ExitStack()
        try:
            if sys.platform != 'linux' or not isinstance(path, (str, Path)):
                raise PreparationError()
            self.path = Path(path)
            if not self.path.is_absolute() or str(self.path) != os.path.abspath(self.path):
                raise PreparationError()
            self.parents = []
            current = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self.stack.callback(os.close, current)
            self.parents.append((Path('/'), current, enrollment._stat(os.fstat(current), parent=True)))
            named = Path('/')
            for part in self.path.parent.parts[1:]:
                current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
                self.stack.callback(os.close, current)
                named = named / part
                self.parents.append((named, current, enrollment._stat(os.fstat(current), parent=True)))
            self.parent_fd = current
            _local_filesystem(current)
            before = self.check()
            self.fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            self.stack.callback(os.close, self.fd)
            _local_filesystem(self.fd)
            if enrollment._stat(os.fstat(self.fd)) != before:
                raise PreparationError()
            self.identity = before
        except BaseException:
            self.stack.close()
            raise

    def check(self):
        # Rewalk each named ancestor as well as retained descriptors. A shared
        # parent is not exclusive; any observed rename/rebinding refuses.
        for path, fd, expected in self.parents:
            value = os.stat(path, follow_symlinks=False)
            if (not stat.S_ISDIR(value.st_mode)
                    or enrollment._stat(value, parent=True) != expected
                    or enrollment._stat(os.fstat(fd), parent=True) != expected):
                raise PreparationError()
        value = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                or value.st_dev != os.fstat(self.parent_fd).st_dev
                or not 0 < value.st_size <= enrollment._MAX_DB_BYTES):
            raise PreparationError()
        for suffix in ('-wal', '-shm', '-journal', '.pilot-owner.lock'):
            try:
                os.stat(self.path.name + suffix, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise PreparationError()
        result = enrollment._stat(value)
        if hasattr(self, 'identity') and (result != self.identity
                or enrollment._stat(os.fstat(self.fd)) != self.identity):
            raise PreparationError()
        return result

    def fingerprint(self):
        self.check()
        os.lseek(self.fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        count = 0
        while chunk := os.read(self.fd, 1024 * 1024):
            count += len(chunk)
            if count > enrollment._MAX_DB_BYTES:
                raise PreparationError()
            digest.update(chunk)
        if count != self.identity['size']:
            raise PreparationError()
        self.check()
        return digest.hexdigest()

    def snapshot(self):
        self.check()
        # SQLite canonicalizes /proc/self/fd paths and can reopen the name.
        # Deserialize only bounded bytes read from the retained no-follow FD;
        # SQLite never receives a source pathname or opens a filesystem ledger.
        os.lseek(self.fd, 0, os.SEEK_SET)
        raw = bytearray()
        while chunk := os.read(self.fd, min(1024 * 1024, enrollment._MAX_DB_BYTES + 1 - len(raw))):
            raw.extend(chunk)
            if len(raw) > enrollment._MAX_DB_BYTES:
                raise PreparationError()
        if len(raw) != self.identity['size']:
            raise PreparationError()
        captured_hash = hashlib.sha256(raw).hexdigest()
        self.check()
        if len(raw) < 100 or raw[:16] != b'SQLite format 3\x00' or raw[18:20] not in (b'\x01\x01', b'\x02\x02'):
            raise PreparationError()
        wal_view = raw[18:20] == b'\x02\x02'
        if wal_view:
            # SQLite's documented deserialize workaround, only in the memory
            # view AFTER hashing original bytes. Not a checkpoint or recovery:
            # absence of omitted WAL still requires external cold-state proof.
            raw[18:20] = b'\x01\x01'
        db = sqlite3.connect(':memory:', timeout=0)
        try:
            db.deserialize(raw)
            del raw
            db.execute('PRAGMA temp_store=MEMORY')
            db.execute('PRAGMA query_only=ON')
            result = enrollment._snapshot(db)
        finally:
            db.close()
        self.check()
        return result, captured_hash, wal_view


def inspect_preparation(source, *, destination=None):
    """Return metadata/fingerprints only; never proves stopped writers or ACLs.

    Optional destination is an ALREADY COPIED, private, unenrolled ledger and
    must pass unchanged enrollment.inspect_plan metadata/schema checks.
    """
    selected = None
    try:
        selected = _Source(source)
        before = selected.fingerprint()
        snapshot, captured_hash, wal_view = selected.snapshot()
        if captured_hash != before:
            raise PreparationError()
        target = None
        if destination is not None:
            target = enrollment.inspect_plan(destination)
            if ((target['file']['device'], target['file']['inode']) ==
                    (selected.identity['device'], selected.identity['inode'])
                    or target['content_sha256'] != before or target['snapshot'] != snapshot):
                raise PreparationError()
        if selected.fingerprint() != before:
            raise PreparationError()
        if target is not None and enrollment.inspect_plan(destination) != target:
            raise PreparationError()
        result = dict(schema='engineering-hub.offline-ledger-preparation.v1',
            source=dict(path=str(selected.path), file=selected.identity,
                        ancestors=[dict(path=str(path), metadata=value) for path, _, value in selected.parents],
                        content_sha256=before, snapshot=snapshot,
                        memory_view_wal_header_normalized=wal_view),
            destination=target, byte_and_logical_parity=target is not None,
            writers_stopped_verified=False, acl_verified=False, backup_verified=False,
            source_modified=False, copied=False, enrolled=False, runtime_activated=False)
        return dict(report=result, digest=enrollment.plan_digest(result))
    except Exception:
        raise PreparationError() from None
    finally:
        if selected is not None:
            selected.stack.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', help='optional already-copied private unenrolled ledger')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(inspect_preparation(args.source, destination=args.destination), sort_keys=True))
        return 0
    except PreparationError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
