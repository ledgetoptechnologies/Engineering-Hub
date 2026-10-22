"""Actual knowledge persistence + advisory reader, no registry or host access."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from agent_message_bus.knowledge import KnowledgeRepository
from engineering_hub import image_version_reports as reports

NOW = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)
DIGEST = 'sha256:' + 'a' * 64
OTHER = 'sha256:' + 'b' * 64


def report(different=False):
    identity = {'repository': 'ghcr.io/ledgetoptechnologies/engineering-hub-control',
                'platform': {'os': 'linux', 'architecture': 'amd64'},
                'config_digest': DIGEST}
    return {'schema_version': 1, 'items': [{
        'target': {'project': 'engineering-hub', 'host': 'nas', 'service': 'hub',
                   'repository': identity['repository'], 'reference': 'latest'},
        'running': {'identity': identity, 'source': 'runtime',
                    'observed_at': NOW.isoformat(),
                    'valid_until': (NOW + timedelta(minutes=10)).isoformat()},
        'published': {'identity': {**identity, 'config_digest': OTHER if different else DIGEST},
                      'source': 'registry', 'reference': 'latest',
                      'observed_at': NOW.isoformat(),
                      'valid_until': (NOW + timedelta(minutes=10)).isoformat()},
    }]}


def save(repo, content, version=0):
    return repo.put(project=reports.REPORT_PROJECT, path=reports.REPORT_PATH,
                    title='Container version observations', content=content,
                    author='hermes', expected_version=version)


@pytest.mark.parametrize('different', [False, True])
def test_real_knowledge_report_comparison_and_expiry(tmp_path, different):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    entry = save(repo, json.dumps(report(different)))
    result = reports.image_versions_for(repo, now=NOW)
    assert result['configured'] and result['advisory_only']
    assert result['source'] == 'agent_observation_report'
    assert result['reported_by'] == 'hermes'
    assert result['report_updated_at'] == entry.updated_at
    assert result['items'][0]['state'] == ('update_available' if different else 'up_to_date')
    expired = reports.image_versions_for(repo, now=NOW + timedelta(minutes=11))
    assert expired['items'][0]['state'] == 'unknown'
    assert expired['items'][0]['reason'] == 'running_observation_expired'
    assert repo.get(reports.REPORT_PROJECT, reports.REPORT_PATH) == entry


def test_absent_and_unavailable_source_are_not_up_to_date(tmp_path):
    assert reports.image_versions_for(None)['reason'] == 'not_configured'
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    assert reports.image_versions_for(repo)['reason'] == 'not_configured'
    class Failed:
        def get(self, *_):
            raise RuntimeError('private failure payload must not escape')
    result = reports.image_versions_for(Failed())
    assert result['reason'] == 'source_unavailable' and result['items'] == []
    assert 'private failure' not in json.dumps(result)


@pytest.mark.parametrize('content', [
    'not json private-token-marker',
    '{"schema_version":1,"schema_version":1,"items":[]}',
    '{"schema_version":true,"items":[]}',
    '{"schema_version":1,"items":[],"token":"private-token-marker"}',
    '{"schema_version":1,"items":[],"extra":NaN}',
    '{"schema_version":2,"items":[]}',
    '{"schema_version":1,"items":[]}',
    json.dumps({'schema_version': 1, 'items': report()['items'] * 2}),
    json.dumps({'schema_version': 1, 'items': report()['items'] * 33}),
])
def test_invalid_report_is_sanitized_and_does_not_become_fresh(tmp_path, content):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    save(repo, content)
    result = reports.image_versions_for(repo, now=NOW)
    assert result['configured'] and result['reason'] == 'invalid_report'
    assert result['items'] == [] and result['reported_by'] is None
    assert 'private-token-marker' not in json.dumps(result)


def test_oversized_and_deep_reports_fail_closed_without_echo():
    for content in (' ' * (reports.MAX_REPORT_BYTES + 1), '[' * 5000):
        source = SimpleNamespace(get=lambda *_: SimpleNamespace(content=content))
        assert reports.image_versions_for(source)['reason'] == 'invalid_report'


def test_reload_uses_current_record_not_previous_comparison(tmp_path):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    entry = save(repo, json.dumps(report(False)))
    assert reports.image_versions_for(repo, now=NOW)['items'][0]['state'] == 'up_to_date'
    save(repo, json.dumps(report(True)), entry.version)
    assert reports.image_versions_for(repo, now=NOW)['items'][0]['state'] == 'update_available'


def test_configured_report_through_real_rest_response_validation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_message_bus.auth import AuthSettings
    from engineering_hub import app as application
    from engineering_hub.auth import AuthConfig
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    save(repo, json.dumps(report(True)))
    monkeypatch.setattr(application, 'image_versions_for',
                        lambda _: reports.image_versions_for(repo, now=NOW))
    app = application.create_app(application.Settings(tmp_path / 'hub.sqlite3',
        AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
        AuthConfig('https://hub.test', beau_github_id=42)))
    with TestClient(app, base_url='https://hub.test') as client:
        headers = {'Authorization': 'Bearer test-codex'}
        for path in ('/api/hub/v1/overview', '/api/hub/v1/image-versions'):
            response = client.get(path, headers=headers)
            assert response.status_code == 200
            result = response.json()
            if path.endswith('/overview'):
                result = result['image_versions']
            assert result['items'][0]['state'] == 'update_available'
            assert isinstance(result['items'][0]['checked_at'], str)
            assert result['reported_by'] == 'hermes'
