"""Actual Linux kernel ownership tests; no workflow or Docker activation."""
import os
import gc
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import uuid
from types import SimpleNamespace
from contextlib import ExitStack, closing

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux kernel ownership prerequisite')

from engineering_hub.pilot_ownership import PilotOwner as ProductionPilotOwner, PilotOwnershipError


def PilotOwner(*args, **kwargs):
    # Explicitly disposable Linux validation storage, never deployment default.
    kwargs.setdefault('allow_ephemeral_test_storage', True)
    return ProductionPilotOwner(*args, **kwargs)


@pytest.fixture
def database(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    return root / 'hub.sqlite'


def worker(database, script=None):
    source = script or '''
import sys
from engineering_hub.pilot_ownership import PilotOwner
with PilotOwner(sys.argv[1], allow_ephemeral_test_storage=True) as owner:
    print(owner.epoch, flush=True)
    sys.stdin.read()
'''
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    return subprocess.Popen([sys.executable, '-c', source, str(database)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, close_fds=True)


def close_worker_pipes(child):
    # Popen.wait() reaps the process but does not close its PIPE file objects.
    # ExitStack attempts every close even if a different stream close fails.
    with ExitStack() as stack:
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stack.callback(stream.close)


def test_enrollment_and_new_epoch(database):
    with PilotOwner(database) as first:
        first.assert_active(database)
        epoch = first.epoch
        assert str(uuid.UUID(epoch)) == epoch
        assert first.previous_epoch is None
        with pytest.raises(PilotOwnershipError):
            PilotOwner(database)
        first.assert_active(database)
    with pytest.raises(PilotOwnershipError):
        first.assert_active(database)
    with PilotOwner(database) as second:
        assert second.previous_epoch == epoch
        assert second.epoch != epoch
        second.assert_active(database)


def test_other_process_and_sigstop_cannot_be_adopted(database):
    child = worker(database)
    try:
        epoch = child.stdout.readline().strip()
        assert str(uuid.UUID(epoch)) == epoch
        os.kill(child.pid, signal.SIGSTOP)
        for _ in range(2):
            with pytest.raises(PilotOwnershipError):
                PilotOwner(database)
        os.kill(child.pid, signal.SIGCONT)
        child.stdin.close()
        assert child.wait(timeout=10) == 0
        with PilotOwner(database) as successor:
            assert successor.previous_epoch == epoch
    finally:
        try:
            if child.poll() is None:
                os.kill(child.pid, signal.SIGCONT)
                child.kill()
            child.wait(timeout=10)
        finally:
            close_worker_pipes(child)


def test_sigkill_releases_even_with_raw_fork_child(database):
    child = worker(database, '''
import os, signal, sys
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
with PilotOwner(sys.argv[1], allow_ephemeral_test_storage=True) as owner:
    pid = os.fork()
    # Both processes share stdout: write each short record atomically so the
    # reader cannot receive another process's text before this record's newline.
    if pid == 0:
        try:
            owner.assert_active(sys.argv[1])
        except PilotOwnershipError:
            os.write(sys.stdout.fileno(), b'fork-denied\\n')
        else:
            os._exit(3)
        signal.pause()
        os._exit(0)
    os.write(sys.stdout.fileno(), (str(pid) + ':' + owner.epoch + '\\n').encode('ascii'))
    sys.stdin.read()
''')
    fork_pid = None
    try:
        lines = [child.stdout.readline().strip(), child.stdout.readline().strip()]
        assert 'fork-denied' in lines
        identity = next(line for line in lines if ':' in line)
        pid, epoch = identity.split(':')
        fork_pid = int(pid)
        # Child close must not unlock the still-live parent's description.
        with pytest.raises(PilotOwnershipError):
            PilotOwner(database)
        child.kill()
        child.wait(timeout=10)
        # Raw fork child remains alive, but its inherited FDs were closed.
        os.kill(fork_pid, 0)
        with PilotOwner(database) as successor:
            assert successor.previous_epoch == epoch
    finally:
        try:
            try:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
            finally:
                if fork_pid:
                    os.kill(fork_pid, signal.SIGKILL)
        finally:
            close_worker_pipes(child)


@pytest.mark.parametrize('kind', ['database_symlink', 'lock_symlink', 'database_hardlink', 'lock_hardlink', 'legacy', 'parent_mode', 'database_mode', 'lock_mode'])
def test_unsafe_existing_state_refused(database, kind):
    lock = database.with_name(database.name + '.pilot-owner.lock')
    other = database.parent / 'other'
    if kind.endswith('symlink'):
        other.write_bytes(b'')
        other.chmod(0o600)
        (database if kind.startswith('database') else lock).symlink_to(other)
    elif kind.endswith('hardlink'):
        other.write_bytes(b'')
        other.chmod(0o600)
        os.link(other, database if kind.startswith('database') else lock)
    elif kind == 'legacy':
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE unrelated(id INTEGER)')
        database.chmod(0o600)
    elif kind == 'parent_mode':
        database.parent.chmod(0o755)
    else:
        file = database if kind == 'database_mode' else lock
        file.write_bytes(b'')
        file.chmod(0o644)
    with pytest.raises(PilotOwnershipError):
        PilotOwner(database)
    assert database.parent.exists()


@pytest.mark.parametrize('change', ['lock_replace', 'database_replace', 'epoch', 'foreign_path', 'parent_mode'])
def test_live_capability_rechecks_binding(database, change):
    with PilotOwner(database) as owner:
        if change.endswith('replace'):
            file = database if change == 'database_replace' else database.with_name(database.name + '.pilot-owner.lock')
            file.rename(file.with_suffix('.saved'))
            file.write_bytes(b'')
            file.chmod(0o600)
        elif change == 'epoch':
            with sqlite3.connect(database) as db:
                db.execute('UPDATE hub_pilot_owner SET epoch=?', (str(uuid.uuid4()),))
        elif change == 'parent_mode':
            database.parent.chmod(0o755)
        with pytest.raises(PilotOwnershipError):
            owner.assert_active(database.with_name('foreign.sqlite') if change == 'foreign_path' else database)


def test_copied_enrollment_not_adopted(database):
    with PilotOwner(database):
        pass
    foreign = database.with_name('copy.sqlite')
    foreign.write_bytes(database.read_bytes())
    foreign.chmod(0o600)
    with pytest.raises(PilotOwnershipError):
        PilotOwner(foreign)


def test_unknown_filesystem_refused(database, monkeypatch):
    import engineering_hub.pilot_ownership as module
    def deny(fd, **kwargs):
        raise PilotOwnershipError()
    monkeypatch.setattr(module, '_local_filesystem', deny)
    with pytest.raises(PilotOwnershipError):
        PilotOwner(database)
    assert not database.exists()


@pytest.mark.parametrize('magic,accepted', [(0xEF53, True), (0x58465342, True), (0x9123683E, True),
                                           (0x2FC12FC1, True), (0x01021994, False), (0x794C7630, False),
                                           (0x6969, False), (0xFF534D42, False), (0x65735546, False), (123, False)])
def test_filesystem_allowlist(magic, accepted, monkeypatch):
    import engineering_hub.pilot_ownership as module
    def result(fd, buffer):
        module.ctypes.cast(buffer, module.ctypes.POINTER(module.ctypes.c_long))[0] = magic
        return 0
    monkeypatch.setattr(module.ctypes, 'CDLL', lambda *a, **kw: SimpleNamespace(fstatfs=result))
    if accepted:
        module._local_filesystem(1)
    else:
        with pytest.raises(PilotOwnershipError):
            module._local_filesystem(1)


@pytest.mark.parametrize('magic', [0x01021994, 0x794C7630, 0x6969, 0xFF534D42])
def test_ephemeral_storage_requires_explicit_test_opt_in(database, magic, monkeypatch):
    import engineering_hub.pilot_ownership as module
    def result(fd, buffer):
        module.ctypes.cast(buffer, module.ctypes.POINTER(module.ctypes.c_long))[0] = magic
        return 0
    monkeypatch.setattr(module.ctypes, 'CDLL', lambda *a, **kw: SimpleNamespace(fstatfs=result))
    with pytest.raises(PilotOwnershipError):
        ProductionPilotOwner(database)
    assert not database.exists()
    if magic in {0x01021994, 0x794C7630}:
        module._local_filesystem(1, allow_ephemeral_test_storage=True)
    else:
        with pytest.raises(PilotOwnershipError):
            module._local_filesystem(1, allow_ephemeral_test_storage=True)


@pytest.mark.parametrize('value', [1, 'true', None])
def test_ephemeral_flag_not_coerced(database, value):
    with pytest.raises(PilotOwnershipError):
        ProductionPilotOwner(database, allow_ephemeral_test_storage=value)
    assert not database.exists()


def test_foreign_owner_and_no_permission_repair(database, monkeypatch):
    import engineering_hub.pilot_ownership as module
    uid = os.getuid()
    monkeypatch.setattr(module.os, 'getuid', lambda: uid + 1)
    with pytest.raises(PilotOwnershipError):
        PilotOwner(database)
    assert not database.exists()
    assert database.parent.stat().st_uid == uid


def test_cloexec_and_explicit_child_close_fds(database):
    import fcntl
    with PilotOwner(database) as owner:
        for fd in (owner._parent_fd, owner._lock_fd):
            assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        child = subprocess.run([sys.executable, '-c', 'print("child-ok")'], close_fds=True,
                               capture_output=True, text=True, timeout=10)
        assert child.returncode == 0 and child.stdout.strip() == 'child-ok'
        owner.assert_active(database)


def test_second_owner_does_not_mutate_epoch(database):
    with PilotOwner(database) as first:
        with sqlite3.connect(database) as db:
            original = db.execute('SELECT * FROM hub_pilot_owner').fetchall()
        with pytest.raises(PilotOwnershipError):
            PilotOwner(database)
        with sqlite3.connect(database) as db:
            assert db.execute('SELECT * FROM hub_pilot_owner').fetchall() == original
        first.assert_active(database)


@pytest.mark.parametrize('original_active', [False, True])
def test_missing_enrolled_lock_is_never_recreated_or_epoch_updated(database, monkeypatch, original_active):
    """Refuse missing locks before O_CREAT, independent of filesystem inode reuse."""
    import engineering_hub.pilot_ownership as module

    owner = PilotOwner(database)
    if not original_active:
        owner.close()
    lock = database.with_name(database.name + '.pilot-owner.lock')

    def rows():
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            return (db.execute('SELECT * FROM hub_pilot_owner').fetchall(),
                    db.execute('SELECT * FROM hub_pilot_owner_epochs ORDER BY epoch').fetchall())

    original_rows = rows()
    original_bytes = database.read_bytes()
    lock.unlink()
    real_open = os.open
    create_attempts = []

    def guarded_open(path, flags, *args, **kwargs):
        if os.fspath(path) in {lock.name, str(lock)} and flags & os.O_CREAT:
            create_attempts.append((os.fspath(path), flags))
            # Constructor sanitizes underlying errors, so the explicit assertion
            # below is essential: merely expecting PilotOwnershipError would
            # wrongly pass even if this spy caused the failure itself.
            raise OSError('Regression: attempted to recreate enrolled lock')
        return real_open(path, flags, *args, **kwargs)

    try:
        with monkeypatch.context() as patched:
            patched.setattr(module.os, 'open', guarded_open)
            with pytest.raises(PilotOwnershipError):
                PilotOwner(database)
        assert create_attempts == [], 'Existing enrollment must never attempt O_CREAT for its missing lock'
        assert not lock.exists() and not lock.is_symlink()
        assert database.read_bytes() == original_bytes
        assert rows() == original_rows
        if original_active:
            with pytest.raises(PilotOwnershipError):
                owner.assert_active(database)
    finally:
        owner.close()


@pytest.mark.parametrize('race', ['database_appears', 'exclusive_lock_collision'])
def test_fresh_lock_races_never_fall_back_to_enrollment(database, monkeypatch, race):
    """Inject a competing path after absence checks, before exclusive lock creation."""
    import engineering_hub.pilot_ownership as module

    lock = database.with_name(database.name + '.pilot-owner.lock')
    real_open = os.open
    lock_attempts, sql_attempts, raced_identity = [], [], []

    def guarded_open(path, flags, *args, **kwargs):
        if os.fspath(path) in {lock.name, str(lock)}:
            lock_attempts.append(flags)
            if flags & os.O_CREAT:
                assert flags & os.O_EXCL, 'Fresh ownership creation must be exclusive'
                target = database.name if race == 'database_appears' else lock.name
                fd = real_open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                               0o600, dir_fd=kwargs['dir_fd'])
                try:
                    info = os.fstat(fd)
                    raced_identity.append((info.st_dev, info.st_ino))
                finally:
                    os.close(fd)
        return real_open(path, flags, *args, **kwargs)

    def forbidden_connect(self):
        sql_attempts.append(True)
        raise AssertionError('An ownership admission race must fail before SQLite enrollment')

    with monkeypatch.context() as patched:
        patched.setattr(module.os, 'open', guarded_open)
        patched.setattr(ProductionPilotOwner, '_connect', forbidden_connect)
        with pytest.raises(PilotOwnershipError):
            PilotOwner(database)
    assert len(raced_identity) == 1
    assert len(lock_attempts) == 2
    assert not lock_attempts[0] & os.O_CREAT
    assert lock_attempts[1] & os.O_CREAT and lock_attempts[1] & os.O_EXCL
    assert sql_attempts == [], 'A wrapped spy failure must not be mistaken for admission denial'
    target = database if race == 'database_appears' else lock
    info = target.stat()
    assert (info.st_dev, info.st_ino) == raced_identity[0]
    assert info.st_mode & 0o777 == 0o600
    assert target.read_bytes() == b''
    if race == 'exclusive_lock_collision':
        assert not database.exists()
    else:
        # Preserve the concurrently appearing database and our newly created
        # lock for inspection; do not enroll or silently remove either one.
        assert lock.exists()


@pytest.mark.parametrize('replacement', ['rename', 'unlink'])
def test_replaced_lock_never_allows_existing_database_adoption(database, replacement):
    owner = PilotOwner(database)
    lock = database.with_name(database.name + '.pilot-owner.lock')
    with sqlite3.connect(database) as db:
        original = db.execute('SELECT * FROM hub_pilot_owner').fetchall()
    try:
        if replacement == 'rename':
            lock.rename(lock.with_name('old-lock'))
        else:
            lock.unlink()
        # A missing enrolled lock must not be recreated, even while its old
        # inode is still held open; durable epoch state must stay untouched.
        with pytest.raises(PilotOwnershipError):
            PilotOwner(database)
        with sqlite3.connect(database) as db:
            assert db.execute('SELECT * FROM hub_pilot_owner').fetchall() == original
        assert not lock.exists() and not lock.is_symlink()
        with pytest.raises(PilotOwnershipError):
            owner.assert_active(database)
    finally:
        owner.close()
    # Closing the original owner does not authorize inode re-enrollment either.
    with pytest.raises(PilotOwnershipError):
        PilotOwner(database)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT * FROM hub_pilot_owner').fetchall() == original
    assert not lock.exists() and not lock.is_symlink()


def test_garbage_collection_closes_orphaned_capability(database):
    owner = PilotOwner(database)
    epoch = owner.epoch
    del owner
    gc.collect()
    with PilotOwner(database) as successor:
        assert successor.previous_epoch == epoch


def test_close_attempts_all_descriptors_when_one_close_fails(database, monkeypatch):
    import engineering_hub.pilot_ownership as module
    owner = PilotOwner(database)
    descriptors = [owner._lock_fd, owner._parent_fd]
    real_close = os.close
    attempted = []
    def close(fd):
        attempted.append(fd)
        real_close(fd)
        if fd == descriptors[0]:
            raise OSError('simulated close error')
    with monkeypatch.context() as patched:
        patched.setattr(module.os, 'close', close)
        with pytest.raises(PilotOwnershipError, match='^pilot ownership unavailable$'):
            owner.close()
    assert attempted == descriptors
    owner.close()
    with PilotOwner(database) as successor:
        assert successor.previous_epoch == owner.epoch


def test_closing_owner_preserves_other_sqlite_connection_write_lock(database):
    owner = PilotOwner(database)
    db = sqlite3.connect(database, timeout=0)
    try:
        db.execute('CREATE TABLE lock_probe(value INTEGER)')
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO lock_probe VALUES(1)')
        owner.assert_active(database)
        owner.close()
        # A different process must still be excluded by SQLite, even though
        # the ownership flock is now released. A raw DB fd close breaks this.
        result = subprocess.run([sys.executable, '-c', '''
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], timeout=0)
try:
    db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
    assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
    print('still-locked')
else:
    raise SystemExit('SQLite transaction lock was lost')
finally:
    db.close()
''', str(database)], capture_output=True, text=True, timeout=10, close_fds=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'still-locked'
        db.commit()
        assert db.execute('SELECT value FROM lock_probe').fetchall() == [(1,)]
    finally:
        db.close()
        owner.close()
