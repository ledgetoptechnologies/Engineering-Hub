"""Protected-layout selection stays explicit; no live state or secret reads."""
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from engineering_hub.deployment_config import load_deployment_config
from tests.test_hub_deployment_config import valid, write


def document():
    value = valid()
    value.update(schema_version=2, storage={'layout': 'protected-ledger-v1'})
    return value


def test_v2_fixed_private_database_preserves_shared_bus_and_v1(tmp_path, monkeypatch):
    from engineering_hub import protected_layout as layout
    monkeypatch.setattr(layout, 'validate_existing_boundary', lambda *_a, **_k: pytest.fail('metadata read'))
    old = load_deployment_config(write(tmp_path, valid()))
    new = load_deployment_config(write(tmp_path, document()))
    assert old.protected_ledger is False
    assert old.hub_database == tmp_path / 'hub.sqlite3'
    assert new.protected_ledger is True
    assert new.hub_database == Path('/var/lib/hub-private/hub.sqlite3')
    assert old.bus_paths == new.bus_paths
    old.validate_ledger_boundary()  # v1 has no new mount requirement
    assert list(tmp_path.iterdir()) == [tmp_path / 'settings.json']


@pytest.mark.parametrize('storage', [None, [], {}, {'layout': 'other'},
    {'layout': True}, {'layout': 'protected-ledger-v1', 'database': '/elsewhere'},
    {'layout': 'protected-ledger-v1', 'create': True}])
def test_v2_closed_storage_contract(tmp_path, storage):
    value = document()
    value['storage'] = storage
    with pytest.raises(ValueError, match='^Invalid Hub deployment configuration$'):
        load_deployment_config(write(tmp_path, value))


def test_config_cannot_live_inside_private_mount(tmp_path, monkeypatch):
    from engineering_hub import protected_layout as layout
    monkeypatch.setattr(layout, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='^Invalid Hub deployment configuration$'):
        load_deployment_config(write(tmp_path, document()))


def test_metadata_gate_receives_existing_shared_roles(tmp_path, monkeypatch):
    from engineering_hub import protected_layout as layout
    deployment = load_deployment_config(write(tmp_path, document()))
    calls = []
    monkeypatch.setattr(layout, 'validate_existing_boundary', lambda *a, **k: calls.append((a, k)))
    deployment.validate_ledger_boundary()
    assert calls == [((deployment.hub_database,), {
        'shared_root': tmp_path, 'shared_paths': tuple(deployment.bus_paths.values())})]


def settings(tmp_path):
    from engineering_hub.app import Settings
    from agent_message_bus.auth import AuthSettings
    deployment = load_deployment_config(write(tmp_path, document()))
    return Settings(deployment.hub_database, AuthSettings('test-hermes', 'test-codex'),
                    deployment.auth_config, require_owned_ledger=True)


def test_unowned_factories_refuse_before_any_database_or_bus(tmp_path, monkeypatch):
    from engineering_hub import app, unified_app
    selected = settings(tmp_path)
    monkeypatch.setattr(app, 'HubRepository', lambda *_a, **_k: pytest.fail('database opened'))
    monkeypatch.setattr(unified_app, '_create_unified_bus', lambda *_a, **_k: pytest.fail('bus opened'))
    with pytest.raises(ValueError, match='explicit existing owner'):
        app.create_app(selected)
    with pytest.raises(ValueError, match='explicit owned entrypoint'):
        unified_app.create_unified_app(selected)
    assert not (tmp_path / 'hub.sqlite3').exists()


def test_protected_settings_reject_other_database_path(tmp_path):
    with pytest.raises(ValueError, match='fixed ledger path'):
        replace(settings(tmp_path), database=tmp_path / 'hub.sqlite3')


def test_default_cli_refuses_v2_before_socket_or_runtime(tmp_path, monkeypatch, capsys):
    from engineering_hub import __main__ as entry
    path = write(tmp_path, document())
    monkeypatch.setattr(sys, 'argv', ['engineering_hub', '--config', str(path)])
    monkeypatch.setattr(entry.asyncio, 'run', lambda *_a, **_k: pytest.fail('runtime started'))
    with pytest.raises(SystemExit) as caught:
        entry.main()
    assert caught.value.code == 2
    assert 'explicit owned entrypoint' in capsys.readouterr().err


@pytest.mark.asyncio
async def test_owned_selector_denies_boundary_before_settings_or_service(tmp_path, monkeypatch):
    from engineering_hub import owned_inactive_hub as service, protected_layout as layout
    path = write(tmp_path, document())
    def reject(*_a, **_k):
        raise layout.ProtectedLayoutError()
    monkeypatch.setattr(layout, 'validate_existing_boundary', reject)
    monkeypatch.setattr(service.Settings, 'from_env', lambda *_a, **_k: pytest.fail('settings read'))
    with pytest.raises(layout.ProtectedLayoutError):
        await service.serve_configuration(path)


def test_pilot_selector_denies_boundary_before_profile_read(tmp_path, monkeypatch):
    from engineering_hub import unified_pilot_startup as service, protected_layout as layout
    path = write(tmp_path, document())
    def reject(*_a, **_k):
        raise layout.ProtectedLayoutError()
    monkeypatch.setattr(layout, 'validate_existing_boundary', reject)
    monkeypatch.setattr(service, 'load_profile', lambda *_a: pytest.fail('profile read'))
    with pytest.raises(service.UnifiedPilotStartupError):
        service.validate_configuration(path, '/var/lib/hub-private/pilot.json')


@pytest.mark.parametrize('role', ['profile', 'credential', 'ca'])
def test_v2_pilot_names_are_fixed_before_secret_resolution(tmp_path, monkeypatch, role):
    from engineering_hub import unified_pilot_startup as service, protected_layout as layout
    from tests.test_hub_unified_pilot_startup import profile_document, encode
    path = write(tmp_path, document())
    monkeypatch.setattr(layout, 'validate_existing_boundary', lambda *_a, **_k: None)
    data = profile_document()
    data['executor']['credential_file'] = '/var/lib/hub-private/executor.token'
    data['executor']['ca_file'] = '/var/lib/hub-private/staging-ca.pem'
    profile = '/var/lib/hub-private/pilot.json'
    if role == 'profile':
        profile = '/other/pilot.json'
        monkeypatch.setattr(service, 'load_profile', lambda *_a: pytest.fail('wrong profile read'))
    else:
        data['executor']['credential_file' if role == 'credential' else 'ca_file'] = '/other/value'
        monkeypatch.setattr(service, 'load_profile', lambda *_a: service.parse_profile(encode(data)))
    with pytest.raises(service.UnifiedPilotStartupError):
        service.validate_configuration(path, profile)


def test_v2_profile_fixed_paths_accept_without_credential_read(tmp_path, monkeypatch):
    from engineering_hub import unified_pilot_startup as service, protected_layout as layout
    from tests.test_hub_unified_pilot_startup import profile_document, encode
    path = write(tmp_path, document())
    monkeypatch.setattr(layout, 'validate_existing_boundary', lambda *_a, **_k: None)
    data = profile_document()
    data['executor']['credential_file'] = '/var/lib/hub-private/executor.token'
    data['executor']['ca_file'] = '/var/lib/hub-private/staging-ca.pem'
    monkeypatch.setattr(service, 'load_profile', lambda *_a: service.parse_profile(encode(data)))
    deployment, profile = service.validate_configuration(path, '/var/lib/hub-private/pilot.json')
    assert profile.credential_file.parent == deployment.hub_database.parent
