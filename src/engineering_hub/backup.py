"""Explicit coherent SQLite backup, including committed WAL transactions.

Backups contain sensitive Hub state, including session data. POSIX output is
created owner-only (0600). Windows inherits the destination directory's ACL;
the operator must choose an appropriately restricted directory. No environment
files or separate credentials are collected. This module starts no services.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


def backup_hub(source, destination, *, timeout_seconds=30):
    """Create a new, verified backup; never overwrite an existing destination."""
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve(strict=False)
    if not source.is_file():
        raise ValueError("Source must be an existing database file")
    if source == destination:
        raise ValueError("Source and destination must differ")
    if not destination.parent.is_dir():
        raise ValueError("Destination directory must already exist")
    if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 300:
        raise ValueError("Backup deadline must be positive and at most 300 seconds")
    # Exclusive creation rejects existing files, symlinks and concurrent creators.
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    created_identity = os.fstat(descriptor)
    os.close(descriptor)
    started = time.monotonic()
    reader = writer = None
    try:
        reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)
        writer = sqlite3.connect(destination, timeout=1)

        def progress(status, remaining, total):
            if time.monotonic() - started > timeout_seconds:
                raise TimeoutError("Backup deadline exceeded")

        reader.backup(writer, pages=128, progress=progress, sleep=0.05)
        check = writer.execute("PRAGMA quick_check").fetchall()
        if check != [("ok",)]:
            raise ValueError("Backup integrity verification failed")
        writer.close()
        writer = None
        reader.close()
        reader = None
        digest = hashlib.sha256()
        with destination.open("rb") as completed:
            for chunk in iter(lambda: completed.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"sha256": digest.hexdigest(), "size_bytes": destination.stat().st_size,
                "quick_check": "ok", "permissions": "0600" if os.name != "nt" else "inherited_directory_acl"}
    except BaseException:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
        # Never remove a pre-existing/replaced file or any other path.
        try:
            current = destination.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (created_identity.st_dev, created_identity.st_ino):
                destination.unlink()
        except FileNotFoundError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a new coherent Engineering Hub SQLite backup")
    parser.add_argument("source", help="Existing Hub database file")
    parser.add_argument("destination", help="New backup file inside a restricted directory")
    args = parser.parse_args(argv)
    try:
        report = backup_hub(args.source, args.destination)
    except (OSError, ValueError, sqlite3.Error, TimeoutError):
        # Do not reflect source paths, SQLite errors or sensitive query content.
        parser.exit(1, "Backup failed; verify paths, permissions and database integrity.\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
