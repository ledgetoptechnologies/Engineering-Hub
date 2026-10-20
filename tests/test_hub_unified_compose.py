"""Small operator Compose; runtime policy lives in the mounted settings file."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_small_compose_has_one_mount_only_tokens_and_persisted_configuration():
    doc = yaml.safe_load((ROOT / 'compose.hub-unified.yaml').read_text())
    assert doc['name'] == 'e-h-control'
    assert set(doc['services']) == {'engineering-hub-control'}
    hub = doc['services']['engineering-hub-control']
    assert hub['command'] == ['--config', '/var/lib/hub/settings.json']
    assert hub['image'] == 'ghcr.io/ledgetoptechnologies/engineering-hub-control:latest'
    assert hub['pull_policy'] == 'always' and hub['restart'] == 'unless-stopped'
    assert hub['network_mode'] == 'host'
    assert hub['volumes'] == ['/mnt/Plugins/App_Data/Engineering-Hub:/var/lib/hub']
    assert not {'ports', 'entrypoint', 'build', 'env_file', 'depends_on'} & hub.keys()
    assert hub['environment'] == {name + '_TOKEN': 'REPLACE_WITH_EXISTING_' + name + '_TOKEN'
                                  for name in ('HERMES', 'CODEX', 'CLAUDE', 'ADMIN')}
    assert hub['cpus'] == 1.0
    assert hub['mem_limit'] == hub['memswap_limit'] == '512m'
    assert hub['stop_grace_period'] == '1200s'
    assert hub['cap_drop'] == ['ALL']
    assert hub['security_opt'] == ['no-new-privileges:true']


def test_settings_example_is_nonsecret_and_valid_after_copy(tmp_path):
    from engineering_hub.deployment_config import load_deployment_config
    text = (ROOT / 'hub-settings.example.json').read_text()
    assert not any(name in text for name in ('HERMES_TOKEN', 'CODEX_TOKEN', 'CLAUDE_TOKEN', 'ADMIN_TOKEN'))
    path = tmp_path / 'settings.json'
    path.write_text(text)
    config = load_deployment_config(path)
    assert config.auth_config.origin == 'https://hub.ledgetoptechnologies.com'
    assert config.auth_config.local_session_ttl_seconds == 30 * 24 * 60 * 60
    assert config.hub_database == tmp_path / 'hub.sqlite3'
    assert config.bus_paths['AGENT_BUS_DB'] == tmp_path / 'agent-message-bus' / 'messages.sqlite3'
    assert all(':8765' not in host for host in config.allowed_hosts)
