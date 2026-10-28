"""Create one new isolated staging state tree; never adopt existing state.

This source-only helper does not start services or create configuration. Its
parent directory must already be a trusted, private, local Linux directory.
Failures deliberately leave all artifacts in place for operator inspection.
"""
import argparse
from pathlib import Path
import os
import stat
import sys

from agent_message_bus.database import MessageRepository
from agent_message_bus.files import FileRepository
from .pilot_ownership import PilotOwner, _local_filesystem
from .private_browser_socket import _check_parent, _private_parent
from .repository import HubRepository


class StagingStateInitError(RuntimeError):
    def __init__(self):
        super().__init__('staging state initialization unavailable')


def _directory(fd, path, expected=None):
    opened = os.fstat(fd)
    named = os.stat(path, follow_symlinks=False)
    identity = (opened.st_dev, opened.st_ino)
    if (not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(named.st_mode)
            or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o700
            or identity != (named.st_dev, named.st_ino)
            or (expected is not None and identity != expected)
            or Path(path).resolve(strict=True) != Path(path)):
        raise StagingStateInitError()
    return identity


def _new_file(directory_fd, name):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise StagingStateInitError()
    finally:
        os.close(fd)


def initialize_staging_state(root):
    """Initialize a new root or raise a sanitized error, preserving partial state.

    Root must be an explicit canonical absolute Linux path. Its existing parent
    must be owned by this user, mode 0700, on a supported local filesystem, and
    trusted against concurrent modification by the same user.
    """
    parent_fd = root_fd = bus_fd = None
    try:
        value = os.fspath(root)
        if (sys.platform != 'linux' or not isinstance(value, str) or not value.startswith('/')
                or value.startswith('//') or os.path.normpath(value) != value or value == '/'):
            raise StagingStateInitError()
        path = Path(value)
        parent_fd, parent_identity = _private_parent(path)
        _directory(parent_fd, path.parent, parent_identity)
        _local_filesystem(parent_fd)
        _check_parent(path, parent_fd, parent_identity)
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        root_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=parent_fd)
        os.fchmod(root_fd, 0o700)
        root_identity = _directory(root_fd, path)
        if os.fstat(root_fd).st_dev != os.fstat(parent_fd).st_dev:
            raise StagingStateInitError()
        _local_filesystem(root_fd)

        def check_root():
            _check_parent(path, parent_fd, parent_identity)
            fresh_fd, fresh_identity = _private_parent(path)
            try:
                if fresh_identity != parent_identity:
                    raise StagingStateInitError()
            finally:
                os.close(fresh_fd)
            _directory(parent_fd, path.parent, parent_identity)
            _directory(root_fd, path, root_identity)

        check_root()
        with PilotOwner(path / 'hub.sqlite3', require_existing=False) as owner:
            repo = HubRepository(path / 'hub.sqlite3', pilot_owner=owner)
            try:
                check_root()
                os.mkdir('agent-message-bus', 0o700, dir_fd=root_fd)
                bus_fd = os.open('agent-message-bus', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=root_fd)
                os.fchmod(bus_fd, 0o700)
                bus_path = path / 'agent-message-bus'
                bus_identity = _directory(bus_fd, bus_path)
                if os.fstat(bus_fd).st_dev != os.fstat(root_fd).st_dev:
                    raise StagingStateInitError()
                _local_filesystem(bus_fd)
                _new_file(bus_fd, 'messages.sqlite3')
                _new_file(bus_fd, 'audit.jsonl')
                os.mkdir('files', 0o700, dir_fd=bus_fd)
                files_fd = os.open('files', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                   dir_fd=bus_fd)
                try:
                    os.fchmod(files_fd, 0o700)
                    _directory(files_fd, bus_path / 'files')
                finally:
                    os.close(files_fd)
                check_root()
                _directory(bus_fd, bus_path, bus_identity)
                MessageRepository(bus_path / 'messages.sqlite3')
                FileRepository(bus_path / 'messages.sqlite3', bus_path / 'files')
                check_root()
                _directory(bus_fd, bus_path, bus_identity)
                owner.assert_active(path / 'hub.sqlite3')
            finally:
                repo.close()
    except Exception:
        raise StagingStateInitError() from None
    finally:
        for fd in (bus_fd, root_fd, parent_fd):
            if fd is not None:
                os.close(fd)


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            # Never echo rejected arguments: they may contain sensitive data.
            self.exit(2, str(StagingStateInitError()) + '\n')

    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--new-state-dir', required=True, metavar='ABSOLUTE_NEW_ROOT')
    args = parser.parse_args(argv)
    try:
        initialize_staging_state(args.new_state_dir)
    except (Exception, KeyboardInterrupt):
        parser.exit(2, str(StagingStateInitError()) + '\n')
    print('{"status":"initialized"}')


if __name__ == '__main__':
    main()
