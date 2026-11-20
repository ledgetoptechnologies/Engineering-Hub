#!/usr/bin/env python3
"""Read-only, content-free status for a local MCP push worker state database."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote


def _snapshot(database: Path, limit: int) -> dict[str, object]:
    uri = f"file:{quote(database.as_posix(), safe='/:')}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cursor_row = conn.execute("SELECT value FROM state WHERE key='cursor'").fetchone() if "state" in tables else None
        dropped_row = conn.execute(
            "SELECT value FROM state WHERE key='routing_issues_dropped'"
        ).fetchone() if "state" in tables else None
        queued_count = int(conn.execute("SELECT COUNT(*) FROM queue WHERE status='queued'").fetchone()[0]) if "queue" in tables else 0
        issues = []
        if "routing_issues" in tables:
            issues = [dict(row) for row in conn.execute(
                "SELECT message_id,seq,reason FROM routing_issues ORDER BY seq DESC LIMIT ?", (limit,)
            )]
    return {
        "cursor": int(cursor_row[0]) if cursor_row is not None else None,
        "queued_count": queued_count,
        "routing_issues_dropped": int(dropped_row[0]) if dropped_row is not None else 0,
        "routing_issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only local MCP push worker diagnostics")
    parser.add_argument("--state-dir", required=True, type=Path,
                        help="worker state directory passed to mcp_push_worker.py")
    parser.add_argument("--limit", type=int, default=100,
                        help="maximum recent routing issue records to print (1-1000)")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
    database = args.state_dir.expanduser().resolve() / "mcp-push-worker.sqlite3"
    if not database.is_file():
        print("worker state database not found", file=sys.stderr)
        return 2
    try:
        print(json.dumps(_snapshot(database, args.limit), indent=2, sort_keys=True))
    except (OSError, sqlite3.Error, ValueError):
        print("could not read worker state database", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
