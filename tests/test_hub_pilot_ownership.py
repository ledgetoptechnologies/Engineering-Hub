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
from contextlib import ExitStack

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux kernel ownership prerequisite')

from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError


@pytest.fixture
def database(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    return root / 'hub.sqlite'


def worker(database, script=None):
    source = script or '''
import sys
from engineering_hub.pilot_ownership import PilotOwner
with PilotOwner(sys.argv[1]) as owner:
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
with PilotOwner(sys.argv[1]) as owner:
    pid = os.fork()
    if pid == 0:
        try:
            owner.assert_active(sys.argv[1])
        except PilotOwnershipError:
            print('fork-denied', flush=True)
        else:
            os._exit(3)
        signal.pause()
        os._exit(0)
    print(str(pid) + ':' + owner.epoch, flush=True)
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
    def deny(fd):
        raise PilotOwnershipError()
    monkeypatch.setattr(module, '_local_filesystem', deny)
    with pytest.raises(PilotOwnershipError):
        PilotOwner(database)
    assert not database.exists()


@pytest.mark.parametrize('magic,accepted', [(0xEF53, True), (0x58465342, True), (0x9123683E, True),
                                           (0x2FC12FC1, True), (0x01021994, True), (0x794C7630, True),
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
        for fd in (owner._parent_fd, owner._lock_fd, owner._database_fd):
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
        # Constructor may create a replacement lock, but must not adopt state
        # protected by the older, still-held inode or mutate its durable epoch.
        with pytest.raises(PilotOwnershipError):
            PilotOwner(database)
        with sqlite3.connect(database) as db:
            assert db.execute('SELECT * FROM hub_pilot_owner').fetchall() == original
        with pytest.raises(PilotOwnershipError):
            owner.assert_active(database)
    finally:
        owner.close()
    # Closing the original owner does not authorize inode re-enrollment either.
    with pytest.raises(PilotOwnershipError):
        PilotOwner(database)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT * FROM hub_pilot_owner').fetchall() == original


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
    descriptors = [owner._database_fd, owner._lock_fd, owner._parent_fd]
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
