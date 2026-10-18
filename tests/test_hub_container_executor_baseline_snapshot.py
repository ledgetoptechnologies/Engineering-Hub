"""Actual private SQLite fixtures only; not baseline health or Docker proof."""
from contextlib import closing
import errno
import hashlib
import base64
import json
import os
from pathlib import Path
import sqlite3
import sys
import subprocess
import threading
import time

import pytest

if sys.platform != 'linux':
    pytest.skip('Linux host storage primitive', allow_module_level=True)

from hub_container_executor import baseline_snapshot as module
from hub_container_executor.baseline_snapshot import BaselineSnapshotError, freeze_baseline_snapshot

pytestmark = pytest.mark.skipif(os.getuid() != 10001 or os.getgid() != 10001,
                                reason='Actual isolated app UID/GID 10001 required')


@pytest.fixture
def rig(tmp_path):
    root = tmp_path / 'generations'
    root.mkdir(mode=0o700)
    generation = root / 'baseline-one'
    generation.mkdir(mode=0o700)
    source = generation / 'hub.sqlite3'
    fd = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    writer = sqlite3.connect(source)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('CREATE TABLE t(value TEXT)')
    writer.execute("INSERT INTO t VALUES('committed')")
    writer.commit()
    # An existing quiet read transaction shares the established WAL read mark;
    # the helper may not change SHM bytes to make its own inventory pass.
    reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    reader.execute('BEGIN')
    assert reader.execute('SELECT value FROM t').fetchall() == [('committed',)]
    yield root, generation, source, writer, reader
    reader.close()
    writer.close()


def freeze(rig, **changes):
    hook = changes.pop('_test_hook', None)
    arguments = dict(generation_root=rig[0], source_generation_id='baseline-one',
                     frozen_leaf='frozen.sqlite3', maximum_bytes=2 * 1024**2,
                     deadline_monotonic=time.monotonic() + 10,
                     allow_ephemeral_test_storage=True)
    arguments.update(changes)
    arguments['generation_root'] = str(arguments['generation_root'])
    # The baseline writer remains in THIS process; the helper always runs in a
    # fresh single-task operator process with no inherited source descriptors.
    # Static unit-test fault hooks also execute only in that isolated process.
    script = '''
import json, sys
from pathlib import Path
from hub_container_executor import baseline_snapshot as module
payload = json.load(sys.stdin)
root = Path(payload['arguments']['generation_root'])
generation = root / payload['arguments']['source_generation_id']
if payload['hook'] is not None:
    exec(payload['hook'])
try:
    result = module.freeze_baseline_snapshot(**payload['arguments'])
    from dataclasses import asdict
    print(json.dumps({'ok': True, 'result': asdict(result)}))
except module.BaselineSnapshotError as error:
    print(json.dumps({'ok': False, 'error': str(error)}))
'''
    child = subprocess.run([sys.executable, '-c', script],
                           input=json.dumps({'arguments': arguments, 'hook': hook}),
                           text=True, capture_output=True, timeout=15)
    assert child.returncode == 0 and child.stderr == ''
    output = json.loads(child.stdout)
    if not output['ok']:
        raise BaselineSnapshotError(output['error'])
    result = output['result']
    return module.FrozenBaselineSnapshot(result['sha256'], result['size_bytes'],
        tuple(tuple(item) for item in result['source_inventory']),
        tuple(tuple(item) for item in result['source_identities']))


def original_bytes(generation):
    # Never raw-open/close a source inode in the process holding its SQLite
    # connections: even a test read could release that process's POSIX locks.
    script = '''
import base64, json, sys
from pathlib import Path
root = Path(json.load(sys.stdin))
print(json.dumps({p.name: base64.b64encode(p.read_bytes()).decode('ascii')
                  for p in root.iterdir() if p.is_file()}))
'''
    child = subprocess.run([sys.executable, '-c', script], input=json.dumps(str(generation)),
                           text=True, capture_output=True, timeout=5)
    assert child.returncode == 0 and child.stderr == ''
    return {name: base64.b64decode(value) for name, value in json.loads(child.stdout).items()}


def test_actual_quiet_wal_snapshot_only_normalizes_owned_copy(rig):
    root, generation, source, writer, reader = rig
    before = original_bytes(generation)
    result = freeze(rig)
    assert original_bytes(generation) == before
    frozen = root / 'frozen.sqlite3'
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == result.sha256.removeprefix('sha256:')
    assert frozen.stat().st_size == result.size_bytes
    assert frozen.stat().st_nlink == 1 and frozen.stat().st_mode & 0o777 == 0o600
    assert source.stat().st_nlink == 1
    assert {item[0] for item in result.source_inventory} == set(before)
    assert json.loads(result.canonical_source_inventory())['files'] == [list(item) for item in result.source_inventory]
    with closing(sqlite3.connect(frozen.as_uri() + '?mode=ro', uri=True)) as db:
        assert db.execute('PRAGMA journal_mode').fetchone() == ('delete',)
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('SELECT value FROM t').fetchall() == [('committed',)]
    assert writer.execute('PRAGMA journal_mode').fetchone() == ('wal',)
    assert reader.execute('SELECT value FROM t').fetchall() == [('committed',)]
    assert set(p.name for p in root.iterdir()) == {'baseline-one', 'frozen.sqlite3'}


def test_actual_wal_without_extra_reader_is_accepted_only_if_inventory_unchanged(rig):
    root, generation, _, _, reader = rig
    reader.close()
    before = original_bytes(generation)
    try:
        freeze(rig)
    except BaselineSnapshotError:
        # Read-only SQLite may change read marks on some versions. That is not
        # permission to checkpoint/rewrite the source or relax SHM inventory.
        assert not (root / 'frozen.sqlite3').exists()
    else:
        assert original_bytes(generation) == before


def test_existing_frozen_collision_never_overwritten_or_removed(rig):
    frozen = rig[0] / 'frozen.sqlite3'
    frozen.write_bytes(b'operator-existing-artifact')
    frozen.chmod(0o600)
    with pytest.raises(BaselineSnapshotError):
        freeze(rig)
    assert frozen.read_bytes() == b'operator-existing-artifact'


@pytest.mark.parametrize('where', ['root', 'generation'])
def test_foreign_entries_fail_closed_without_cleanup(rig, where):
    directory = rig[0 if where == 'root' else 1]
    foreign = directory / 'foreign.bin'
    foreign.write_bytes(b'preserve')
    foreign.chmod(0o600)
    with pytest.raises(BaselineSnapshotError):
        freeze(rig)
    assert foreign.read_bytes() == b'preserve'
    assert not (rig[0] / 'frozen.sqlite3').exists()


def test_all_original_files_count_toward_byte_budget(rig):
    footprint = sum(p.stat().st_size for p in rig[1].iterdir())
    with pytest.raises(BaselineSnapshotError, match='byte_limit'):
        freeze(rig, maximum_bytes=footprint - 1)
    assert not (rig[0] / 'frozen.sqlite3').exists()


@pytest.mark.parametrize('changes', [dict(maximum_bytes=True), dict(maximum_bytes=0),
    dict(deadline_monotonic=float('nan')), dict(deadline_monotonic=True),
    dict(source_generation_id='../baseline'), dict(frozen_leaf='../frozen.sqlite3'),
    dict(frozen_leaf='baseline-one'), dict(frozen_leaf='validation-owner.lock'),
    dict(frozen_leaf='snapshot-owned.sqlite3')])
def test_strict_explicit_configuration(rig, changes):
    with pytest.raises(BaselineSnapshotError):
        freeze(rig, **changes)
    assert not (rig[0] / 'frozen.sqlite3').exists()


def test_expired_deadline_has_no_effect(rig):
    before = original_bytes(rig[1])
    with pytest.raises(BaselineSnapshotError, match='deadline'):
        freeze(rig, deadline_monotonic=time.monotonic() - 1)
    assert original_bytes(rig[1]) == before


def test_source_shm_mutation_during_copy_denied(rig):
    hook = '''
import os
real = module.copy_sqlite_generation
def mutated(*args, **kwargs):
    copied = real(*args, **kwargs)
    with (generation / 'hub.sqlite3-shm').open('r+b') as file:
        file.seek(-1, os.SEEK_END)
        old = file.read(1)
        file.seek(-1, os.SEEK_END)
        file.write(bytes([old[0] ^ 1]))
    return copied
module.copy_sqlite_generation = mutated
'''
    with pytest.raises(BaselineSnapshotError, match='inventory_changed'):
        freeze(rig, _test_hook=hook)
    assert not (rig[0] / 'frozen.sqlite3').exists()
    assert not any(p.name.startswith('snapshot-') for p in rig[1].iterdir())


def test_private_permissions_required_without_repair(rig):
    source = rig[2]
    source.chmod(0o640)
    try:
        with pytest.raises(BaselineSnapshotError, match='private_snapshot_file'):
            freeze(rig)
        assert source.stat().st_mode & 0o777 == 0o640
    finally:
        source.chmod(0o600)


def test_source_hardlink_rejected_without_removing_either_name(rig):
    other = rig[0].parent / 'other-db-name'
    os.link(rig[2], other)
    with pytest.raises(BaselineSnapshotError, match='private_snapshot_file'):
        freeze(rig)
    assert other.exists() and rig[2].stat().st_nlink == 2


def test_source_symlink_rejected_without_following_or_removing(rig):
    moved = rig[0].parent / 'original-db'
    rig[2].rename(moved)
    rig[2].symlink_to(moved)
    with pytest.raises(BaselineSnapshotError):
        freeze(rig)
    assert rig[2].is_symlink() and moved.exists()


def test_generation_directory_replacement_fails_and_cleans_only_owned_temp(rig):
    saved = rig[0] / 'original-baseline'
    hook = '''
real = module.copy_sqlite_generation
def replaced(*args, **kwargs):
    copied = real(*args, **kwargs)
    generation.rename(root / 'original-baseline')
    generation.mkdir(mode=0o700)
    (generation / 'foreign').write_bytes(b'new-directory')
    return copied
module.copy_sqlite_generation = replaced
'''
    with pytest.raises(BaselineSnapshotError):
        freeze(rig, _test_hook=hook)
    assert (rig[1] / 'foreign').read_bytes() == b'new-directory'
    assert (saved / 'hub.sqlite3').exists()
    assert not any(p.name.startswith('snapshot-') for p in saved.iterdir())
    assert not (rig[0] / 'frozen.sqlite3').exists()


def test_unknown_copier_temporary_collision_is_not_deleted(rig):
    hook = '''
def collision(source, destination, **kwargs):
    Path(destination).write_bytes(b'unknown-creator')
    Path(destination).chmod(0o600)
    raise RuntimeError('unknown copier failure')
module.copy_sqlite_generation = collision
'''
    with pytest.raises(BaselineSnapshotError):
        freeze(rig, _test_hook=hook)
    unknown = [p for p in rig[1].iterdir() if p.name.startswith('snapshot-')]
    assert len(unknown) == 1 and unknown[0].read_bytes() == b'unknown-creator'


def test_no_cross_device_publication_fallback(rig):
    hook = '''
import errno
def refused(*args, **kwargs):
    raise OSError(errno.EXDEV, 'synthetic cross-device')
module.os.link = refused
'''
    with pytest.raises(BaselineSnapshotError):
        freeze(rig, _test_hook=hook)
    assert not (rig[0] / 'frozen.sqlite3').exists()
    assert not any(p.name.startswith('snapshot-') for p in rig[1].iterdir())


def test_published_frozen_preserved_on_postpublication_uncertainty(rig):
    hook = '''
real = module.os.link
def published_then_uncertain(*args, **kwargs):
    real(*args, **kwargs)
    raise RuntimeError('uncertain return after actual exclusive link')
module.os.link = published_then_uncertain
'''
    with pytest.raises(BaselineSnapshotError):
        freeze(rig, _test_hook=hook)
    frozen = rig[0] / 'frozen.sqlite3'
    assert frozen.exists() and frozen.stat().st_nlink == 1
    assert not any(p.name.startswith('snapshot-') for p in rig[1].iterdir())


def test_same_process_sqlite_writer_or_reader_is_rejected_before_raw_db_open(rig):
    before = original_bytes(rig[1])
    with pytest.raises(BaselineSnapshotError, match='operator_owns_source_descriptor|dedicated_snapshot_operator'):
        freeze_baseline_snapshot(generation_root=rig[0], source_generation_id='baseline-one',
                                 frozen_leaf='frozen.sqlite3', maximum_bytes=2 * 1024**2,
                                 deadline_monotonic=time.monotonic() + 5,
                                 allow_ephemeral_test_storage=True)
    assert original_bytes(rig[1]) == before
    assert not (rig[0] / 'frozen.sqlite3').exists()


def test_kernel_multithread_operator_rejected_in_fresh_process(rig):
    hook = '''
import threading
release = threading.Event()
thread = threading.Thread(target=lambda: release.wait(2), daemon=True)
thread.start()
'''
    with pytest.raises(BaselineSnapshotError, match='dedicated_snapshot_operator'):
        freeze(rig, _test_hook=hook)
    assert not (rig[0] / 'frozen.sqlite3').exists()


def test_single_thread_source_descriptor_is_rejected_in_fresh_process(rig):
    hook = '''
import sqlite3
source_connection = sqlite3.connect(generation / 'hub.sqlite3')
source_connection.execute('SELECT value FROM t').fetchall()
'''
    before = original_bytes(rig[1])
    with pytest.raises(BaselineSnapshotError, match='snapshot_operator_owns_source_descriptor'):
        freeze(rig, _test_hook=hook)
    assert original_bytes(rig[1]) == before
    assert not (rig[0] / 'frozen.sqlite3').exists()
