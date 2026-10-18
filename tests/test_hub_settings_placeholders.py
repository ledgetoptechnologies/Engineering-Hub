from pathlib import Path
import pytest
from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings
from engineering_hub.auth import AuthConfig


@pytest.mark.parametrize('identity', ['hermes', 'codex', 'claude', 'admin'])
def test_hub_rejects_operator_token_placeholders_before_startup(tmp_path, identity):
    values = dict(hermes_token='synthetic-hermes', codex_token='synthetic-codex',
                  claude_token='synthetic-claude', admin_token=None)
    values[identity + '_token'] = 'REPLACE_WITH_EXISTING_' + identity.upper() + '_TOKEN'
    auth = AuthSettings(**values)
    database = tmp_path / 'not-created' / 'hub.sqlite3'
    with pytest.raises(ValueError, match='Replace agent-token placeholders'):
        Settings(database, auth, AuthConfig(base_url='https://hub.ledgetoptechnologies.com'))
    assert not database.exists()


def test_hub_allows_configured_fixture_tokens_without_modifying_bus_auth(tmp_path):
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    settings = Settings(tmp_path / 'hub.sqlite3', auth, AuthConfig(base_url='https://hub.ledgetoptechnologies.com'))
    assert settings.agent_auth is auth
