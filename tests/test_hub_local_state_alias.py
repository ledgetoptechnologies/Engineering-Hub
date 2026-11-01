import os
from pathlib import Path
import pytest

from scripts.hub_local_state_alias import StateAlias
from scripts.hub_local_test_volume import OwnedVolume

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux descriptors required')


def fixture_paths(tmp_path, monkeypatch):
    a = tmp_path / 'alias'; b = tmp_path / 'docker' / 'volumes' / 'test' / '_data'
    a.mkdir(mode=0o700); b.mkdir(mode=0o700, parents=True)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(StateAlias, '_check_tmpfs', staticmethod(lambda fd: None))
    monkeypatch.setattr(StateAlias, '_private_rw', staticmethod(lambda mount_id: None))
    ids = {(os.stat(p).st_dev, os.stat(p).st_ino) for p in (a, b)}
    original = StateAlias._identity
    monkeypatch.setattr(StateAlias, '_identity', staticmethod(lambda x: (7, 7) if original(x) in ids else original(x)))
    info = a.stat()
    original_fstat = os.fstat
    def owned(fd):
        result = original_fstat(fd)
        if (result.st_dev, result.st_ino) in ids:
            fields = list(result); fields[4] = fields[5] = 10001
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(os, 'fstat', owned)
    return a, b, OwnedVolume('test', 'task', 'daemon', str(b)), info


def test_retains_and_rechecks_named_descriptors(tmp_path, monkeypatch):
    a, b, volume, info = fixture_paths(tmp_path, monkeypatch)
    verifier = StateAlias(str(a), volume, uid=10001, gid=10001)
    try:
        assert verifier.check() == (7, 7)
        a.rename(tmp_path / 'old')
        a.mkdir(mode=0o700)
        with pytest.raises(ValueError, match='changed'):
            verifier.check()
    finally:
        verifier.close()
    with pytest.raises(ValueError, match='closed'):
        verifier.check()


def test_requires_same_directory(tmp_path, monkeypatch):
    a, b, volume, info = fixture_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(StateAlias, '_identity', staticmethod(lambda x: (x.st_dev, x.st_ino)))
    with pytest.raises(ValueError, match='same directory'):
        StateAlias(str(a), volume, uid=10001, gid=10001)


def test_rejects_overlap_symlink_and_wrong_owner(tmp_path, monkeypatch):
    a, b, volume, info = fixture_paths(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='outside Docker root'):
        StateAlias(str(b / 'child'), volume, uid=10001, gid=10001)
    with pytest.raises(ValueError, match='invalid ownership'):
        StateAlias(str(a), volume, uid=10002, gid=10001)
    a.rmdir(); a.symlink_to(b, target_is_directory=True)
    with pytest.raises(OSError):
        StateAlias(str(a), volume, uid=10001, gid=10001)


def test_rejects_noncanonical_or_untrusted(tmp_path, monkeypatch):
    a, b, volume, info = fixture_paths(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='canonical'):
        StateAlias(str(a) + '/..', volume, uid=10001, gid=10001)
    monkeypatch.setattr(os, 'geteuid', lambda: 10002)
    with pytest.raises(ValueError, match='trusted local'):
        StateAlias(str(a), volume, uid=10001, gid=10001)


def test_mount_replacement_is_denied(tmp_path, monkeypatch):
    a, b, volume, info = fixture_paths(tmp_path, monkeypatch)
    verifier = StateAlias(str(a), volume, uid=10001, gid=10001)
    try:
        monkeypatch.setattr(StateAlias, '_mount_id', staticmethod(lambda fd: -1))
        with pytest.raises(ValueError, match='mount identity changed'):
            verifier.check()
    finally:
        verifier.close()


def test_failed_chain_open_attempts_every_close(tmp_path, monkeypatch):
    path = tmp_path / 'a' / 'missing'
    path.parent.mkdir()
    original_close = os.close
    calls = []
    def broken_close(fd):
        calls.append(fd)
        original_close(fd)
        if len(calls) == 1:
            raise OSError('injected close failure')
    monkeypatch.setattr(os, 'close', broken_close)
    with pytest.raises(OSError):
        StateAlias._open_chain(str(path))
    assert len(calls) >= 2


def test_private_mount_parser_denies_shared_and_readonly(monkeypatch):
    import io
    import builtins
    original = builtins.open
    def check(line):
        monkeypatch.setattr(builtins, 'open', lambda path, *a, **kw: io.StringIO(line) if path == '/proc/self/mountinfo' else original(path, *a, **kw))
        StateAlias._private_rw(42)
    check('42 1 0:1 / /x rw - tmpfs tmpfs rw\n')
    for line in ('42 1 0:1 / /x ro - tmpfs tmpfs ro\n', '42 1 0:1 / /x rw shared:1 - tmpfs tmpfs rw\n'):
        with pytest.raises(ValueError, match='private writable'):
            check(line)


def test_named_mount_replacement_only(tmp_path, monkeypatch):
    a, b, volume, info = fixture_paths(tmp_path, monkeypatch)
    verifier = StateAlias(str(a), volume, uid=10001, gid=10001)
    original = StateAlias._mount_id
    try:
        pinned = {fd for chain in verifier._chains for _, fd in chain}
        monkeypatch.setattr(StateAlias, '_mount_id', staticmethod(lambda fd: original(fd) if fd in pinned else -1))
        with pytest.raises(ValueError, match='named mount identity changed'):
            verifier.check()
    finally:
        verifier.close()
