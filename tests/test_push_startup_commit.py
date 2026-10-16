"""A commit may arrive after watch readiness but before ASGI startup returns."""
import importlib

from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from agent_message_bus.database import MessageRepository


def test_commit_during_startup_does_not_fail_ready_push_source(tmp_path, monkeypatch):
    app_module = importlib.import_module('agent_message_bus.app')

    class CommitAtReadiness:
        def __init__(self, path, read_after, deliver, **kwargs):
            self.path, self.deliver = path, deliver
            self.running = False
            self.healthy = True
            self.stats = {'cursor': 0}

        def start(self):
            self.running = True
            repository = MessageRepository(self.path)
            repository.insert(sender='hermes', recipient='codex', message_type='INFO', body={})
            self.stats['cursor'] = repository.delivery_head()
            self.deliver(['codex'], self.stats['cursor'])

        def stop(self):
            self.running = False

    monkeypatch.setattr(app_module, 'native_event_support', lambda path: (True, 'isolated readiness interleaving'))
    monkeypatch.setattr(app_module, 'SQLiteEventSource', CommitAtReadiness)
    app = app_module.create_app(db_path=tmp_path/'ready.sqlite3', audit_path=tmp_path/'audit.jsonl',
                                file_root=tmp_path/'files', auth=AuthSettings('hermes-fixture', 'codex-fixture',
                                claude_token='claude-fixture'), push_events_operational=True)
    with TestClient(app) as client:
        health = client.get('/health').json()
        assert health['push']['state'] == 'healthy'
        assert health['push']['subscriptions_enabled'] is True
        assert health['push']['cursor'] > 0
    assert not app.state.push_event_source.running
