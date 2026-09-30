from agent_message_bus.retention import RetentionPolicy


def test_retention_has_safe_minimums():
    assert RetentionPolicy(0, -1).safe() == RetentionPolicy(1, 1)
