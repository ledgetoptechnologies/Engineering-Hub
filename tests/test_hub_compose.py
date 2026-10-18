from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def service():
    config = yaml.safe_load((ROOT / 'compose.hub-control.yaml').read_text())
    assert config['name'] == 'engineering-hub-control'
    assert set(config['services']) == {'engineering-hub-control'}
    return config['services']['engineering-hub-control']


def test_standalone_main_moving_tag_and_owner_host_network_policy():
    hub = service()
    assert 'profiles' not in hub
    assert hub['image'] == 'ghcr.io/ledgetoptechnologies/engineering-hub-control:latest'
    assert 'build' not in hub and 'ports' not in hub
    assert hub['network_mode'] == 'host'
    assert hub['restart'] == 'unless-stopped'
    assert hub['pull_policy'] == 'always'
    # Production bus resources must not be renamed or merged into this profile.
    assert 'container_name' not in hub


def test_nonroot_readonly_bounded_runtime_without_host_privileges():
    hub = service()
    assert hub['user'] == '568:568'
    assert hub['read_only'] is True
    assert hub['cap_drop'] == ['ALL']
    assert hub['security_opt'] == ['no-new-privileges:true']
    assert hub['pids_limit'] == 128
    assert hub['cpus'] == 1.0
    assert hub['mem_limit'] == hub['memswap_limit'] == '512m'
    assert hub['tmpfs'] == ['/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777']
    assert not {'privileged', 'devices', 'cap_add', 'pid', 'ipc'} & hub.keys()


def test_declared_stop_grace_covers_owned_pilot_contract_without_activation():
    from engineering_hub.pilot_service import REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
    hub = service()
    declared = int(hub['environment']['HUB_SUPERVISOR_STOP_GRACE_SECONDS'])
    assert hub['stop_grace_period'] == f'{declared}s'
    assert declared >= REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS + 30
    assert hub['command'] == ['--split-ingress', '--browser-port', '8771']
    assert 'entrypoint' not in hub


def test_only_dedicated_operator_state_bind_no_credential_files_or_source():
    hub = service()
    assert len(hub['volumes']) == 1
    mount = hub['volumes'][0]
    assert mount['type'] == 'bind'
    assert mount['source'] == '/mnt/Plugins/App_Data/Agent_MCP/engineering-hub'
    assert mount['target'] == '/var/lib/hub'
    assert mount['bind']['create_host_path'] is False
    assert mount.get('read_only', False) is False
    assert not {'env_file', 'secrets', 'configs'} & hub.keys()
    env = hub['environment']
    for identity in ('HERMES', 'CODEX', 'CLAUDE'):
        assert env[f'{identity}_TOKEN'] == f'REPLACE_WITH_EXISTING_{identity}_TOKEN'
    assert env['HUB_DB'] == '/var/lib/hub/hub.sqlite3'
    assert not {'ADMIN_TOKEN', 'AGENT_BUS_DB', 'AGENT_BUS_PORT'} & env.keys()
    for optional in ('HUB_GITHUB_CLIENT_ID', 'HUB_GITHUB_CLIENT_SECRET', 'HUB_BEAU_GITHUB_ID'):
        assert env[optional] == ''
    assert env['HUB_BASE_URL'] == 'https://hub.ledgetoptechnologies.com'
    assert env['HUB_ALTERNATE_ORIGINS'] == ''
    assert env['HUB_HUMAN_AUTH_MODE'] == 'cloudflare_access'
    assert env['HUB_INGRESS_MODE'] == 'combined'  # One core, outer per-socket guards.
    assert env['HUB_AGENT_ALLOWED_HOSTS'] == '192.168.50.80:8770,127.0.0.1:8770'
    assert env['HUB_CF_ACCESS_ISSUER'] == 'https://ledgetoptechnologies.cloudflareaccess.com'
    assert len(env['HUB_CF_ACCESS_AUDIENCE']) == 64
    assert set(env['HUB_CF_ACCESS_ALLOWED_EMAILS'].split(',')) == {
        'bkoltz@ledgetoptechnologies.com', 'beaukoltz@ledgetopdroneservices.com'}


def test_image_entrypoint_and_health_are_the_separate_control_service():
    hub = service()
    dockerfile = (ROOT / 'docker' / 'Dockerfile.hub-control').read_text()
    assert 'USER 568:568' in dockerfile
    assert '"engineering_hub"' in dockerfile and '"8770"' in dockerfile
    assert 'entrypoint' not in hub
    assert hub['command'] == ['--split-ingress', '--browser-port', '8771']
    assert hub['healthcheck']['test'][:3] == ['CMD', 'python', '-c']
    assert '127.0.0.1:' in hub['healthcheck']['test'][3]
    assert '(8770,8771)' in hub['healthcheck']['test'][3]


def test_documented_operator_gates_not_a_deployment_or_egress_claim():
    doc = (ROOT / 'docs' / 'hub-control-deployment.md').read_text()
    for requirement in ('does not enforce immutable', 'not egress-isolated',
                        'callback query strings', 'exact numeric GitHub',
                        'not auto-create', 'No canonical bus topic-registry',
                        'SQLite backup utility', 'port 8765'):
        assert requirement in doc
