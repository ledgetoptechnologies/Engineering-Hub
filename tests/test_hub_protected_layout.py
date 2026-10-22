"""Metadata-only protected layout checks; every file below is synthetic.

Unit fixtures inject mountinfo for a disposable directory. The dedicated-mount
test additionally runs against a real isolated container tmpfs at fixed ROOT.
No SQLite ledger contents or real credentials are used by this module's tests.
"""
import builtins
import os
from pathlib import Path
import sqlite3
import stat
import sys

import pytest

from engineering_hub import protected_layout as layout


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux mountinfo and nofollow metadata')


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    root = tmp_path / 'protected'
    root.mkdir(mode=0o700)
    database = root / 'hub.sqlite3'
    lock = root / 'hub.sqlite3.pilot-owner.lock'
    database.write_bytes(b'synthetic-not-a-SQLite-ledger')
    lock.write_bytes(b'')
    database.chmod(0o600)
    lock.chmod(0o600)
    monkeypatch.setattr(layout, 'ROOT', root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        mount_id = layout._mount_id(fd)
    finally:
        os.close(fd)
    device = root.stat().st_dev
    row = f'{mount_id} 1 {os.major(device)}:{os.minor(device)} / {root} rw - tmpfs tmpfs rw\n'
    monkeypatch.setattr(layout, '_read_mountinfo', lambda: row)
    return root, database, lock, row


def test_fixed_production_path_is_not_an_arbitrary_configuration_option(monkeypatch):
    monkeypatch.setattr(layout, 'ROOT', Path('/var/lib/hub-private'))
    assert layout.database_path() == Path('/var/lib/hub-private/hub.sqlite3')


def test_accepts_existing_files_without_opening_contents_or_writing(boundary, monkeypatch):
    root, database, lock, _ = boundary
    original = database.read_bytes()
    original_open = os.open
    opens = []
    def checked_open(path, flags, *args, **kwargs):
        opens.append(os.fspath(path))
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        assert flags & os.O_DIRECTORY or str(path).startswith('/proc/self/')
        return original_open(path, flags, *args, **kwargs)
    with monkeypatch.context() as guard:
        guard.setattr(os, 'open', checked_open)
        guard.setattr(builtins, 'open', lambda *_a, **_k: pytest.fail('Content open'))
        guard.setattr(sqlite3, 'connect', lambda *_a, **_k: pytest.fail('SQLite opened'))
        guard.setattr(os, 'chmod', lambda *_a, **_k: pytest.fail('Permission mutation'))
        guard.setattr(os, 'chown', lambda *_a, **_k: pytest.fail('Owner mutation'))
        assert layout.validate_existing_boundary(database) is None
    assert database.read_bytes() == original and lock.read_bytes() == b''
    assert str(database) not in opens and str(lock) not in opens


@pytest.mark.parametrize('value', [None, True, 'relative/hub.sqlite3', '/var/lib/hub/hub.sqlite3',
    '/var/lib/hub-private//hub.sqlite3', '/var/lib/hub-private/../hub-private/hub.sqlite3'])
def test_wrong_or_noncanonical_database_paths_never_fallback(boundary, value):
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(value)


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'nested-database', 'nested-directory',
    'wrong-device', 'wrong-mount-id', 'malformed', 'bad-escape'])
def test_exact_unambiguous_mountpoint_and_pinned_descriptor_required(boundary, monkeypatch, case):
    root, database, _, row = boundary
    fields = row.split()
    if case == 'missing':
        text = row.replace(str(root), str(root.parent))
    elif case == 'duplicate':
        text = row + row
    elif case.startswith('nested'):
        text = row + row.replace(str(root), str(root / ('hub.sqlite3' if case == 'nested-database' else 'extra')))
    elif case == 'wrong-device':
        fields[2] = '0:999999'
        text = ' '.join(fields)
    elif case == 'wrong-mount-id':
        fields[0] = str(int(fields[0]) + 1)
        text = ' '.join(fields)
    elif case == 'bad-escape':
        text = row.replace(str(root), str(root) + r'\999')
    else:
        text = 'not valid mountinfo\n'
    monkeypatch.setattr(layout, '_read_mountinfo', lambda: text)
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(database)


@pytest.mark.parametrize('name', ['hub.sqlite3', 'hub.sqlite3.pilot-owner.lock'])
@pytest.mark.parametrize('case', ['missing', 'directory', 'symlink', 'hardlink', 'mode', 'wrong-uid', 'wrong-device'])
def test_private_files_must_be_exact_owned_regular_single_link_metadata(boundary, monkeypatch, name, case):
    root, database, _, _ = boundary
    selected = root / name
    if case == 'missing':
        selected.unlink()
    elif case == 'directory':
        selected.unlink()
        selected.mkdir(mode=0o700)
    elif case == 'symlink':
        saved = root / ('saved-' + name)
        selected.rename(saved)
        selected.symlink_to(saved)
    elif case == 'hardlink':
        os.link(selected, root / ('alias-' + name))
    elif case == 'mode':
        selected.chmod(0o640)
    else:
        original_stat = os.stat
        def changed(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == name and kwargs.get('dir_fd') is not None:
                fields = list(value)
                fields[4 if case == 'wrong-uid' else 2] += 1
                return os.stat_result(fields)
            return value
        monkeypatch.setattr(os, 'stat', changed)
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(database)


@pytest.mark.parametrize('case', ['root-mode', 'root-owner', 'root-symlink', 'ancestor-symlink', 'unsafe-ancestor'])
def test_full_private_ancestor_boundary_is_required(boundary, monkeypatch, case):
    root, database, _, _ = boundary
    if case == 'root-mode':
        root.chmod(0o770)
    elif case == 'root-owner':
        original_fstat = os.fstat
        expected = root.stat().st_ino
        def changed(fd):
            value = original_fstat(fd)
            if value.st_ino == expected:
                fields = list(value)
                fields[4] += 1
                return os.stat_result(fields)
            return value
        monkeypatch.setattr(os, 'fstat', changed)
    elif case == 'root-symlink':
        alias = root.with_name('symlink-root')
        alias.symlink_to(root, target_is_directory=True)
        monkeypatch.setattr(layout, 'ROOT', alias)
        database = layout.database_path()
    elif case == 'ancestor-symlink':
        alias = root.parent.with_name(root.parent.name + '-alias')
        alias.symlink_to(root.parent, target_is_directory=True)
        monkeypatch.setattr(layout, 'ROOT', alias / root.name)
        database = layout.database_path()
    else:
        root.parent.chmod(0o777)
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(database)


def test_distinct_shared_and_private_directories_on_same_device_are_valid(boundary):
    root, database, _, _ = boundary
    shared = root.with_name('shared')
    shared.mkdir()
    (shared / 'hub.sqlite3').write_bytes(b'untouched shared historical ledger')
    assert shared.stat().st_dev == root.stat().st_dev
    layout.validate_existing_boundary(database, shared_root=shared)
    assert (shared / 'hub.sqlite3').read_bytes() == b'untouched shared historical ledger'


@pytest.mark.parametrize('alias', ['root', 'database'])
def test_shared_bind_aliases_rejected_by_device_and_inode_not_device_alone(boundary, monkeypatch, alias):
    root, database, _, _ = boundary
    shared = root.with_name('shared')
    shared.mkdir()
    shared_database = shared / 'hub.sqlite3'
    shared_database.write_bytes(b'synthetic')
    original_stat = os.stat
    selected = original_stat(root if alias == 'root' else database)
    def aliased(path, *args, **kwargs):
        if path == (shared if alias == 'root' else shared_database):
            return selected
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', aliased)
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(database, shared_root=shared)


@pytest.mark.parametrize('role', ['agent-message-bus', 'agent-message-bus/messages.sqlite3',
    'agent-message-bus/audit.jsonl', 'agent-message-bus/files'])
def test_conventional_bus_role_aliases_rejected_before_any_owner(boundary, monkeypatch, role):
    root, database, _, _ = boundary
    shared = root.with_name('shared')
    shared.mkdir()
    original_stat = os.stat
    is_directory = role in ('agent-message-bus', 'agent-message-bus/files')
    selected = original_stat(root if is_directory else database)
    def aliased(path, *args, **kwargs):
        if path == shared / role:
            return selected
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', aliased)
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(database, shared_root=shared)


@pytest.mark.parametrize('target', ['root', 'database', 'lock'])
def test_explicit_selected_role_alias_to_private_state_rejected(boundary, monkeypatch, target):
    root, database, lock, _ = boundary
    selected_path = root.with_name('explicit-config-or-bus-role')
    original_stat = os.stat
    selected = original_stat({'root': root, 'database': database, 'lock': lock}[target])
    def aliased(path, *args, **kwargs):
        if path == selected_path:
            return selected
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', aliased)
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(database, shared_paths={'selected_role': selected_path})


def test_explicit_role_paths_allow_distinct_same_device_entries_and_missing_optional_roles(boundary):
    root, database, _, _ = boundary
    existing = root.with_name('selected-nonsecret-config')
    existing.write_bytes(b'synthetic config, never read')
    assert existing.stat().st_dev == database.stat().st_dev
    layout.validate_existing_boundary(database,
        shared_paths={'configuration': existing, 'optional': root.with_name('not-present')})


@pytest.mark.parametrize('paths', ['not-a-path-list', [None], ['relative'], ['//bad/root'],
    ['/bad/../root'], [Path('/duplicate')] * 33])
def test_invalid_or_unbounded_explicit_roles_fail_closed(boundary, paths):
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(boundary[1], shared_paths=paths)


def test_root_or_file_replacement_during_check_is_rejected(boundary, monkeypatch):
    root, database, lock, row = boundary
    first = True
    def replaced():
        nonlocal first
        if first:
            first = False
            root.rename(root.with_name('preserved-old-root'))
            root.mkdir(mode=0o700)
            database.write_bytes(b'synthetic replacement')
            lock.write_bytes(b'')
            database.chmod(0o600)
            lock.chmod(0o600)
        return row
    monkeypatch.setattr(layout, '_read_mountinfo', replaced)
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(database)


def test_mount_generation_change_during_recheck_rejected(boundary, monkeypatch):
    _, database, _, row = boundary
    reads = 0
    def changed():
        nonlocal reads
        reads += 1
        if reads == 1:
            return row
        fields = row.split()
        fields[1] = str(int(fields[1]) + 1)
        return ' '.join(fields)
    monkeypatch.setattr(layout, '_read_mountinfo', changed)
    with pytest.raises(layout.ProtectedLayoutError):
        layout.validate_existing_boundary(database)


def test_proc_read_is_bounded_and_does_not_follow_symlinks(tmp_path):
    oversized = tmp_path / 'synthetic-proc'
    oversized.write_bytes(b'x' * 33)
    with pytest.raises(layout.ProtectedLayoutError):
        layout._proc_text(oversized, 32)
    alias = tmp_path / 'symlink'
    alias.symlink_to(oversized)
    with pytest.raises(OSError):
        layout._proc_text(alias, 64)


def test_mount_escape_parser_handles_only_kernel_path_escapes():
    assert layout._unescape(r'/one\040two/three\134four') == '/one two/three\\four'
    with pytest.raises(layout.ProtectedLayoutError):
        layout._unescape(r'/not\999valid')


def test_actual_dedicated_container_mount_accepts_metadata_only():
    # This path is supplied only as an isolated tmpfs by the explicit local
    # Docker test invocation. Do not create it or adopt an arbitrary host path.
    root = Path('/var/lib/hub-private')
    if not root.exists():
        pytest.skip('Dedicated disposable container mount not supplied')
    rows = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    if not any(row[4] == str(root) and row[row.index('-') + 1] == 'tmpfs' for row in rows):
        pytest.skip('Only a disposable tmpfs is accepted by this fixture')
    assert root.stat().st_uid == os.getuid() and stat.S_IMODE(root.stat().st_mode) == 0o700
    database = root / 'hub.sqlite3'
    lock = root / 'hub.sqlite3.pilot-owner.lock'
    assert not database.exists() and not lock.exists()
    database.write_bytes(b'synthetic-not-a-database')
    lock.write_bytes(b'')
    database.chmod(0o600)
    lock.chmod(0o600)
    layout.validate_existing_boundary(database)
    assert database.read_bytes() == b'synthetic-not-a-database'
