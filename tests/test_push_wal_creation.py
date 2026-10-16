"""A queued WAL create may hide a commit made before the inode watch exists."""
import gc
import sqlite3
import threading
from pathlib import Path

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

    class DelayedWalCreate(SQLiteEventSource):
        delayed = False

        def _process_data(self, data):
            if not self.delayed and self._wal_wd is None and wal.name.encode() in data:
                self.delayed = True
                pending.set()
                if not release.wait(5):
                    raise RuntimeError('fixture failed to release queued WAL creation')
            return super()._process_data(data)

    source = DelayedWalCreate(db, MessageCursorReader(), lambda rows, cursor: delivered.append((rows,cursor)))
    try:
        source.start()
        assert pending.wait(3), 'fixture must capture the real queued WAL creation event'
        repository.insert(sender='hermes', recipient='codex', message_type='INFO', body={})
        head = repository.delivery_head()
        release.set()
        assert source.wait_for_cursor(head, timeout=2), 'committed row must not be stranded behind WAL creation'
        assert source.healthy
        assert any(cursor == head and 'codex' in rows for rows,cursor in delivered)
    finally:
        release.set()
        source.stop()
