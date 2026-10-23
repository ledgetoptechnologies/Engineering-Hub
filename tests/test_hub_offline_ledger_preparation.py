"""Disposable Linux read-only classification; no NAS acceptance or writer proof."""
import json
import os
import shutil
import sqlite3
import sys

import pytest

from engineering_hub import offline_ledger_preparation as preparation
from engineering_hub import pilot_enrollment as enrollment
from engineering_hub.pilot_ownership import _local_filesystem
from engineering_hub.repository import HubRepository


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux anchored file inspection')


@pytest.fixture
def files(tmp_path, monkeypatch):
    local = lambda fd: _local_filesystem(fd, allow_ephemeral_test_storage=True)
    monkeypatch.setattr(preparation, '_local_filesystem', local)
    monkeypatch.setattr(enrollment, '_local_filesystem', local)
    shared = tmp_path / 'shared'
    shared.mkdir(mode=0o770)
    source = shared / 'hub.sqlite3'
    repo = HubRepository(source)
    repo.create_task('human:42', 'engineering-hub', 'PRIVATE-TITLE', 'PRIVATE-REQUEST')
    source.chmod(0o770)
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    target = private / 'hub.sqlite3'
    return source, target


def test_source_only_creates_nothing_and_retains_shared_metadata(files):
    source, target = files
    before = source.read_bytes(), source.stat(), sorted(source.parent.iterdir())
    result = preparation.inspect_preparation(source)
    assert result['report']['destination'] is None
    assert result['report']['source']['file']['mode'] == 0o770
    assert all(result['report'][name] is False for name in (
        'byte_and_logical_parity', 'writers_stopped_verified', 'acl_verified',
        'backup_verified', 'source_modified', 'copied', 'enrolled', 'runtime_activated'))
    assert 'PRIVATE-' not in json.dumps(result)
    assert not target.exists()
    after = source.stat()
    assert (before[1].st_mtime_ns, before[1].st_ctime_ns) == (after.st_mtime_ns, after.st_ctime_ns)
    assert source.read_bytes() == before[0] and sorted(source.parent.iterdir()) == before[2]
    assert result['digest'] == enrollment.plan_digest(result['report'])


def test_existing_private_copy_parity_then_separate_enrollment(files):
    source, target = files
    shutil.copyfile(source, target)
    target.chmod(0o600)
    raw = target.read_bytes()
    result = preparation.inspect_preparation(source, destination=target)
    assert result['report']['byte_and_logical_parity'] is True
    assert source.read_bytes() == target.read_bytes() == raw
    plan = result['report']['destination']
    applied = enrollment.apply_plan(plan, enrollment.plan_digest(plan),
        writers_stopped=True, backup_verified=True)
    assert applied['runtime_activated'] is False
    assert source.read_bytes() == raw


@pytest.mark.parametrize('hazard', ['wal', 'shm', 'journal', 'lock', 'hardlink', 'symlink',
    'ancestor-symlink', 'owner', 'unknown', 'active-run', 'oversize'])
def test_source_hazards_refuse_without_repair(files, hazard, monkeypatch):
    source, _ = files
    if hazard in {'wal', 'shm', 'journal', 'lock'}:
        suffix = '.pilot-owner.lock' if hazard == 'lock' else '-' + hazard
        source.with_name(source.name + suffix).write_bytes(b'preserve')
    elif hazard == 'hardlink':
        os.link(source, source.with_name('alias'))
    elif hazard == 'symlink':
        alias = source.with_name('alias')
        alias.symlink_to(source)
        source = alias
    elif hazard == 'ancestor-symlink':
        alias = source.parent.with_name('alias')
        alias.symlink_to(source.parent, target_is_directory=True)
        source = alias / source.name
    elif hazard == 'oversize':
        monkeypatch.setattr(enrollment, '_MAX_DB_BYTES', 1)
    else:
        with sqlite3.connect(source) as db:
            if hazard == 'owner':
                db.execute('CREATE TABLE hub_pilot_owner (id INTEGER)')
            elif hazard == 'unknown':
                db.execute('CREATE TABLE unsupported (id INTEGER)')
            else:
                db.execute('INSERT INTO hub_entities VALUES(?,?,?)', ('run', 'run', '{"status":"running"}'))
    before = source.read_bytes()
    with pytest.raises(preparation.PreparationError):
        preparation.inspect_preparation(source)
    assert source.read_bytes() == before


@pytest.mark.parametrize('hazard', ['missing', 'mode', 'different', 'same', 'sidecar'])
def test_destination_not_created_repaired_or_adopted(files, hazard):
    source, target = files
    if hazard == 'same':
        source.chmod(0o600)
        source.parent.chmod(0o700)
        target = source
    elif hazard != 'missing':
        shutil.copyfile(source, target)
        target.chmod(0o770 if hazard == 'mode' else 0o600)
        if hazard == 'different':
            with sqlite3.connect(target) as db:
                db.execute('PRAGMA user_version=42')
        elif hazard == 'sidecar':
            target.with_name(target.name + '-wal').write_bytes(b'preserve')
    before = target.read_bytes() if target.exists() else None
    with pytest.raises(preparation.PreparationError):
        preparation.inspect_preparation(source, destination=target)
    assert (target.read_bytes() if target.exists() else None) == before


@pytest.mark.parametrize('phase', ['source-write', 'source-rename', 'parent-rename', 'new-sidecar'])
def test_rebinding_or_mutation_during_logical_read_refuses(files, monkeypatch, phase):
    source, _ = files
    original = enrollment._snapshot
    def raced(db):
        result = original(db)
        if phase == 'source-write':
            with source.open('ab') as stream:
                stream.write(b'changed')
        elif phase == 'source-rename':
            old = source.with_name('retained')
            source.rename(old)
            shutil.copyfile(old, source)
        elif phase == 'parent-rename':
            source.parent.rename(source.parent.with_name('retained'))
            source.parent.mkdir()
        else:
            source.with_name(source.name + '-shm').write_bytes(b'preserve')
        return result
    monkeypatch.setattr(enrollment, '_snapshot', raced)
    with pytest.raises(preparation.PreparationError):
        preparation.inspect_preparation(source)


def test_cli_safe_error_and_no_raw_rows(files, capsys):
    source, target = files
    assert preparation.main(['--source', str(source)]) == 0
    assert 'PRIVATE-' not in capsys.readouterr().out
    assert preparation.main(['--source', str(source), '--destination', str(target)]) == 1
    result = capsys.readouterr()
    assert not result.out and 'preserve artifacts' in result.err


def test_destination_mutation_during_final_source_hash_refuses(files, monkeypatch):
    source, target = files
    shutil.copyfile(source, target)
    target.chmod(0o600)
    original = preparation._Source.fingerprint
    calls = 0
    def raced(selected):
        nonlocal calls
        result = original(selected)
        calls += 1
        if calls == 2:
            with sqlite3.connect(target) as db:
                db.execute('PRAGMA user_version=42')
        return result
    monkeypatch.setattr(preparation._Source, 'fingerprint', raced)
    with pytest.raises(preparation.PreparationError):
        preparation.inspect_preparation(source, destination=target)


def test_sqlite_source_inspection_opens_only_memory_not_source_path(files, monkeypatch):
    source, _ = files
    original = sqlite3.connect
    opened = []
    def guarded(database, *args, **kwargs):
        opened.append(database)
        assert database == ':memory:'
        return original(database, *args, **kwargs)
    monkeypatch.setattr(preparation.sqlite3, 'connect', guarded)
    assert preparation.inspect_preparation(source)['report']['source']['snapshot']['tables']
    assert opened == [':memory:']


@pytest.mark.parametrize('mode', ['WAL', 'DELETE'])
def test_original_journal_header_bytes_preserved_in_parity_report(files, mode):
    source, target = files
    with sqlite3.connect(source) as db:
        assert db.execute('PRAGMA journal_mode=' + mode).fetchone()[0] == mode.lower()
    db.close()  # Context manager commits but does not close the WAL connection.
    raw = source.read_bytes()
    assert raw[18:20] == (b'\x02\x02' if mode == 'WAL' else b'\x01\x01')
    shutil.copyfile(source, target)
    target.chmod(0o600)
    report = preparation.inspect_preparation(source, destination=target)['report']
    assert report['source']['memory_view_wal_header_normalized'] is (mode == 'WAL')
    assert report['byte_and_logical_parity'] is True
    assert source.read_bytes() == target.read_bytes() == raw


@pytest.mark.parametrize('header', [b'\x01\x02', b'\x02\x01', b'\x00\x00', b'\x03\x03'])
def test_malformed_journal_header_pairs_are_not_normalized(files, header):
    source, _ = files
    raw = bytearray(source.read_bytes())
    raw[18:20] = header
    source.write_bytes(raw)
    with pytest.raises(preparation.PreparationError):
        preparation.inspect_preparation(source)
    assert source.read_bytes() == raw
