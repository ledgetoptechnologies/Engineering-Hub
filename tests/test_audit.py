import json

from agent_message_bus.audit import AuditLog, correlation_id_context, request_id_context


def test_audit_chain_is_redacted_and_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    audit.append(operation="send", agent="hermes", message_id="id", recipient="codex", correlation_id="c")
    audit.append(operation="acknowledge", agent="codex", message_id="id", recipient="codex")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert audit.verify()
    assert rows[1]["prev_hash"] == rows[0]["hash"]
    assert "token" not in path.read_text().lower()
    assert "body" not in path.read_text().lower()
    path.write_text(path.read_text().replace('"result":"ok"', '"result":"tampered"', 1))
    assert not audit.verify()


def test_audit_inherits_request_observability_context(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)
    request_token = request_id_context.set("request-123")
    correlation_token = correlation_id_context.set("correlation-456")
    try:
        audit.append(operation="claim", agent="codex", message_id="message-1")
    finally:
        correlation_id_context.reset(correlation_token)
        request_id_context.reset(request_token)

    record = json.loads(path.read_text().splitlines()[0])
    assert record["request_id"] == "request-123"
    assert record["correlation_id"] == "correlation-456"
    assert audit.verify()
