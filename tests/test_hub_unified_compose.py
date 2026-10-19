"""Source-only in-place cutover profile, never an installed NAS assertion."""
from pathlib import Path

import pytest
import yaml

from engineering_hub.app import Settings


ROOT = Path(__file__).resolve().parents[1]


def service():
    document = yaml.safe_load((ROOT / 'compose.hub-unified.yaml').read_text())
    assert document['name'] == 'e-h-control'
    assert set(document['services']) == {'engineering-hub-control'}
    return document['services']['engineering-hub-control']


def test_one_existing_hub_with_both_original_mcp_ports_and_no_extra_owner():
    hub = service()
    assert hub['command'] == ['--split-ingress', '--unified-mcp', '--browser-port', '8771',
                               '--agent-alias-port', '8765']
    assert hub['network_mode'] == 'host'
    assert not {'build', 'entrypoint', 'container_name', 'ports', 'depends_on'} & hub.keys()
    assert hub['image'] == 'ghcr.io/ledgetoptechnologies/engineering-hub-control:latest'
    assert hub['pull_policy'] == 'always'
    assert hub['user'] == '568:568'
    assert hub['read_only'] is True and hub['cap_drop'] == ['ALL']
    assert hub['security_opt'] == ['no-new-privileges:true']
    assert hub['mem_limit'] == hub['memswap_limit'] == '512m'
    assert hub['cpus'] == 1.0 and hub['pids_limit'] == 128
    assert hub['tmpfs'] == ['/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777']


def test_preserves_both_operator_state_roots_and_never_autocreates_them():
    hub = service()
    assert [(v['source'], v['target']) for v in hub['volumes']] == [
        ('/mnt/Plugins/App_Data/Engineering-Hub/', '/var/lib/hub'),
        ('/mnt/Plugins/App_Data/Agent_MCP', '/var/lib/agent-message-bus')]
    assert all(v['type'] == 'bind' and v['bind']['create_host_path'] is False
               for v in hub['volumes'])
    env = hub['environment']
    assert env['HUB_DB'] == '/var/lib/hub/hub.sqlite3'
    assert env['AGENT_BUS_DB'] == '/var/lib/agent-message-bus/messages.sqlite3'
    assert env['AGENT_BUS_AUDIT'] == '/var/lib/agent-message-bus/audit.jsonl'
    assert env['AGENT_BUS_FILE_ROOT'] == '/var/lib/agent-message-bus/files'
    assert env['CLAIM_LEASE_SECONDS'] == '600'
    assert (env['ACK_RETENTION_DAYS'], env['REJECT_RETENTION_DAYS']) == ('30', '90')
    assert env['HUB_BUS_STARTUP_CLEANUP'] == '0'


def test_existing_protected_tokens_remain_placeholders_not_chat_credentials():
    env = service()['environment']
    for name in ('HERMES', 'CODEX', 'CLAUDE', 'ADMIN'):
        assert env[name + '_TOKEN'] == 'REPLACE_WITH_EXISTING_' + name + '_TOKEN'
    assert env['AGENT_BUS_ALLOWED_IPS'] == '192.168.60.92,192.168.20.151'
    assert not {'env_file', 'secrets'} & service().keys()


def test_closed_worker_defaults_and_separate_approved_cloudflare_identity(monkeypatch):
    env = service()['environment']
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match='placeholders'):
        Settings.from_env()
    for name in ('HERMES', 'CODEX', 'CLAUDE', 'ADMIN'):
        monkeypatch.setenv(name + '_TOKEN', 'synthetic-compose-' + name.lower())
    settings = Settings.from_env()
    assert settings.work_queue_enabled is settings.registry_enabled is False
    assert settings.auth_config.origin == 'https://hub.ledgetoptechnologies.com'
    assert settings.auth_config.human_auth_mode == 'cloudflare_access'
    assert settings.auth_config.cloudflare_config is not None
    assert settings.auth_config.local_session_ttl_seconds == 30 * 24 * 60 * 60
    assert {'192.168.50.80:8765', '192.168.50.80:8770'} <= set(settings.agent_allowed_hosts)
    assert settings.agent_auth.allowed_ips == frozenset({'192.168.60.92', '192.168.20.151'})


def test_health_covers_all_three_sockets_and_stop_grace_is_not_shortened():
    hub = service()
    assert hub['healthcheck']['test'][:3] == ['CMD', 'python', '-c']
    assert '(8765,8770,8771)' in hub['healthcheck']['test'][3]
    assert hub['healthcheck']['timeout'] == '12s'
    assert hub['stop_grace_period'] == '1200s'
    assert hub['environment']['HUB_SUPERVISOR_STOP_GRACE_SECONDS'] == '1200'
