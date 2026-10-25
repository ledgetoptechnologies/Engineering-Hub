"""Inventory is bounded read-only observation, not a new control surface."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from engineering_hub.staging_client import StagingClient, StagingClientError, StagingConnectionConfig
from engineering_hub.staging_controller import StagingController
from engineering_hub.staging_runtime import validate_runtime_snapshot
from engineering_hub.repository import HubRepository, HubError


NOW = datetime.now(timezone.utc)


def snapshot(now=NOW):
    return {'schema': 'hub-staging-runtime.v1', 'observed_at': now.isoformat(),
        'sources': [{'id': 'worker', 'state': 'observed', 'observed_at': now.isoformat()},
                    {'id': 'host', 'state': 'not_configured', 'observed_at': None}],
        'containers': [{'id': 'a' * 64, 'name': 'example', 'image': 'example:local',
                        'state': 'running', 'scope': 'worker', 'managed': False}], 'services': []}


def test_runtime_snapshot_accepts_observation_without_granting_ownership():
    value = snapshot()
    assert validate_runtime_snapshot(value, now=NOW) == value
    value['sources'][1] = {'id': 'host', 'state': 'observed', 'observed_at': NOW.isoformat()}
    value['services'] = [{'name': 'viewer-staging.service', 'state': 'running', 'scope': 'host'}]
    assert validate_runtime_snapshot(value, now=NOW) == value


@pytest.mark.parametrize('mutate', [
    lambda v: v.update(token='secret'),
    lambda v: v.update(observed_at=(NOW - timedelta(seconds=91)).isoformat()),
    lambda v: v.update(observed_at=(NOW + timedelta(seconds=11)).isoformat()),
    lambda v: v.update(observed_at='2026-10-25'),
    lambda v: v['containers'][0].update(managed=True),
    lambda v: v['containers'][0].update(command='private argument'),
    lambda v: v['containers'][0].update(id='short'),
    lambda v: v['containers'][0].update(scope=[]),
    lambda v: v['containers'][0].update(state=[]),
    lambda v: v['sources'][0].update(state='stale'),
    lambda v: v['sources'][0].update(id=[]),
    lambda v: v['sources'][0].update(state=[]),
    lambda v: v['sources'].append(deepcopy(v['sources'][0])),
    lambda v: v['containers'].append(deepcopy(v['containers'][0])),
    lambda v: v.update(services=[{'name': 'x.service', 'state': 'running', 'scope': 'host'}]),
    lambda v: v['containers'][0].update(name='bad\nname'),
    lambda v: v['containers'][0].update(image='x' * 513),
])
def test_runtime_rejects_secrets_stale_rows_malformed_or_ambiguous_records(mutate):
    value = snapshot()
    mutate(value)
    with pytest.raises(ValueError, match='invalid_runtime_snapshot'):
        validate_runtime_snapshot(value, now=NOW)


@pytest.mark.asyncio
async def test_runtime_client_uses_only_fixed_authenticated_get_and_rejects_secrets(tmp_path):
    credential = tmp_path / 'token'
    credential.write_text('synthetic-inventory-token')
    credential.chmod(0o600)
    config = StagingConnectionConfig('http://127.0.0.1:8782', credential, enabled=True)
    calls = []
    # Collection can precede this test by minutes in the full release suite.
    # Produce the remote observation when this request test actually starts.
    response = snapshot(datetime.now(timezone.utc))

    def remote(request):
        calls.append(request)
        assert request.method == 'GET' and request.url.path == '/v1/runtime' and not request.content
        assert request.headers['authorization'] == 'Bearer synthetic-inventory-token'
        return httpx.Response(200, json=response)

    client = StagingClient(config, test_transport=httpx.MockTransport(remote))
    try:
        assert await client.runtime() == response
        response['containers'][0]['environment'] = {'PRIVATE': 'do not expose'}
        with pytest.raises(StagingClientError):
            await client.runtime()
        response.clear()
        response['padding'] = 'x' * (128 * 1024)
        with pytest.raises(StagingClientError):
            await client.runtime()
        assert len(calls) == 3
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_controller_runtime_checks_admin_before_network_and_hides_failure():
    controller = object.__new__(StagingController)
    controller.repo = HubRepository
    controller.resources = SimpleNamespace(admin_actors=frozenset({'human:1'}))
    calls = []

    async def remote():
        calls.append('GET')
        raise RuntimeError('private-host-and-token-must-not-escape')

    controller.client = SimpleNamespace(runtime=remote)
    for actor in ('codex', 'human:2'):
        with pytest.raises(HubError):
            await controller.runtime(actor)
    assert calls == []
    assert await controller.runtime('human:1') == {
        'configured': True, 'state': 'unavailable', 'snapshot': None}
    assert calls == ['GET']
