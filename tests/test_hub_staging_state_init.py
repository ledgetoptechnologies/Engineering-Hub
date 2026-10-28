"""Disposable Linux initialization; no runtime, service, or external state."""
import os
import sqlite3
import stat
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux ownership prerequisite')

from engineering_hub import pilot_ownership, staging_state_init as init
from engineering_hub.repository import HubRepository


@pytest.fixture
def private_parent(tmp_path, monkeypatch):
    # Use the existing explicit test-storage seam, while retaining the real
    # production owner type, identity, kernel lock, and filesystem checks.
    actual = pilot_ownership._local_filesystem
    def disposable(fd, **_kwargs):
        return actual(fd, allow_ephemeral_test_storage=True)
    monkeypatch.setattr(pilot_ownership, '_local_filesystem', disposable)
    monkeypatch.setattr(init, '_local_filesystem', disposable)
    parent = tmp_path / 'trusted'
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    return parent


def test_fresh_state_reopens_under_existing_owner(private_parent):
    root = private_parent / 'staging'
    init.initialize_staging_state(root)
    bus = root / 'agent-message-bus'
    for directory in (root, bus, bus / 'files'):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file in (root / 'hub.sqlite3', root / 'hub.sqlite3.pilot-owner.lock',
                 bus / 'messages.sqlite3', bus / 'audit.jsonl'):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert (bus / 'audit.jsonl').read_bytes() == b''
    with pilot_ownership.PilotOwner(root / 'hub.sqlite3', require_existing=True,
                                    allow_ephemeral_test_storage=True) as owner:
        assert owner.previous_epoch is not None
        repo = HubRepository(root / 'hub.sqlite3', pilot_owner=owner)
        with repo._connection() as db:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='hub_entities'").fetchone()
        repo.close()
    with sqlite3.connect(bus / 'messages.sqlite3') as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'messages', 'files'} <= names


def test_existing_root_and_symlink_are_refused(private_parent):
    outside = private_parent / 'outside'
    outside.mkdir(mode=0o700)
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'unchanged')
    root = private_parent / 'staging'
    root.mkdir(mode=0o700)
    with pytest.raises(init.StagingStateInitError, match='^staging state initialization unavailable$'):
        init.initialize_staging_state(root)
    assert list(root.iterdir()) == []
    root.rmdir()
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(init.StagingStateInitError):
        init.initialize_staging_state(root)
    assert sentinel.read_bytes() == b'unchanged'
    assert list(outside.iterdir()) == [sentinel]


def test_noncanonical_path_and_unsafe_parent_refused(private_parent):
    root = private_parent / 'staging'
    with pytest.raises(init.StagingStateInitError):
        init.initialize_staging_state(str(private_parent) + '/./staging')
    private_parent.chmod(0o755)
    with pytest.raises(init.StagingStateInitError):
        init.initialize_staging_state(root)
    assert not root.exists()


def test_writable_grandparent_refused_before_creation(private_parent):
    grandparent = private_parent / 'unsafe'
    grandparent.mkdir(mode=0o700)
    parent = grandparent / 'private'
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    grandparent.chmod(0o777)
    root = parent / 'staging'
    try:
        with pytest.raises(init.StagingStateInitError):
            init.initialize_staging_state(root)
        assert not root.exists()
    finally:
        grandparent.chmod(0o700)


def test_collision_preserves_partial_state_and_external_sentinel(private_parent, monkeypatch):
    outside = private_parent / 'outside'
    outside.write_bytes(b'untouched')
    real_new_file = init._new_file
    def collision(fd, name):
        if name == 'messages.sqlite3':
            os.symlink(outside, name, dir_fd=fd)
        return real_new_file(fd, name)
    monkeypatch.setattr(init, '_new_file', collision)
    root = private_parent / 'staging'
    with pytest.raises(init.StagingStateInitError):
        init.initialize_staging_state(root)
    assert (root / 'hub.sqlite3').is_file()
    assert (root / 'agent-message-bus' / 'messages.sqlite3').is_symlink()
    assert outside.read_bytes() == b'untouched'
    with pytest.raises(init.StagingStateInitError):
        init.initialize_staging_state(root)


def test_repository_failure_preserves_created_artifacts(private_parent, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError('sensitive detail')
    monkeypatch.setattr(init, 'FileRepository', fail)
    root = private_parent / 'staging'
    with pytest.raises(init.StagingStateInitError) as error:
        init.initialize_staging_state(root)
    assert str(error.value) == 'staging state initialization unavailable'
    assert error.value.__suppress_context__
    assert (root / 'hub.sqlite3').exists()
    assert (root / 'agent-message-bus' / 'messages.sqlite3').exists()
    assert (root / 'agent-message-bus' / 'audit.jsonl').exists()
    assert (root / 'agent-message-bus' / 'files').is_dir()


def test_cli_calls_initializer_once_and_reports_bounded_success(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(init, 'initialize_staging_state', calls.append)
    init.main(['--new-state-dir', '/private/example'])
    assert calls == ['/private/example']
    captured = capsys.readouterr()
    assert captured.out == '{"status":"initialized"}\n'
    assert captured.err == ''


@pytest.mark.parametrize('argv', [[], ['--new-state', '/private/secret'],
                                  ['--new-state-dir'],
                                  ['--new-state-dir', '/private/secret', '--unknown-secret']])
def test_cli_rejects_arguments_without_echoing_them(argv, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(init, 'initialize_staging_state', calls.append)
    with pytest.raises(SystemExit) as error:
        init.main(argv)
    assert error.value.code == 2
    assert calls == []
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'staging state initialization unavailable\n'


def test_cli_sanitizes_initializer_failure(monkeypatch, capsys):
    calls = []
    def fail(path):
        calls.append(path)
        raise RuntimeError('private failure details')
    monkeypatch.setattr(init, 'initialize_staging_state', fail)
    with pytest.raises(SystemExit) as error:
        init.main(['--new-state-dir', '/private/example'])
    assert error.value.code == 2
    assert calls == ['/private/example']
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'staging state initialization unavailable\n'
