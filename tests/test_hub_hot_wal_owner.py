"""Actual SIGKILL/WAL reads before any successor write or epoch adoption."""
from contextlib import ExitStack
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys

import pytest

from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubRepository


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux kernel pilot ownership')


_CHILD = '''
import json, signal, sys, uuid
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubRepository

with PilotOwner(sys.argv[1], allow_ephemeral_test_storage=True) as owner:
    repo = HubRepository(sys.argv[1], pilot_owner=owner)
    committed = repo.create_task('human:123', 'engineering-hub', 'Committed before crash', 'committed')
    with repo._transaction() as db:
        db.execute('PRAGMA cache_size=1')
        db.execute('PRAGMA cache_spill=ON')
        # Force dirty pages beyond the tiny cache; none of this transaction is
        # committed. The already committed task must still remain readable.
        for _ in range(32):
            pending = dict(committed, id=str(uuid.uuid4()), title='Uncommitted ' + 'x' * 8192)
            db.execute('INSERT INTO hub_entities VALUES (?, ?, ?)',
                       (pending['id'], 'task', json.dumps(pending)))
        print('ready', flush=True)
        signal.pause()
'''


def test_read_committed_hot_wal_before_successor_epoch_write(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'hub.sqlite'
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    child = subprocess.Popen(
        [sys.executable, '-c', _CHILD, str(path)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env, close_fds=True,
    )
    with ExitStack() as cleanup:
        for stream in (child.stdout, child.stderr):
            cleanup.callback(stream.close)
        try:
            # A bounded readiness handshake, not an unbounded readline/sleep.
            with selectors.DefaultSelector() as ready:
                ready.register(child.stdout, selectors.EVENT_READ)
                assert ready.select(timeout=15), 'WAL writer did not become ready'
                assert os.read(child.stdout.fileno(), 64) == b'ready\n'
            wal, shm = Path(str(path) + '-wal'), Path(str(path) + '-shm')
            assert wal.exists() and shm.exists()
            assert wal.stat().st_size > 32
            child.kill()
            assert child.wait(timeout=10) == -signal.SIGKILL
            assert wal.exists() and shm.exists()

            # Do not call HubRepository.__init__ or PilotOwner yet: both may
            # write. This exercises the actual existing-ledger read boundary,
            # without creating a writer or claiming a successor epoch first.
            reader = HubRepository.__new__(HubRepository)
            reader.path = str(path)
            reader.pilot_owner = None
            reader.execution_principal = None
            tasks = reader.list_tasks()
            assert [task['title'] for task in tasks] == ['Committed before crash']
            with reader._connection() as db:
                assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
                with pytest.raises(sqlite3.OperationalError, match='readonly'):
                    db.execute('DELETE FROM hub_entities')

            # Only after proving the hot-WAL read may the next genuine owner
            # acquire the lock and persist its new epoch.
            with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
                successor = HubRepository(path, pilot_owner=owner)
                assert successor.list_tasks() == tasks
                created = successor.create_task('human:123', 'engineering-hub', 'After restart', 'restart')
                assert {task['id'] for task in successor.list_tasks()} == {tasks[0]['id'], created['id']}
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
