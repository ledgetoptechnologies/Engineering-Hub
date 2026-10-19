"""Explicit preservation first, original startup retention after verification."""
from datetime import timedelta
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux bus factory')


def test_invalid_retention_mode_fails_before_bus_construction(tmp_path, monkeypatch):
    import agent_message_bus.app as bus_module
    from engineering_hub.unified_app import create_unified_app
    for value in ('true', 'false', '', '2', ' 1'):
        monkeypatch.setenv('HUB_BUS_STARTUP_CLEANUP', value)
        monkeypatch.setattr(bus_module, 'create_app', lambda **kw: pytest.fail('No bus construction'))
        # Validate the mode before any factory state is opened. Supply a valid
        # settings object without requiring any real database paths.
        with pytest.raises(ValueError, match='HUB_BUS_STARTUP_CLEANUP'):
            create_unified_app(settings=object())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('mode', [None, '0', '1'])
def test_unified_factory_preserves_then_explicitly_restores_startup_cleanup(tmp_path, monkeypatch, mode):
    from agent_message_bus.auth import AuthSettings
    from agent_message_bus.database import MessageRepository, iso, now_utc
    from engineering_hub.app import Settings
    from engineering_hub.auth import AuthConfig
    from engineering_hub.unified_app import create_unified_app

    if mode is None:
        monkeypatch.delenv('HUB_BUS_STARTUP_CLEANUP', raising=False)
    else:
        monkeypatch.setenv('HUB_BUS_STARTUP_CLEANUP', mode)
    auth = AuthSettings('retention-hermes', 'retention-codex', claude_token='retention-claude')
    database = tmp_path / 'bus.sqlite3'
    repo = MessageRepository(database)
    acked = repo.insert(sender='hermes', recipient='codex', message_type='info', body={})
    repo.claim(acked.id, 'codex')
    repo.acknowledge(acked.id, 'codex')
    claimed = repo.insert(sender='hermes', recipient='codex', message_type='info', body={})
    repo.claim(claimed.id, 'codex')
    with repo.connect() as connection:
        connection.execute('UPDATE messages SET acknowledged_at=? WHERE id=?',
                           (iso(now_utc() - timedelta(days=100)), acked.id))
        connection.execute('UPDATE messages SET claim_expires_at=? WHERE id=?',
                           (iso(now_utc() - timedelta(minutes=5)), claimed.id))
    audit = tmp_path / 'audit.jsonl'
    audit.touch()
    files = tmp_path / 'files'
    files.mkdir()
    for name, value in (('AGENT_BUS_DB', database), ('AGENT_BUS_AUDIT', audit),
                        ('AGENT_BUS_FILE_ROOT', files)):
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv('ACK_RETENTION_DAYS', '30')
    settings = Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(
        'https://hub.test', human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://fixture.cloudflareaccess.com', cloudflare_audience='fixture',
        cloudflare_allowed_emails=('beau@example.invalid',)))
    core = create_unified_app(settings)
    assert core.state.embedded_bus.state.service.auth is auth
    with repo.connect() as connection:
        ack_row = connection.execute('SELECT status FROM messages WHERE id=?', (acked.id,)).fetchone()
        ack_status = tuple(ack_row) if ack_row is not None else None
    if mode == '1':
        assert ack_status is None
        assert repo.get(claimed.id).status == 'pending'
    else:
        assert ack_status == ('acknowledged',)
        assert repo.get(claimed.id).status == 'claimed'
