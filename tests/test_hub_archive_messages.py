"""Actual SQLite/files, synthetic readonly-mount flag—not kernel RO proof."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

if sys.platform != 'linux':
    pytest.skip('Actual Linux owned descriptors and immutable URI required', allow_module_level=True)

from agent_message_bus.database import MessageRepository
from engineering_hub import archive_messages as module
from engineering_hub.archive_messages import ArchiveMessageReader, ArchiveReadError


IDS = [f'00000000-0000-0000-0000-{number:012d}' for number in range(1, 6)]
EXPECTED_KEYS = {'archive_inert', 'id', 'sender', 'recipient', 'message_type', 'priority', 'body',
                 'status', 'correlation_id', 'in_reply_to', 'created_at'}


def create(path, *, change=None, hub=False):
    rows = []
    for i, (sender, recipient, status, body) in enumerate([
        ('hermes', 'codex', 'pending', {'text': 'original\nbody', 'value': 2.5}),
        ('codex', 'claude', 'acknowledged', ['original', 3]),
        ('hermes', 'claude', 'claimed', {'private': 'not-for-codex'}),
        ('claude', 'codex', 'rejected', 'historical rejection'),
    ]):
        rows.append(dict(id=IDS[i], sender=sender, recipient=recipient, message_type='discussion', priority='normal',
                         body_json=json.dumps(body), status=status, correlation_id='topic-alpha',
                         in_reply_to=IDS[0] if i else None, hop_count=0, requires_user_approval=0,
                         created_at='2026-10-01T00:00:00Z', claimed_by=None, claim_expires_at=None,
                         acknowledged_at=None, rejected_at=None, rejection_reason=None))
    if change:
        change(rows)
    with closing(sqlite3.connect(path)) as db:
        db.execute(MessageRepository._migration_table_sql().replace('messages__migration', 'messages'))
        for row in rows:
            db.execute('INSERT INTO messages VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        if hub:
            db.execute('CREATE TABLE hub_entities(id TEXT PRIMARY KEY,kind TEXT,data TEXT)')
        db.commit()
    return rows


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def archive(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.sqlite3'
    rows = create(path)
    before = path.read_bytes()
    # Explicit test-only substitution. All file/FD/SQLite/hash/ACL checks are
    # real, but this writable temporary directory is NOT kernel RO evidence.
    monkeypatch.setattr(module, '_readonly', lambda fd: True)
    reader = ArchiveMessageReader(path, expected_sha256=sha(path), maximum_database_bytes=1024**2)
    yield reader, path, rows, before
    reader.close()


def test_sender_or_recipient_history_and_no_unauthorized_existence_oracles(archive):
    reader, _, _, _ = archive
    assert [item['id'] for item in reader.list_messages('codex')['items']] == [IDS[0], IDS[1], IDS[3]]
    assert [item['id'] for item in reader.list_messages('hermes')['items']] == [IDS[0], IDS[2]]
    assert [item['id'] for item in reader.list_messages('claude')['items']] == [IDS[1], IDS[2], IDS[3]]
    assert reader.get_message('codex', IDS[2]) is None
    assert reader.get_message('codex', IDS[4]) is None
    assert reader.get_message('claude', IDS[2])['body'] == {'private': 'not-for-codex'}


@pytest.mark.parametrize('principal', ['human:42', 'human:' + '9' * 20, 'human:cf:' + 'a' * 64])
def test_exact_authenticated_owner_principals_can_read_all(archive, principal):
    assert len(archive[0].list_messages(principal)['items']) == 4


@pytest.mark.parametrize('principal', ['human:0', 'human:-1', 'human:042', 'human:anyone', 'human:cf:abc',
                                       'human:' + '1' * 21, 'human:cf:' + 'A' * 64, 'admin', '', None, 42])
def test_no_request_selected_or_malformed_owner_identity(archive, principal):
    with pytest.raises(ArchiveReadError):
        archive[0].list_messages(principal)


def test_original_body_exact_projection_inert_status_and_pagination(archive):
    reader, path, rows, before = archive
    item = reader.get_message('codex', IDS[0])
    assert set(item) == EXPECTED_KEYS
    assert item['body'] == json.loads(rows[0]['body_json']) and item['status'] == 'pending'
    assert item['archive_inert'] is True
    first = reader.list_messages('codex', limit=2)
    assert first['archive_inert'] is True and first['next_after_id'] == IDS[1]
    second = reader.list_messages('codex', limit=2, after_id=first['next_after_id'])
    assert [item['id'] for item in second['items']] == [IDS[3]] and second['next_after_id'] is None
    assert not any(hasattr(reader, method) for method in ('claim', 'ack', 'send', 'reject', 'execute'))
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ['legacy.sqlite3']


@pytest.mark.parametrize('value', [True, 0, 51, '2', None])
def test_list_limit_strict_and_query_injection_rejected(archive, value):
    with pytest.raises(ArchiveReadError):
        archive[0].list_messages('codex', limit=value)
    for identity in ("' OR 1=1 --", '../legacy', IDS[0] + 'x'):
        with pytest.raises(ArchiveReadError):
            archive[0].get_message('codex', identity)
        with pytest.raises(ArchiveReadError):
            archive[0].list_messages('codex', after_id=identity)


def test_frozen_hash_or_inode_tampering_cannot_be_adopted(archive):
    reader, path, _, before = archive
    path.rename(path.with_suffix('.original'))
    path.write_bytes(before)
    with pytest.raises(ArchiveReadError):
        reader.get_message('codex', IDS[0])


def test_same_inode_content_tampering_is_rejected(archive):
    reader, path, _, before = archive
    inode = path.stat().st_ino
    path.write_bytes(b'corrupted-header' + before[16:])
    assert path.stat().st_ino == inode
    with pytest.raises(ArchiveReadError):
        reader.list_messages('codex')


def test_changed_bytes_readonly_flag_or_sqlite_companions_deny(archive, monkeypatch):
    reader, path, _, _ = archive
    monkeypatch.setattr(module, '_readonly', lambda fd: False)
    with pytest.raises(ArchiveReadError):
        reader.list_messages('codex')
    monkeypatch.setattr(module, '_readonly', lambda fd: True)
    sidecar = Path(str(path) + '-wal')
    sidecar.write_bytes(b'')
    with pytest.raises(ArchiveReadError):
        reader.list_messages('codex')


@pytest.mark.parametrize('attack', ['symlink_leaf', 'symlink_parent', 'hardlink', 'hub_schema', 'wrong_hash', 'size', 'writable_mount'])
def test_binding_denies_unsafe_or_unapproved_database(tmp_path, monkeypatch, attack):
    root = tmp_path / 'approved'
    root.mkdir()
    path = root / 'legacy.sqlite3'
    create(path, hub=attack == 'hub_schema')
    expected = sha(path)
    maximum = 1024**2
    monkeypatch.setattr(module, '_readonly', lambda fd: attack != 'writable_mount')
    if attack == 'symlink_leaf':
        actual = root / 'original.sqlite3'
        path.rename(actual)
        path.symlink_to(actual)
    elif attack == 'symlink_parent':
        alias = tmp_path / 'alias'
        alias.symlink_to(root, target_is_directory=True)
        path = alias / path.name
    elif attack == 'hardlink':
        os.link(path, root / 'another-name')
    elif attack == 'wrong_hash': expected = 'f' * 64
    elif attack == 'size': maximum = 512
    with pytest.raises(ArchiveReadError) as error:
        ArchiveMessageReader(path, expected_sha256=expected, maximum_database_bytes=maximum)
    assert str(error.value) == error.value.code == 'archive_read_unavailable'
    assert str(path) not in str(error.value)


@pytest.mark.parametrize('change', [dict(timeout_seconds=True), dict(timeout_seconds=0), dict(timeout_seconds=31),
                                   dict(timeout_seconds=float('nan')), dict(maximum_database_bytes=True),
                                   dict(expected_sha256='sha256:' + 'a' * 64)])
def test_constructor_inputs_bounded_and_not_coerced(tmp_path, change):
    arguments = dict(expected_sha256='a' * 64, maximum_database_bytes=1024**2)
    arguments.update(change)
    with pytest.raises(ArchiveReadError):
        ArchiveMessageReader(tmp_path / 'never-created', **arguments)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('body', ['NaN', '1e309', '{"duplicate":1,"duplicate":2}', '"\\ud800"',
                                  '[' * 21 + '0' + ']' * 21, json.dumps('x' * 65537),
                                  json.dumps([0] * 4097)])
def test_visible_body_bounds_and_safe_json_fail_closed(tmp_path, monkeypatch, body):
    path = tmp_path / 'legacy.sqlite3'
    create(path, change=lambda rows: rows[0].update(body_json=body))
    monkeypatch.setattr(module, '_readonly', lambda fd: True)
    reader = ArchiveMessageReader(path, expected_sha256=sha(path), maximum_database_bytes=1024**2)
    try:
        with pytest.raises(ArchiveReadError):
            reader.get_message('codex', IDS[0])
    finally:
        reader.close()


def test_closed_pid_and_deadline_guards_never_read_with_wrong_process(archive, monkeypatch):
    reader = archive[0]
    pid = reader._pid
    monkeypatch.setattr(module.os, 'getpid', lambda: pid + 1)
    with pytest.raises(ArchiveReadError):
        reader.list_messages('codex')
    monkeypatch.setattr(module.os, 'getpid', lambda: pid)
    ticks = iter(range(0, 10000, 100))
    monkeypatch.setattr(module.time, 'monotonic', lambda: float(next(ticks)))
    with pytest.raises(ArchiveReadError):
        reader.list_messages('codex')
    reader.close()
    reader.close()
    with pytest.raises(ArchiveReadError):
        reader.get_message('codex', IDS[0])


def test_close_releases_only_reader_owned_descriptors(archive, tmp_path):
    reader = archive[0]
    unrelated = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        reader.close()
        assert os.fstat(unrelated).st_ino == tmp_path.stat().st_ino
    finally:
        os.close(unrelated)
