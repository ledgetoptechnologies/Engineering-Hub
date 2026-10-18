"""The published dashboard contract must match the actual route schema."""
import json
from pathlib import Path

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig


def test_checked_in_openapi_matches_app(tmp_path):
    app = create_app(Settings(tmp_path / 'hub.db',
        AuthSettings('contract-fixture-hermes', 'contract-fixture-codex'),
        AuthConfig('https://hub.invalid')))
    expected = json.loads((Path(__file__).parents[1] / 'contracts/hub-v1.openapi.json').read_text())
    assert app.openapi() == expected
    public = expected['components']['schemas']['PublicReachability']['properties']
    assert 'valid_until' in public
    assert not {'session_id', 'observer_id', 'observer_epoch', 'connector_generation'} & public.keys()
