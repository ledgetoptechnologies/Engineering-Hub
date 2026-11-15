"""Canonical closed preparation only; no sockets, worker jobs or host effects."""
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from engineering_hub.repository import HubError, HubRepository
from engineering_hub.staging_client import StagingClient, StagingConnectionConfig, load_optional_config
from engineering_hub.staging_module import (SharedStagingPreparationError, compose_staging,
    configured_staging_actions, prepare_shared_staging)
from engineering_hub.work_queue import ClosedClaimPolicy, WorkAction


def config_file(tmp_path, *, enabled=True, projects=None):
    token = tmp_path / 'synthetic-token'
    token.write_text('synthetic-preparation-token-never-live')
    token.chmod(0o600)
    path = tmp_path / 'staging.json'
    path.write_text(json.dumps({'schema': 1, 'origin': 'http://127.0.0.1:8780',
        'credential_file': str(token), 'enabled': enabled, 'auto_run': False,
        'projects': {'engineering-hub': {'recipes': ['static', 'ci']}} if projects is None else projects}))
    path.chmod(0o600)
    return path


@pytest.fixture
def prepared(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db', execution_principal='executor:container-pilot')
    path = config_file(tmp_path)
    requests, clients = [], []
    def factory(config):
        def request(value):
            requests.append(value)
            raise AssertionError('preparation must not make requests')
        client = StagingClient(config, test_transport=httpx.MockTransport(request))
        clients.append(client)
        return client
    return repo, path, requests, clients, factory


def pilot_actions():
    return {'pilot.test': WorkAction('staging-pc', project='engineering-hub')}


def prepare(value, **changes):
    repo, path, _, _, factory = value
    options = dict(path=path, machine_id='staging-pc', pilot_actions=pilot_actions(),
                   target_verifier=lambda *args: False, client_factory=factory)
    options.update(changes)
    return prepare_shared_staging(repo, **options)


def test_pure_action_derivation_preserves_full_historical_key_algorithm(tmp_path):
    config, _ = load_optional_config(config_file(tmp_path))
    registry, mapping = configured_staging_actions(config, machine_id='staging-pc')
    for recipe in ('static', 'ci'):
        expected = 'staging-service.' + hashlib.sha256(json.dumps(['engineering-hub', recipe], separators=(',', ':')).encode()).hexdigest()[:24]
        assert mapping['engineering-hub', recipe] == expected
        assert registry[expected] == WorkAction('staging-pc', project='engineering-hub')
    assert len(registry) == 2


@pytest.mark.asyncio
async def test_shared_preparation_has_one_complete_uninstalled_queue_and_no_grants_or_effects(prepared):
    repo, _, requests, clients, _ = prepared
    draft = prepare(prepared)
    controller = draft.module.controller
    try:
        assert draft.queue.repo is repo and controller.repo is repo and controller.queue is draft.queue
        assert draft.policies.repo is repo and draft.policies.queue is draft.queue
        assert type(draft.queue.claim_verifier) is ClosedClaimPolicy
        assert len(draft.queue.actions) == 3 and set(controller.actions.values()) < set(draft.queue.actions)
        assert draft.policies.assert_pending_binding('modular', controller._policy_binding) is controller._policy_binding
        assert not hasattr(draft.queue, '_shared_execution_policies')
        assert not draft.policies._installed and not draft.policies._open and not draft.policies._ready
        assert controller.background is None and controller.inflight == set()
        assert draft.module.client is controller.client is clients[0]
        assert controller.resources.repo is repo and controller.resources._control is controller.principal
        assert repo.execution_principal == 'executor:container-pilot'
        with pytest.raises(HubError):
            controller.resources.authorize('codex', 'engineering-hub', 'build')
        assert requests == [] and not repo.list_runs()
        with repo._connection() as db:
            assert db.execute('SELECT count(*) FROM hub_work_jobs').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM hub_staging_submissions').fetchone()[0] == 0
    finally:
        await draft.module.client.aclose()


@pytest.mark.asyncio
async def test_standalone_composition_still_uses_identical_keys_and_lenient_mode(prepared):
    repo, path, requests, clients, factory = prepared
    config, _ = load_optional_config(path)
    expected, mapping = configured_staging_actions(config, machine_id='staging-pc')
    module = compose_staging(repo, path=path, machine_id='staging-pc', client_factory=factory)
    shared = None
    try:
        shared = prepare(prepared)
        assert module.status == {'state': 'configured', 'enabled': True}
        assert module.controller.queue.actions == expected and module.controller.actions == mapping
        assert shared.module.controller.actions == module.controller.actions
        assert {k: shared.queue.actions[k] for k in expected} == module.controller.queue.actions
        assert module.controller.execution_policies is None
        assert not hasattr(module.controller.queue, '_shared_execution_policies')
        assert requests == []
    finally:
        if shared is not None:
            await shared.module.client.aclose()
        await module.client.aclose()


def test_only_missing_or_explicit_disabled_configuration_returns_none(tmp_path):
    repo = HubRepository(tmp_path / 'hub.db')
    options = dict(machine_id='staging-pc', pilot_actions=pilot_actions(),
                   target_verifier=lambda *args: False)
    assert prepare_shared_staging(repo, path=tmp_path / 'missing.json', **options) is None
    assert prepare_shared_staging(repo, path=config_file(tmp_path, enabled=False), **options) is None
    path = tmp_path / 'invalid.json'
    path.write_text('{"schema":1,"PRIVATE":"redacted"}')
    with pytest.raises(SharedStagingPreparationError) as error:
        prepare_shared_staging(repo, path=path, **options)
    assert error.value.draft is None and 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('kind', ['no_profiles', 'wrong_machine', 'wrong_environment', 'no_proof',
                                  'wrong_key', 'empty_pilot', 'overlap', 'queue_full'])
def test_invalid_enabled_profiles_and_action_maps_fail_before_client(prepared, kind):
    _, path, requests, clients, _ = prepared
    options = {}
    if kind == 'no_profiles':
        data = json.loads(path.read_text()); data['projects'] = {}; path.write_text(json.dumps(data))
    elif kind == 'wrong_machine':
        options['pilot_actions'] = {'pilot': WorkAction('foreign', project='engineering-hub')}
    elif kind == 'wrong_environment':
        options['pilot_actions'] = {'pilot': WorkAction('staging-pc', frozenset({'production'}), project='engineering-hub')}
    elif kind == 'no_proof':
        options['pilot_actions'] = {'pilot': WorkAction('staging-pc', project='engineering-hub', requires_execution_proof=False)}
    elif kind == 'wrong_key':
        options['pilot_actions'] = {'bad key': WorkAction('staging-pc', project='engineering-hub')}
    elif kind == 'empty_pilot':
        options['pilot_actions'] = {}
    elif kind == 'overlap':
        config, _ = load_optional_config(path)
        action_map, _ = configured_staging_actions(config, machine_id='staging-pc')
        options['pilot_actions'] = {next(iter(action_map)): WorkAction('staging-pc', project='engineering-hub')}
    else:
        options['pilot_actions'] = {f'pilot.{n}': WorkAction('staging-pc', project='engineering-hub') for n in range(255)}
    with pytest.raises(SharedStagingPreparationError) as error:
        prepare(prepared, **options)
    assert error.value.draft is None and clients == [] and requests == []


@pytest.mark.parametrize('projects', [
    {'engineering-hub': {'recipes': ['bad/recipe']}},
    {'engineering-hub': {'recipes': ['same', 'same']}},
    {'engineering-hub': {'recipes': []}},
    {'engineering-hub': {'recipes': ['valid'], 'unexpected': True}},
])
def test_pure_derivation_revalidates_frozen_input_metadata(tmp_path, projects):
    config, _ = load_optional_config(config_file(tmp_path))
    object.__setattr__(config, 'projects', projects)
    with pytest.raises(ValueError):
        configured_staging_actions(config, machine_id='staging-pc')


@pytest.mark.asyncio
async def test_failure_after_client_acquisition_retains_exact_client_for_caller_cleanup(prepared, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError('PRIVATE constructor exception must not escape')
    monkeypatch.setattr('engineering_hub.staging_controller.StagingController', failed)
    with pytest.raises(SharedStagingPreparationError) as error:
        prepare(prepared)
    draft = error.value.draft
    assert draft is not None and draft.module.client is prepared[3][0]
    assert not draft.module.client._client.is_closed
    assert not draft.policies._installed and not draft.policies._open
    assert draft.module.controller is None and prepared[2] == []
    assert 'PRIVATE' not in str(error.value)
    await draft.module.client.aclose()
    assert draft.module.client._client.is_closed


def test_client_factory_failure_is_closed_and_has_no_silent_optional_fallback(prepared):
    def failed(config):
        raise RuntimeError('PRIVATE credential failure')
    with pytest.raises(SharedStagingPreparationError) as error:
        prepare(prepared, client_factory=failed)
    assert error.value.draft is not None and error.value.draft.module.client is None
    assert not error.value.draft.policies._installed
    assert 'PRIVATE' not in str(error.value) and prepared[2] == []


@pytest.mark.asyncio
async def test_exact_queue_capacity_boundary_is_allowed_but_stays_closed(prepared):
    actions = {f'pilot.{n}': WorkAction('staging-pc', project='engineering-hub') for n in range(254)}
    draft = prepare(prepared, pilot_actions=actions)
    try:
        assert len(draft.queue.actions) == 256
        assert not draft.policies._installed and not draft.policies._open
        assert prepared[2] == []
    finally:
        await draft.module.client.aclose()


def test_unknown_configured_project_refuses_before_client(prepared):
    path = prepared[1]
    data = json.loads(path.read_text())
    data['projects'] = {'unregistered-client': {'recipes': ['static']}}
    path.write_text(json.dumps(data))
    with pytest.raises(SharedStagingPreparationError):
        prepare(prepared)
    assert prepared[2] == [] and prepared[3] == []


@pytest.mark.parametrize('key', ['target_verifier', 'client_factory'])
def test_async_factories_or_verifiers_are_not_invoked(prepared, key):
    calls = []
    async def unsupported(*args):
        calls.append(True)
    with pytest.raises(SharedStagingPreparationError):
        prepare(prepared, **{key: unsupported})
    assert calls == [] and prepared[3] == []
