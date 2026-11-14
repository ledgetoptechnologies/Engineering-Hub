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


def test_review_asset_contract_describes_complete_and_range_bytes():
    from engineering_hub.interactive_service import TYPES

    contract = json.loads((Path(__file__).parents[1] / 'contracts/hub-v1.openapi.json').read_text())
    operation = contract['paths']['/api/hub/v1/interactive-reviews/{review_id}/assets/{asset_id}']['get']
    for code in ('200', '206'):
        content = operation['responses'][code]['content']
        assert set(content) == TYPES
        assert all(value['schema'] == {'type': 'string', 'format': 'binary'} for value in content.values())


def test_manual_json_operations_publish_exact_required_body_schema():
    from engineering_hub.openapi_body import request_body
    from engineering_hub.interactive_reviews_api import ReviewCreate, ReviewAnswers
    from engineering_hub.infrastructure_api import DeviceInput, MaintenanceInput, ResolutionInput
    from engineering_hub.staging_api import (StagingKeep, StagingCleanupPlan, StagingCleanupApply,
        StagingSubmission, ClosedInput, StagingPolicy)
    from engineering_hub.shared_staging_api import _Recover
    operations = {
        ('post', '/api/hub/v1/interactive-reviews'): ReviewCreate,
        ('post', '/api/hub/v1/interactive-reviews/{review_id}/answers'): ReviewAnswers,
        ('put', '/api/hub/v1/infrastructure/devices/{device_id}'): DeviceInput,
        ('post', '/api/hub/v1/infrastructure/devices/{device_id}/maintenance'): MaintenanceInput,
        ('post', '/api/hub/v1/infrastructure/maintenance/{maintenance_id}/resolve'): ResolutionInput,
        ('post', '/api/hub/v1/staging/resources/{resource_id}/keep'): StagingKeep,
        ('post', '/api/hub/v1/staging/cleanup/plan'): StagingCleanupPlan,
        ('post', '/api/hub/v1/staging/cleanup/apply'): StagingCleanupApply,
        ('post', '/api/hub/v1/staging/jobs'): StagingSubmission,
        ('post', '/api/hub/v1/staging/jobs/{job_id}/reconcile'): ClosedInput,
        ('put', '/api/hub/v1/staging/projects/{project}/policy'): StagingPolicy,
        ('post', '/api/hub/v1/staging/resources/{resource_id}/preview-session'): ClosedInput,
        ('post', '/api/hub/v1/staging/shared/recover'): _Recover,
    }
    contract = json.loads((Path(__file__).parents[1] / 'contracts/hub-v1.openapi.json').read_text())
    for (method, path), model in operations.items():
        body = contract['paths'][path][method]['requestBody']
        actual = body['content']['application/json']['schema']
        assert actual['title'] == model.model_json_schema()['title']
        assert actual.get('required', []) == model.model_json_schema().get('required', [])
        assert set(actual['properties']) == set(model.model_fields) or set(actual['properties']) == {field.alias or name for name, field in model.model_fields.items()}
        assert body['required'] is True
        assert '$ref' not in json.dumps(body)  # inlined definitions resolve without dangling refs
    review = contract['paths']['/api/hub/v1/interactive-reviews']['post']['requestBody']
    properties = review['content']['application/json']['schema']['properties']
    assert properties['items']['items']['properties']['choices']['anyOf'][0]['items']['properties']['id']
    review_schema = review['content']['application/json']['schema']
    assert {'project', 'component_id', 'component_revision', 'topic', 'dataset_digest', 'title', 'items', 'client_request_id'} <= set(review_schema['required'])
    assert 'schema' in review_schema['properties'] and 'schema_name' not in review_schema['properties']
    assert review_schema['additionalProperties'] is False
    submission = contract['paths']['/api/hub/v1/staging/jobs']['post']['requestBody']['content']['application/json']['schema']
    assert {'request_id', 'review_task_id', 'review_task_revision', 'project', 'recipe', 'source_commit', 'source_digest', 'source_zip_base64', 'source_commit_object_base64'} <= set(submission['required'])
    assert submission['additionalProperties'] is False
    empty = contract['paths']['/api/hub/v1/staging/shared/recover']['post']['requestBody']['content']['application/json']['schema']
    assert empty['properties'] == {} and empty['additionalProperties'] is False


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
