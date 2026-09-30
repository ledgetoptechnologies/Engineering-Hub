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
    assert [tool.name for tool in tools] == ["send_message", "receive_messages", "claim_message",
                                              "ack_message", "reject_message", "get_message"]
    send = tools[0]
    assert "recipient" in send.parameters["properties"]
    assert "body" in send.parameters["properties"]
