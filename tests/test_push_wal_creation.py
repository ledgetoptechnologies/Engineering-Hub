"""A queued WAL creation must not hide commits during startup reconciliation."""
import gc
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from agent_message_bus.database import MessageRepository
from agent_message_bus.sqlite_events import MessageCursorReader, SQLiteEventSource


def test_queued_wal_creation_reconciles_commits_before_watch_installation(tmp_path):
    db = tmp_path/'creation.sqlite3'
    repository = MessageRepository(db)
    gc.collect()
    connection = sqlite3.connect(db)
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
    connection.close()
    wal = Path(str(db)+'-wal')
    if wal.exists():
        assert wal.stat().st_size == 0
        wal.unlink()  # Isolated empty fixture only, no live application data.
    pending = threading.Event()
    release = threading.Event()
    delivered = []
    baseline = []
    errors = []

    class DelayedWalCreate(SQLiteEventSource):
        delayed = False

        def _setup(self):
            super()._setup()
            # The directory watch exists, but its queued WAL creation has not
            # yet been processed. This commit belongs to the startup snapshot.
            repository.insert(sender='hermes', recipient='codex', message_type='INFO', body={})
            baseline.append(repository.delivery_head())

        def _process_data(self, data, *, reconcile=True):
            if not self.delayed and self._wal_wd is None and wal.name.encode() in data:
                self.delayed = True
                pending.set()
                if not release.wait(5):
                    raise RuntimeError('fixture failed to release queued WAL creation')
            return super()._process_data(data, reconcile=reconcile)

    source = DelayedWalCreate(db, MessageCursorReader(), lambda rows, cursor: delivered.append((rows,cursor)))

    def start_source():
        try:
            source.start()
        except Exception as error:
            errors.append(error)

    starter = threading.Thread(target=start_source)
    try:
        starter.start()
        assert pending.wait(3), 'fixture must capture the real queued WAL creation event'
        assert len(baseline) == 1
        with closing(sqlite3.connect(db, timeout=0, isolation_level=None)) as contender:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                contender.execute('BEGIN IMMEDIATE')
        release.set()
        starter.join(5)
        assert not starter.is_alive()
        assert not errors, errors
        assert source.stats['cursor'] == baseline[0]
        assert not delivered, 'startup snapshot must not replay historical recipients'
        repository.insert(sender='hermes', recipient='codex', message_type='INFO', body={})
        head = repository.delivery_head()
        assert head > baseline[0]
        assert source.wait_for_cursor(head, timeout=2), 'committed row must not be stranded behind WAL creation'
        assert source.healthy
        assert any(cursor == head and 'codex' in rows for rows,cursor in delivered)
    finally:
        release.set()
        starter.join(5)
        source.stop()
