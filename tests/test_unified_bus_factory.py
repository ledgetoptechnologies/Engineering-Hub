import json
import os
import subprocess
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings


def auth():
    return AuthSettings('hermes-test', 'codex-test', claude_token='claude-test')


def build(tmp_path, **kwargs):
    return create_app(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
                      file_root=tmp_path / 'files', auth=kwargs.pop('auth', auth()),
                      push_events_operational=False, **kwargs)


def run_isolated(tmp_path, code):
    # A fresh interpreter proves import-time behavior; a cached module would not.
    env = {key: value for key, value in os.environ.items() if not key.startswith(('AGENT_BUS_', 'HERMES_', 'CODEX_', 'CLAUDE_', 'ADMIN_'))}
    env.update(AGENT_BUS_DB=str(tmp_path / 'default.sqlite3'), AGENT_BUS_AUDIT=str(tmp_path / 'default-audit.jsonl'),
               AGENT_BUS_FILE_ROOT=str(tmp_path / 'default-files'), HERMES_TOKEN='hermes-test',
               CODEX_TOKEN='codex-test', CLAUDE_TOKEN='claude-test')
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_factory_is_inert(tmp_path):
    seen = run_isolated(tmp_path, """
        import json, os
        import agent_message_bus.app as module
        from agent_message_bus.app import create_app
        print(json.dumps({'cached': module._default_app is not None,
                          'entries': sorted(os.listdir('.'))}))
    """)
    assert seen == {'cached': False, 'entries': []}


def test_default_app_is_created_lazily_once(tmp_path):
    seen = run_isolated(tmp_path, """
        import json, os
        import agent_message_bus.app as module
        before = sorted(os.listdir('.'))
        first = module.app
        second = getattr(module, 'app')
        print(json.dumps({'before': before, 'same': first is second, 'cached': module._default_app is first,
                          'db': os.path.exists('default.sqlite3')}))
    """)
    assert seen == {'before': [], 'same': True, 'cached': True, 'db': True}


def test_unknown_module_attribute_still_raises():
    import agent_message_bus.app as module
    with pytest.raises(AttributeError):
        module.not_an_attribute


def test_state_exposes_exact_mcp_and_auth(tmp_path):
    settings = auth()
    app = build(tmp_path, auth=settings)
    assert type(app.state.mcp) is FastMCP
    assert app.state.auth is settings
    assert app.state.service.auth is settings
    assert {tool.name for tool in app.state.mcp._tool_manager.list_tools()} >= {'send_message', 'bus_info'}


def test_injected_transport_security_reaches_session_manager(tmp_path):
    security = TransportSecuritySettings(allowed_hosts=['hub.test:8770'], allowed_origins=[])
    app = build(tmp_path, transport_security_settings=security)
    assert app.state.mcp.settings.transport_security is security
    assert app.state.mcp.session_manager.security_settings is security
    headers = {'Authorization': 'Bearer claude-test', 'Accept': 'application/json, text/event-stream',
               'Content-Type': 'application/json'}
    initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                  'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                             'clientInfo': {'name': 'factory-test', 'version': '1'}}}
    with TestClient(app, base_url='http://hub.test:8770') as client:
        assert client.post('/mcp', headers=headers, json=initialize).status_code == 200
        rejected = client.post('/mcp', headers={**headers, 'Host': 'old-bus.test:8765'}, json=initialize)
        assert rejected.status_code == 421


def test_default_transport_security_is_unchanged(tmp_path):
    app = build(tmp_path)
    security = app.state.mcp.settings.transport_security
    assert type(security) is TransportSecuritySettings
    assert app.state.mcp.session_manager.security_settings is security
    assert '127.0.0.1:8765' in security.allowed_hosts


def seed_retention_rows(tmp_path):
    from datetime import timedelta
    from agent_message_bus.database import MessageRepository, iso, now_utc
    repo = MessageRepository(tmp_path / 'bus.sqlite3')
    acked = repo.insert(sender='hermes', recipient='codex', message_type='a', body={})
    repo.claim(acked.id, 'codex'); repo.acknowledge(acked.id, 'codex')
    claimed = repo.insert(sender='hermes', recipient='codex', message_type='c', body={})
    repo.claim(claimed.id, 'codex')
    with repo.connect() as conn:
        conn.execute('UPDATE messages SET acknowledged_at=? WHERE id=?', (iso(now_utc() - timedelta(days=100)), acked.id))
        conn.execute('UPDATE messages SET claim_expires_at=? WHERE id=?', (iso(now_utc() - timedelta(minutes=5)), claimed.id))
    return repo, acked.id, claimed.id


def test_skipping_startup_cleanup_preserves_expired_state(tmp_path):
    repo, acked, claimed = seed_retention_rows(tmp_path)
    build(tmp_path, startup_cleanup=False)
    assert repo.get(acked).status == 'acknowledged'
    assert repo.get(claimed).status == 'claimed'


def test_default_startup_cleanup_unchanged(tmp_path):
    repo, acked, claimed = seed_retention_rows(tmp_path)
    build(tmp_path)
    with pytest.raises(Exception):
        repo.get(acked)
    assert repo.get(claimed).status == 'pending'


@pytest.mark.parametrize('bad', [0, 1, None, 'false'])
def test_invalid_startup_cleanup_rejected_before_side_effects(tmp_path, bad):
    with pytest.raises(ValueError, match='startup_cleanup'):
        build(tmp_path, startup_cleanup=bad)
    assert not (tmp_path / 'bus.sqlite3').exists()
    assert not (tmp_path / 'files').exists()


@pytest.mark.parametrize('bad', [{'allowed_hosts': ['hub.test']}, 'hub.test', object()])
def test_invalid_transport_security_rejected_before_side_effects(tmp_path, bad):
    with pytest.raises(ValueError, match='transport_security_settings'):
        build(tmp_path, transport_security_settings=bad)
    assert not (tmp_path / 'bus.sqlite3').exists()
    assert not (tmp_path / 'files').exists()
