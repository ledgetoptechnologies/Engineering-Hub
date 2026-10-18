from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def service():
    config = yaml.safe_load((ROOT / 'compose.hub-control.yaml').read_text())
    assert config['name'] == 'engineering-hub-control'
    assert set(config['services']) == {'hub-control'}
    return config['services']['hub-control']


def test_standalone_opt_in_immutable_image_gate_and_loopback_only():
    hub = service()
    assert hub['profiles'] == ['hub-control']
    assert hub['image'].startswith('${HUB_CONTROL_IMAGE:?')
    assert 'build' not in hub and 'network_mode' not in hub
    assert hub['ports'] == ['127.0.0.1:8770:8770']
    assert hub['restart'] == 'unless-stopped'
    assert hub['pull_policy'] == 'missing'
    # Production bus resources must not be renamed or merged into this profile.
    assert 'container_name' not in hub


def test_nonroot_readonly_bounded_runtime_without_host_privileges():
    hub = service()
    assert hub['user'] == '10001:10001'
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
    assert declared >= REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS
    assert 'command' not in hub and 'entrypoint' not in hub


def test_only_dedicated_operator_state_bind_no_credential_files_or_source():
    hub = service()
    assert len(hub['volumes']) == 1
    mount = hub['volumes'][0]
    assert mount['type'] == 'bind'
    assert mount['source'].startswith('${HUB_STATE_DIRECTORY:?')
    assert mount['target'] == '/var/lib/hub'
    assert mount['bind']['create_host_path'] is False
    assert mount['read_only'] is False
    assert not {'env_file', 'secrets', 'configs'} & hub.keys()
    env = hub['environment']
    for identity in ('HERMES', 'CODEX', 'CLAUDE'):
        assert env[f'{identity}_TOKEN'].startswith('${' + identity + '_TOKEN:?')
    assert env['HUB_DB'] == '/var/lib/hub/hub.sqlite3'
    assert not {'ADMIN_TOKEN', 'AGENT_BUS_DB', 'AGENT_BUS_PORT'} & env.keys()
    for optional in ('HUB_GITHUB_CLIENT_ID', 'HUB_GITHUB_CLIENT_SECRET', 'HUB_BEAU_GITHUB_ID'):
        assert env[optional] == '${' + optional + ':-}'


def test_image_entrypoint_and_health_are_the_separate_control_service():
    hub = service()
    dockerfile = (ROOT / 'docker' / 'Dockerfile.hub-control').read_text()
    assert 'USER 10001:10001' in dockerfile
    assert '"engineering_hub"' in dockerfile and '"8770"' in dockerfile
    assert not {'command', 'entrypoint'} & hub.keys()
    assert hub['healthcheck']['test'][:3] == ['CMD', 'python', '-c']
    assert '127.0.0.1:8770/health' in hub['healthcheck']['test'][3]


def test_documented_operator_gates_not_a_deployment_or_egress_claim():
    doc = (ROOT / 'docs' / 'hub-control-deployment.md').read_text()
    for requirement in ('does not enforce immutable', 'not egress-isolated',
                        'callback query strings', 'exact numeric GitHub',
                        'not auto-create', 'No canonical bus topic-registry',
                        'SQLite backup utility', 'port 8765'):
        assert requirement in doc
