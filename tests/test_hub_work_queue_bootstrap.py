"""First-version app wiring, not proof of a deployed runner."""
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.repository import HubRepository


def settings(tmp_path, **kwargs):
    return Settings(tmp_path / 'hub.sqlite3',
                    AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
                    AuthConfig('https://hub.invalid'), **kwargs)


def reviewed(repo):
    task = repo.create_task('human:42', 'engineering-hub', 'Synthetic reviewed work', str(uuid4()))
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], 'a' * 40)
    return repo.review_task('claude', task['id'], task['revision'], 'a' * 40, 'approve')


def test_default_queue_is_explicitly_unconfigured_and_still_authenticated(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        assert client.get('/api/hub/v1/work/status').status_code == 401
        assert client.get('/api/hub/v1/work/status', headers={'Authorization': 'Bearer test-codex'}).status_code == 423
    assert app.state.work_queue is None


def test_opt_in_first_version_connects_and_claims_only_verified_staging_work(tmp_path):
    app = create_app(settings(tmp_path, work_queue_enabled=True))
    task = reviewed(app.state.repository)
    headers = {'Authorization': 'Bearer test-codex'}
    target = {'task_id': task['id'], 'source_commit': 'a' * 40, 'artifact_digest': 'sha256:' + 'b' * 64}
    with TestClient(app) as client:
        state = client.get('/api/hub/v1/work/status', headers=headers)
        assert state.status_code == 200 and state.json()['jobs'] == []
        production = client.post('/api/hub/v1/work/jobs', headers=headers, json={
            'request_id': 'prod-test', 'action_key': 'engineering-hub.validate',
            'environment': 'production', 'target': target})
        assert production.status_code == 201
        staging = client.post('/api/hub/v1/work/jobs', headers=headers, json={
            'request_id': 'stage-test', 'action_key': 'engineering-hub.validate',
            'environment': 'staging', 'target': target})
        assert staging.status_code == 201
        instance = str(uuid4())
        result = client.post('/api/hub/v1/work/claim', headers=headers,
                             json={'instance_id': instance, 'lease_seconds': 60})
        assert result.status_code == 200
        job = result.json()['job']
        assert job['id'] == staging.json()['id']  # Unapproved production is ineligible.
        refused = client.post(f"/api/hub/v1/work/jobs/{job['id']}/complete", headers=headers,
                              json={'instance_id': instance, 'fence': job['fence'], 'outcome': 'succeeded'})
        assert refused.status_code == 409  # No real execution proof provider wired.
        assert app.state.repository.get_task(task['id'])['status'] == 'verified'


def test_new_queue_cannot_bypass_legacy_dispatch_machine_exclusion(tmp_path):
    with pytest.raises(ValueError, match='shared reservation bridge'):
        create_app(settings(tmp_path, work_queue_enabled=True), staging_dispatcher=SimpleNamespace())


def test_real_app_passes_configured_queue_to_authenticated_mcp(tmp_path):
    app = create_app(settings(tmp_path, work_queue_enabled=True))
    headers = {'Authorization': 'Bearer test-codex',
               'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': '2025-06-18'}
    with TestClient(app, base_url='http://hub.invalid') as client:
        response = client.post('/mcp/', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
        assert response.status_code == 200
        tools = {tool['name'] for tool in response.json()['result']['tools']}
        assert len(tools) == 16 and 'hub_work_status' in tools
        response = client.post('/mcp/', headers=headers, json={
            'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': 'hub_work_status', 'arguments': {'payload': {}}}})
        result = response.json()['result']
        assert not result.get('isError')
        data = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        assert 'engineering-hub.validate' in data['actions']


def test_queue_enable_flag_requires_explicit_boolean_or_environment_value(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='enable flag'):
        settings(tmp_path, work_queue_enabled=1)
    monkeypatch.setenv('HUB_WORK_QUEUE_ENABLED', 'yes')
    with pytest.raises(ValueError, match='must be 0 or 1'):
        Settings.from_env()
