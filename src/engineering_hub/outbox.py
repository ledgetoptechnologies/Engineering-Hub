"""Bounded, explicitly invoked delivery of Hub event pointers to the agent bus.

The injected client owns authentication. This module never handles agent tokens,
starts a watcher, or treats delivery as execution/approval acknowledgement.
"""
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3
import uuid


class HubOutboxPublisher:
    def __init__(self, repo, bus_client, *, recipients=("hermes", "claude"),
                 topic="auto-wake-v1-test", correlation_id="engineering-hub-events"):
        self.path = repo.path
        self.client = bus_client
        self.recipients = tuple(dict.fromkeys(recipients))
        if not self.recipients or not all(x in {"hermes", "claude", "codex"} for x in self.recipients):
            raise ValueError("Recipients must be configured agent identities")
        if topic != "auto-wake-v1-test":
            raise ValueError("Publisher topic must use the approved linked discussion")
        if not isinstance(correlation_id, str) or not re.fullmatch(r"[a-zA-Z0-9_./:-]{1,128}", correlation_id):
            raise ValueError("Invalid correlation identifier")
        self.topic, self.correlation_id = topic, correlation_id
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS hub_delivery_namespace (id INTEGER PRIMARY KEY CHECK(id=1), namespace TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO hub_delivery_namespace VALUES (1, ?)", (str(uuid.uuid4()),))
            self.namespace = db.execute("SELECT namespace FROM hub_delivery_namespace WHERE id=1").fetchone()[0]
            db.execute("""CREATE TABLE IF NOT EXISTS hub_delivery_receipts (
                sequence INTEGER NOT NULL, recipient TEXT NOT NULL, request_json TEXT NOT NULL,
                message_id TEXT, PRIMARY KEY(sequence, recipient))""")

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _prepare(self, limit):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            candidates = []
            for recipient in self.recipients:
                rows = db.execute("""SELECT o.* FROM hub_outbox o LEFT JOIN hub_delivery_receipts d
                    ON d.sequence=o.sequence AND d.recipient=? WHERE d.message_id IS NULL
                    ORDER BY o.sequence LIMIT ?""", (recipient, limit)).fetchall()
                candidates.extend((dict(row), recipient) for row in rows)
            candidates.sort(key=lambda item: (item[0]["sequence"], item[1]))
            for event, recipient in candidates[:limit]:
                fingerprint = json.dumps([self.namespace, event["sequence"], event["action"],
                                          event["entity_id"], event["revision"], recipient], separators=(",", ":"))
                key = "hub-event-" + hashlib.sha256(fingerprint.encode()).hexdigest()
                request = {"recipient": recipient, "message_type": "info", "priority": "normal",
                           "correlation_id": self.correlation_id, "client_request_id": key,
                           "body": {"topic": self.topic, "event": event["action"],
                                    "entity_id": event["entity_id"], "revision": event["revision"],
                                    "outbox_sequence": event["sequence"]}}
                db.execute("INSERT OR IGNORE INTO hub_delivery_receipts(sequence,recipient,request_json) VALUES(?,?,?)",
                           (event["sequence"], recipient, json.dumps(request, sort_keys=True, separators=(",", ":"))))
            if not candidates:
                return []
            result = []
            for event, recipient in candidates[:limit]:
                row = db.execute("SELECT sequence,recipient,request_json FROM hub_delivery_receipts WHERE sequence=? AND recipient=? AND message_id IS NULL",
                                 (event["sequence"], recipient)).fetchone()
                if row:
                    result.append(dict(row))
            return result

    def _mark_delivered(self, sequence, recipient, message_id):
        with self._db() as db:
            db.execute("UPDATE hub_delivery_receipts SET message_id=? WHERE sequence=? AND recipient=? AND message_id IS NULL",
                       (message_id, sequence, recipient))

    def _pending(self):
        with self._db() as db:
            return sum(db.execute("""SELECT COUNT(*) FROM hub_outbox o LEFT JOIN hub_delivery_receipts d
                ON d.sequence=o.sequence AND d.recipient=? WHERE d.message_id IS NULL""", (recipient,)).fetchone()[0]
                       for recipient in self.recipients)

    async def drain_batch(self, limit=50):
        """Send at most limit pointers. Replays use the exact persisted request.

        Delivery is at-least-once; bus client_request_id deduplication is required.
        The first uncertain/failing result stops the batch for safe explicit retry.
        """
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Batch size must be between 1 and 500")
        delivered, failed = 0, 0
        for delivery in self._prepare(limit):
            try:
                result = await self.client.send_message(**json.loads(delivery["request_json"]))
                message_id = result.get("id") if isinstance(result, dict) else None
                if not isinstance(message_id, str) or str(uuid.UUID(message_id)) != message_id:
                    raise ValueError("Invalid delivery receipt")
                self._mark_delivered(delivery["sequence"], delivery["recipient"], message_id)
                delivered += 1
            except Exception:
                # Deliberately omit exception text: clients may include secrets.
                failed += 1
                break
        return {"delivered": delivered, "failed": failed, "pending": self._pending()}
