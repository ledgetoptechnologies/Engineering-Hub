"""Agent-facing contracts, separate from backend repository tests."""
import base64
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from agent_message_bus.api import Service
from agent_message_bus.audit import AuditLog
from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import MessageRepository
from agent_message_bus.files import FileRepository
from agent_message_bus.mcp_server import build_mcp
from agent_message_bus.retention import RetentionPolicy


def context(token):
    request = Request({
        "type": "http", "method": "POST", "scheme": "http", "server": ("test", 80),
        "client": ("127.0.0.1", 1234), "root_path": "", "path": "/mcp", "raw_path": b"/mcp",
        "query_string": b"", "headers": [(b"authorization", f"Bearer {token}".encode())], "state": {},
    })
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


@pytest.fixture
def tools(tmp_path):
    db = tmp_path / "messages.sqlite3"
    service = Service(MessageRepository(db), AuditLog(tmp_path / "audit.jsonl"),
                      AuthSettings("h", "c", claude_token="a"), RetentionPolicy(),
                      FileRepository(db, tmp_path / "files"))
    service.semantic = SimpleNamespace(
        search=lambda **kwargs: [{"source": "knowledge", "score": 0.8, "project": "demo", "path": "notes.md"}],
        status=lambda: {"available": True, "model": "test-only"},
    )
    return {tool.name: tool for tool in build_mcp(service)._tool_manager.list_tools()}


def test_tools_expose_shared_storage_without_identity_or_delete(tools):
    assert {"semantic_search", "semantic_status", "file_put", "file_get", "file_list", "file_history"} <= tools.keys()
    assert "file_delete" not in tools
    for name in ("file_put", "file_get", "file_list", "file_history", "semantic_search", "semantic_status"):
        assert not {"identity", "author", "sender"} & tools[name].parameters["properties"].keys()
    assert set(tools["file_put"].parameters["required"]) == {"project", "path", "content", "expected_version"}


def test_three_authenticated_agents_share_versioned_text(tools):
    put, get = tools["file_put"].fn, tools["file_get"].fn
    first = put(project="demo", path="docs/plan.md", content="First plan", expected_version=0, ctx=context("h"))
    assert first["version"] == 1 and first["created_by"] == "hermes"
    second = get(project="demo", path="docs/plan.md", ctx=context("c"))
    assert second["content"] == "First plan" and second["sha256"] == first["sha256"]
    updated = put(project="demo", path="docs/plan.md", content="Revised plan", expected_version=1, ctx=context("a"))
    assert updated["version"] == 2 and updated["updated_by"] == "claude"
    with pytest.raises(ValueError):
        put(project="demo", path="docs/plan.md", content="Stale plan", expected_version=1, ctx=context("c"))
    assert get(project="demo", path="docs/plan.md", version=1, ctx=context("c"))["content"] == "First plan"
    assert get(project="demo", path="docs/plan.md", ctx=context("h"))["content"] == "Revised plan"
    listed = tools["file_list"].fn(project="demo", ctx=context("a"))
    assert len(listed["files"]) == 1
    assert listed["files"][0]["path"] == "docs/plan.md"
    versions = tools["file_history"].fn(project="demo", path="docs/plan.md", ctx=context("c"))
    assert {v["version"] for v in versions["versions"]} == {1, 2}


def test_binary_roundtrip_and_size_validation(tools):
    data = b"\x89PNG\x00\xff\x01"
    put, get = tools["file_put"].fn, tools["file_get"].fn
    first = put(project="demo", path="media/test.bin", content=base64.b64encode(data).decode(),
                encoding="base64", expected_version=0, ctx=context("c"))
    result = get(project="demo", path="media/test.bin", encoding="base64", ctx=context("a"))
    assert base64.b64decode(result["content"]) == data and result["sha256"] == first["sha256"]
    with pytest.raises(ValueError):
        put(project="demo", path="too-big.txt", content="x" * (1048576 + 1), expected_version=0, ctx=context("h"))
    with pytest.raises(ValueError):
        put(project="demo", path="bad.bin", content="!not-base64!", encoding="base64", expected_version=0, ctx=context("h"))


@pytest.mark.parametrize("path", ["../secret", "/absolute", "a/../secret", "a\\secret"])
def test_unsafe_paths_rejected(tools, path):
    with pytest.raises(ValueError):
        tools["file_put"].fn(project="demo", path=path, content="no", expected_version=0, ctx=context("h"))


def test_semantic_calls_are_authenticated_and_qualified(tools):
    result = tools["semantic_search"].fn(query="find maintenance instructions", project="demo", ctx=context("h"))
    assert result["mode"] == "semantic" and result["results"][0]["score"] == 0.8
    assert tools["semantic_status"].fn(ctx=context("a"))["available"] is True
    with pytest.raises(Exception):
        tools["semantic_search"].fn(query="test", ctx=context("bad"))
