from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def append(self, *, operation: str, agent: str, message_id: str | None = None,
               recipient: str | None = None, result: str = "ok", correlation_id: str | None = None) -> None:
        record: dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                  "operation": operation, "agent": agent, "message_id": message_id,
                                  "recipient": recipient, "result": result, "correlation_id": correlation_id}
        with self._lock:
            try:
                previous = ""
                if self.path.exists():
                    lines = self.path.read_text().splitlines()
                    if lines:
                        previous = json.loads(lines[-1])["hash"]
                record["prev_hash"] = previous
                payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
                record["hash"] = hashlib.sha256(payload.encode()).hexdigest()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                self.last_error = None
            except Exception as exc:  # audit must not break successful work
                self.last_error = str(exc)
                log.exception("audit log append failed")

    def verify(self) -> bool:
        previous = ""
        try:
            if not self.path.exists(): return True
            for line in self.path.read_text().splitlines():
                record = json.loads(line)
                actual = record.pop("hash")
                if record.get("prev_hash", "") != previous:
                    return False
                expected = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if not hmac_compare(actual, expected): return False
                previous = actual
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False


def hmac_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)
