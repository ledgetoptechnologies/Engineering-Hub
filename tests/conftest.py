"""Keep host-side tests isolated from the Docker deployment .env."""
from __future__ import annotations

import os
from pathlib import Path


_TEST_DATA = Path(__file__).parent / ".test-data"
_TEST_DATA.mkdir(exist_ok=True)
os.environ["AGENT_BUS_DB"] = str(_TEST_DATA / "messages.sqlite3")
os.environ["AGENT_BUS_AUDIT"] = str(_TEST_DATA / "audit.jsonl")
os.environ["HERMES_TOKEN"] = "test-hermes-token"
os.environ["CODEX_TOKEN"] = "test-codex-token"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
