"""Persisted configuration is shared by CLI, Hub and preserved bus factory."""
import json
import os
import sys
from pathlib import Path

import pytest

from engineering_hub.app import Settings
from engineering_hub.deployment_config import load_deployment_config


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    source = Path(__file__).parents[1] / 'hub-settings.example.json'
    settings = json.loads(source.read_text())
    settings['agent_allowed_ips'] = ['127.0.0.1']
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(settings))
    for agent in ('HERMES', 'CODEX', 'CLAUDE', 'ADMIN'):
        monkeypatch.setenv(agent + '_TOKEN', 'synthetic-file-' + agent.lower())
    return load_deployment_config(path), path


def test_file_settings_override_only_deployment_values_not_tokens(deployment, monkeypatch):
    config, _ = deployment
    monkeypatch.setenv('HUB_DB', '/wrong/hub.sqlite3')
    monkeypatch.setenv('AGENT_BUS_DB', '/wrong/messages.sqlite3')
    monkeypatch.setenv('HUB_BASE_URL', 'https://wrong.example')
    monkeypatch.setenv('HUB_HUMAN_AUTH_MODE', 'github')
    monkeypatch.setenv('HUB_AGENT_ALLOWED_HOSTS', 'wrong.example')
    monkeypatch.setenv('AGENT_BUS_ALLOWED_IPS', '192.0.2.1')
    before = dict(os.environ)
    result = Settings.from_env(config)
    assert dict(os.environ) == before
    assert result.database == config.hub_database
    assert result.auth_config == config.auth_config
    assert result.agent_allowed_hosts == config.allowed_hosts
    assert result.agent_auth.allowed_ips == frozenset({'127.0.0.1'})
    assert result.agent_auth.codex_token == 'synthetic-file-codex'
    assert result.work_queue_enabled is result.registry_enabled is False


def test_config_cli_selects_unified_pair_and_passes_same_typed_config(deployment, monkeypatch):
    from engineering_hub import __main__ as cli, frontends, server, unified_app
    config, path = deployment
    monkeypatch.setattr(sys, 'argv', ['hub', '--host', '0.0.0.0', '--config', str(path)])
    calls = []
    core, pair, pending = object(), object(), object()
    def create(settings, *, bus_paths):
        assert settings.database == config.hub_database
        assert settings.agent_auth.allowed_ips == frozenset(config.allowed_ips)
        assert bus_paths == config.bus_paths
        calls.append('create')
        return core
    def wrap(value):
        assert value is core
        return pair
    def serve(factory, **kwargs):
        assert calls == []
        assert kwargs == {'agent_host': '0.0.0.0', 'agent_port': 8770,
                          'browser_host': '0.0.0.0', 'browser_port': 8771, 'agent_alias_port': None}
        assert factory() is pair
        return pending
    def run(value):
        assert value is pending
    monkeypatch.setattr(unified_app, 'create_unified_app', create)
    monkeypatch.setattr(frontends, 'HubFrontends', wrap)
    monkeypatch.setattr(server, 'serve_frontends', serve)
    monkeypatch.setattr(cli.asyncio, 'run', run)
    cli.main()
    assert calls == ['create']


def test_cli_missing_config_fails_without_constructing_resources(tmp_path, monkeypatch, capsys):
    from engineering_hub import __main__ as cli, unified_app
    monkeypatch.setattr(sys, 'argv', ['hub', '--config', str(tmp_path / 'missing.json')])
    monkeypatch.setattr(unified_app, 'create_unified_app', lambda *a, **kw: pytest.fail('constructed'))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert 'configure it before startup' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_config_cli_rejects_alias(deployment, monkeypatch):
    from engineering_hub import __main__ as cli
    _, path = deployment
    monkeypatch.setattr(sys, 'argv', ['hub', '--config', str(path), '--agent-alias-port', '8765'])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_file_factory_missing_bus_state_does_not_create_history(deployment):
    from engineering_hub.unified_app import create_unified_app
    config, path = deployment
    with pytest.raises(ValueError, match='Existing separate bus and Hub state'):
        create_unified_app(Settings.from_env(config), bus_paths=config.bus_paths)
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.skipif(sys.platform != 'linux', reason='Actual unified bus owner on Linux')
def test_file_factory_reads_preserved_bus_paths_without_environment(deployment, monkeypatch):
    from agent_message_bus.database import MessageRepository
    from engineering_hub.unified_app import create_unified_app
    config, _ = deployment
    paths = config.bus_paths
    paths['AGENT_BUS_DB'].parent.mkdir()
    repository = MessageRepository(paths['AGENT_BUS_DB'])
    message = repository.insert(sender='hermes', recipient='codex', message_type='info', body={})
    paths['AGENT_BUS_AUDIT'].touch()
    paths['AGENT_BUS_FILE_ROOT'].mkdir()
    for name in paths:
        monkeypatch.delenv(name, raising=False)
    core = create_unified_app(Settings.from_env(config), bus_paths=paths)
    assert core.state.embedded_bus.state.service.auth.codex_token == 'synthetic-file-codex'
    assert repository.get(message.id).id == message.id
    assert core.state.settings.database == config.hub_database
