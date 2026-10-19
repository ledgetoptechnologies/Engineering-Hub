"""Real Linux flock lifecycle tests; no daemon, service or database ownership."""
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from uuid import UUID

import pytest

pytest.importorskip('fcntl', reason='Real Linux ownership required')
pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux ownership required')

from hub_validation_worker import ownership as module
from hub_validation_worker.ownership import ValidationOwner, ValidationOwnershipError


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'private'
    path.mkdir(mode=0o700)
    return path


def owner(path):
    return ValidationOwner(path, allow_ephemeral_test_storage=True)


def test_constructor_inert_and_explicit_acquisition_fresh_instance(state, monkeypatch):
    actual = os.open
    def forbidden(*args, **kwargs):
        raise AssertionError('constructor must not open state')
    monkeypatch.setattr(module.os, 'open', forbidden)
    first = owner(state)
    assert list(state.iterdir()) == []
    monkeypatch.setattr(module.os, 'open', actual)
    with pytest.raises(ValidationOwnershipError):
        _ = first.instance_id
    first.acquire()
    assert first.directory == state
    generation = first.instance_id
    assert str(UUID(generation)) == generation
    lock = state / 'validation-owner.lock'
    inode = lock.stat().st_ino
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert lock.read_bytes() == module._MARKER
    first.close()
    first.close()
    assert lock.stat().st_ino == inode
    with pytest.raises(ValidationOwnershipError):
        first.acquire()
    second = owner(state).acquire()
    try:
        assert second.instance_id != generation
        assert lock.stat().st_ino == inode
    finally:
        second.close()


def test_second_owner_denied_nonblocking_until_explicit_close(state):
    first = owner(state).acquire()
    try:
        started = time.monotonic()
        with pytest.raises(ValidationOwnershipError):
            owner(state).acquire()
        assert time.monotonic() - started < 1
        first.assert_active()
    finally:
        first.close()
    second = owner(state).acquire()
    second.close()


@pytest.mark.parametrize('mode', [0o755, 0o750, 0o770])
def test_nonprivate_directory_is_not_fixed_or_created(state, mode):
    state.chmod(mode)
    with pytest.raises(ValidationOwnershipError):
        owner(state).acquire()
    assert stat.S_IMODE(state.stat().st_mode) == mode
    assert not (state / 'validation-owner.lock').exists()


def test_missing_or_symlink_directory_denied(state):
    missing = state / 'missing'
    with pytest.raises(ValidationOwnershipError):
        owner(missing).acquire()
    assert not missing.exists()
    alias = state.parent / 'alias'
    alias.symlink_to(state, target_is_directory=True)
    with pytest.raises(ValidationOwnershipError):
        owner(alias).acquire()
    assert list(state.iterdir()) == []


@pytest.mark.parametrize('kind', ['empty', 'foreign', 'extra', 'symlink', 'hardlink', 'fifo', 'mode', 'directory'])
def test_foreign_lock_never_adopted_truncated_or_repaired(state, kind):
    lock = state / 'validation-owner.lock'
    if kind == 'symlink':
        target = state / 'target'
        target.write_bytes(module._MARKER)
        target.chmod(0o600)
        lock.symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(lock, 0o600)
    elif kind == 'directory':
        lock.mkdir(mode=0o700)
    else:
        lock.write_bytes({'empty': b'', 'foreign': b'foreign db or lock',
                          'extra': module._MARKER + b'x'}.get(kind, module._MARKER))
        lock.chmod(0o644 if kind == 'mode' else 0o600)
        if kind == 'hardlink':
            os.link(lock, state / 'second-name')
    before = lock.lstat()
    content = lock.read_bytes() if stat.S_ISREG(before.st_mode) else None
    with pytest.raises(ValidationOwnershipError):
        owner(state).acquire()
    assert lock.lstat().st_ino == before.st_ino
    assert lock.lstat().st_mode == before.st_mode
    if content is not None:
        assert lock.read_bytes() == content


def test_live_path_replacement_denies_without_adopting_or_deleting(state):
    first = owner(state).acquire()
    lock = state / 'validation-owner.lock'
    original = state / 'original-lock'
    lock.rename(original)
    lock.write_bytes(module._MARKER)
    lock.chmod(0o600)
    replacement = lock.stat().st_ino
    try:
        with pytest.raises(ValidationOwnershipError):
            first.assert_active()
        assert first._lock_fd is not None  # Original ownership held until close.
        assert os.fstat(first._lock_fd).st_ino == original.stat().st_ino
    finally:
        first.close()
    assert lock.stat().st_ino == replacement and original.exists()


def test_parent_replacement_and_marker_tamper_deny(state):
    first = owner(state).acquire()
    lock = state / 'validation-owner.lock'
    lock.write_bytes(b'foreign')
    try:
        with pytest.raises(ValidationOwnershipError):
            first.assert_active()
        state.rename(state.with_name('moved-private'))
        state.mkdir(mode=0o700)
        with pytest.raises(ValidationOwnershipError):
            first.assert_active()
    finally:
        first.close()


def test_actual_crash_releases_kernel_lock_without_deleting_marker(state):
    code = '''
import os,sys
from hub_validation_worker.ownership import ValidationOwner
owner=ValidationOwner(sys.argv[1],allow_ephemeral_test_storage=True).acquire()
print(owner.instance_id,flush=True)
os._exit(23)
'''
    process = subprocess.run([sys.executable, '-c', code, str(state)], capture_output=True, text=True, timeout=5)
    assert process.returncode == 23
    previous = process.stdout.strip()
    next_owner = owner(state).acquire()
    try:
        assert next_owner.instance_id != previous
    finally:
        next_owner.close()


def test_real_fork_child_close_never_unlocks_parent_or_strands_fd(state):
    first = owner(state).acquire()
    # Test explicit child cleanup, not only the automatic hook.
    module._OWNERS.discard(first)
    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    held, release = threading.Event(), threading.Event()
    def mutex_owner():
        with first._mutex:
            held.set()
            release.wait(5)
    thread = threading.Thread(target=mutex_owner)
    thread.start()
    assert held.wait(2)
    child = os.fork()
    if child == 0:
        os.close(ready_r)
        os.close(release_w)
        try:
            first.close()
            first.close()
            try:
                first.assert_active()
            except ValidationOwnershipError:
                pass
            else:
                os._exit(4)
            os.write(ready_w, b'closed')
            os.read(release_r, 1)
            os._exit(0)
        except BaseException:
            os._exit(5)
    os.close(ready_w)
    os.close(release_r)
    try:
        assert select.select([ready_r], [], [], 3)[0], 'Child inherited a locked Python mutex'
        assert os.read(ready_r, 6) == b'closed'
        release.set()
        thread.join(3)
        first.assert_active()
        with pytest.raises(ValidationOwnershipError):
            owner(state).acquire()
        first.close()
        second = owner(state).acquire()
        try:
            assert os.waitpid(child, os.WNOHANG) == (0, 0)
        finally:
            second.close()
    finally:
        release.set()
        thread.join(3)
        first.close()
        os.close(ready_r)
        try:
            os.write(release_w, b'x')
        except BrokenPipeError:
            pass
        os.close(release_w)
        deadline = time.monotonic() + 3
        while True:
            reaped, status = os.waitpid(child, os.WNOHANG)
            if reaped:
                assert os.waitstatus_to_exitcode(status) == 0
                break
            if time.monotonic() >= deadline:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                pytest.fail('Child ownership test did not finish')
            time.sleep(0.01)


def test_lazy_fork_hook_drops_child_fds(state):
    first = owner(state).acquire()
    child = os.fork()
    if child == 0:
        os._exit(0 if first._closed and first._parent_fd is None and first._lock_fd is None else 7)
    try:
        deadline = time.monotonic() + 3
        while True:
            reaped, status = os.waitpid(child, os.WNOHANG)
            if reaped:
                assert os.waitstatus_to_exitcode(status) == 0
                break
            if time.monotonic() >= deadline:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                pytest.fail('Fork hook test timed out')
            time.sleep(0.01)
        first.assert_active()
    finally:
        first.close()
