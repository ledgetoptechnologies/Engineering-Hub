"""The published dashboard contract must match the actual route schema."""
import json
from pathlib import Path
import subprocess
import sys

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


def test_metric_history_contract_declares_required_query_parameters():
    contract = json.loads((Path(__file__).parents[1] / 'contracts/hub-v1.openapi.json').read_text())
    operation = contract['paths']['/api/hub/v1/infrastructure/metrics/history']['get']
    params = {parameter['name']: parameter for parameter in operation['parameters']}
    assert set(params) == {'host', 'range'}
    assert all(item['in'] == 'query' and item['required'] for item in params.values())
    assert params['host']['schema']['pattern'] == r'^(?:host|target):[0-9a-f]{64}$'
    assert params['range']['schema']['enum'] == ['1h', '6h', '24h', '7d']


def test_infrastructure_devices_contract_declares_retired_filter():
    contract = json.loads((Path(__file__).parents[1] / 'contracts/hub-v1.openapi.json').read_text())
    params = contract['paths']['/api/hub/v1/infrastructure/devices']['get']['parameters']
    assert params == [{'name': 'include_retired', 'in': 'query', 'required': False,
                       'description': 'Include retired devices when true.',
                       'schema': {'type': 'boolean', 'default': False}}]


def test_contract_survives_typing_cache_eviction_and_equivalent_literal_order(tmp_path):
    # Project routes import lazily. Earlier tests can evict typing's caches;
    # equal Literal unions may then retain a different insertion order. Reuse
    # the canonical nullable alias rather than depending on a warm cache.
    script = '''
import json, sys, typing
from pathlib import Path
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from agent_message_bus.auth import AuthSettings
for cleanup in typing._cleanups:
    cleanup()
seed = typing.Literal['codex', 'hermes', 'claude'] | None
app = create_app(Settings(Path(sys.argv[1]),
    AuthSettings('contract-fixture-hermes', 'contract-fixture-codex'),
    AuthConfig('https://hub.invalid')))
assert app.openapi() == json.loads(Path(sys.argv[2]).read_text())
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'cold.db'),
        str(Path(__file__).parents[1] / 'contracts/hub-v1.openapi.json')],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
