"""Explicit archive composition, not operator enrollment or live data proof."""
import sys

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig


def settings(tmp_path):
    return Settings(tmp_path / 'hub.sqlite3',
        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
        AuthConfig('https://hub.test', beau_github_id=42))


def test_archive_is_absent_by_default_and_not_enabled_by_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_ARCHIVE_PATH', '/never-read/archive.sqlite3')
    config = settings(tmp_path)
    app = create_app(config)
    assert app.state.archive_reader is None
    assert not any('/archive/' in path for path in app.openapi()['paths'])
    with TestClient(app, base_url=config.auth_config.origin) as client:
        assert client.get('/api/hub/v1/archive/messages',
            headers={'Authorization': 'Bearer test-codex'}).status_code == 404


def test_nonreader_capability_rejected_before_hub_database_creation(tmp_path):
    config = settings(tmp_path)
    with pytest.raises(ValueError, match='Explicit approved archive reader required'):
        create_app(config, archive_reader=object())
    assert not config.database.exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux readonly reader required')
def test_explicit_reader_uses_real_hub_auth_and_caller_retains_lifetime(tmp_path, monkeypatch):
    from engineering_hub import archive_messages as module
    from engineering_hub.archive_messages import ArchiveMessageReader
    from tests.test_hub_archive_messages import create, sha, IDS
    legacy = tmp_path / 'legacy.sqlite3'
    create(legacy)
    monkeypatch.setattr(module, '_readonly', lambda fd: True)  # Fixture, not kernel mount proof.
    reader = ArchiveMessageReader(legacy, expected_sha256=sha(legacy), maximum_database_bytes=1024**2)
    config = settings(tmp_path)
    app = create_app(config, archive_reader=reader)
    try:
        with TestClient(app, base_url=config.auth_config.origin) as client:
            base = '/api/hub/v1/archive/messages'
            assert client.get(base).status_code == 401
            response = client.get(base, headers={'Authorization': 'Bearer test-codex'})
            assert response.status_code == 200
            assert [item['id'] for item in response.json()['items']] == [IDS[0], IDS[1], IDS[3]]
            assert client.get(base + '/' + IDS[2], headers={
                'Authorization': 'Bearer test-codex'}).status_code == 404
            session = app.state.sessions.create_session(42)
            client.cookies.set(config.auth_config.cookie_name, session['cookie_token'])
            assert len(client.get(base).json()['items']) == 4
            assert client.get(base, headers={'Authorization': 'Bearer invalid'}).status_code == 401
        # HTTP shutdown must not close a capability owned by external composition.
        assert reader.get_message('codex', IDS[0])['archive_inert'] is True
    finally:
        reader.close()
