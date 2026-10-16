"""Deterministic regression for the independently reviewed sequence-head race."""
from agent_message_bus.database import MessageRepository


def test_delivery_head_uses_one_snapshot_across_concurrent_commit(tmp_path):
    reader = MessageRepository(tmp_path / 'race.sqlite3')
    writer = MessageRepository(reader.path)
    writer.insert(sender='hermes', recipient='codex', message_type='INFO', body={})
    before = reader.delivery_head()

    class InterleavingConnection:
        def __init__(self, conn):
            self.conn = conn
            self.inserted = False

        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            # With the rejected two-query implementation this commits between
            # the old high-water read and the new MAX mapping read.
            if 'sqlite_sequence' in sql and not self.inserted:
                self.inserted = True
                writer.insert(sender='hermes', recipient='codex', message_type='INFO', body={})
            return result

    with reader.connect() as conn:
        interleaving = InterleavingConnection(conn)
        assert reader._delivery_head(interleaving) == before
        assert interleaving.inserted
    assert reader.delivery_head() > before
    page, cursor = reader.list_after('codex', after=before)
    assert len(page) == 1 and page[0]['seq'] == cursor
