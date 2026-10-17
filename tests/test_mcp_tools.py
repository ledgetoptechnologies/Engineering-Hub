from types import SimpleNamespace

from starlette.requests import Request

from agent_message_bus.api import Service
from agent_message_bus.audit import AuditLog
from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import MessageRepository
from agent_message_bus.mcp_server import build_mcp
from agent_message_bus.retention import RetentionPolicy


def test_mcp_exposes_exact_lifecycle_tools_and_schemas(tmp_path):
    service = Service(MessageRepository(tmp_path / "m.sqlite3"), AuditLog(tmp_path / "audit.jsonl"),
                      AuthSettings("h", "c"), RetentionPolicy())
    tools = build_mcp(service)._tool_manager.list_tools()
    assert [tool.name for tool in tools] == ["send_message", "get_send_receipt", "presence_status",
                                              "topic_presence_join", "topic_presence_leave", "topic_presence_status",
                                              "receive_messages", "wait_for_messages", "claim_message",
                                              "ack_message", "reject_message", "get_message",
                                              "knowledge_put", "knowledge_get", "knowledge_list",
                                              "knowledge_search", "semantic_search", "semantic_status",
                                              "file_put", "file_get", "file_list", "file_history",
                                              "feedback_submit", "feedback_get", "feedback_list", "feedback_update", "bus_info"]
    send = tools[0]
    assert "recipient" in send.parameters["properties"]
    assert "body" in send.parameters["properties"]
    assert "in_reply_to" in send.parameters["properties"]
    assert "in_reply_to" not in send.parameters["required"]

    for name, expected in {
        "topic_presence_join": {"topic"},
        "topic_presence_status": {"topic"},
        "topic_presence_leave": {"topic", "generation"},
    }.items():
        tool = next(item for item in tools if item.name == name)
        assert set(tool.parameters["properties"]) == expected
        assert "agent" not in tool.parameters["properties"]
        assert "session_id" not in tool.parameters["properties"]
        assert tool.parameters["properties"]["topic"]["pattern"] == r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
        assert tool.parameters["properties"]["topic"]["minLength"] == 1
        assert tool.parameters["properties"]["topic"]["maxLength"] == 128
    leave = next(item for item in tools if item.name == "topic_presence_leave")
    assert leave.parameters["properties"]["generation"]["pattern"] == r"^[0-9a-f]{32}$"

    put = next(tool for tool in tools if tool.name == "knowledge_put")
    assert set(put.parameters["required"]) == {"project", "path", "title", "content", "expected_version"}
    assert "author" not in put.parameters["properties"]
    assert "identity" not in put.parameters["properties"]
    for name in ("knowledge_get", "knowledge_list", "knowledge_search"):
        tool = next(item for item in tools if item.name == name)
        assert "identity" not in tool.parameters["properties"]


def test_mcp_send_message_accepts_correlated_reply(tmp_path):
    service = Service(MessageRepository(tmp_path / "m.sqlite3"), AuditLog(tmp_path / "audit.jsonl"),
                      AuthSettings("h", "c"), RetentionPolicy())
    send = next(tool for tool in build_mcp(service)._tool_manager.list_tools() if tool.name == "send_message").fn

    def context(token):
        request = Request({
            "type": "http", "method": "POST", "scheme": "http", "server": ("test", 80),
            "client": ("127.0.0.1", 1234), "root_path": "", "path": "/mcp", "raw_path": b"/mcp",
            "query_string": b"", "headers": [(b"authorization", f"Bearer {token}".encode())], "state": {},
        })
        return SimpleNamespace(request_context=SimpleNamespace(request=request))

    parent = send(recipient="codex", message_type="task", body={}, ctx=context("h"))
    reply = send(recipient="hermes", message_type="reply", body={}, in_reply_to=parent["id"], ctx=context("c"))
    assert reply["in_reply_to"] == parent["id"]
    assert reply["hop_count"] == 1
