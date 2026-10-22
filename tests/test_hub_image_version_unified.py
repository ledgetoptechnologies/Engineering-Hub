"""Real unified bus knowledge -> MCP/REST status path, synthetic observations."""
from datetime import datetime, timedelta, timezone
import json
import sys

import pytest

if sys.platform != 'linux':
    pytest.skip('Unified bus requires Linux FileRepository', allow_module_level=True)

from fastapi.testclient import TestClient
from engineering_hub.image_version_reports import REPORT_PATH, REPORT_PROJECT
from tests.test_hub_image_version_reports import report
from tests.test_hub_unified_mcp import AUTH, BASE, composed, content, initialize


def test_existing_agent_report_populates_both_authenticated_read_surfaces(composed):
    core, _bus, _settings = composed
    body = report(True)
    now = datetime.now(timezone.utc)
    for item in body['items']:
        for side in ('running', 'published'):
            item[side]['observed_at'] = now.isoformat()
            item[side]['valid_until'] = (now + timedelta(minutes=10)).isoformat()
    with TestClient(core, base_url=BASE) as client:
        session = initialize(client)
        before = content(client, session, 'hub_overview')
        assert before['image_versions']['reason'] == 'not_configured'
        saved = content(client, session, 'knowledge_put', {
            'project': REPORT_PROJECT, 'path': REPORT_PATH,
            'title': 'Synthetic image observations', 'content': json.dumps(body),
            'expected_version': 0,
        })
        assert saved['updated_by'] == 'codex'
        snapshot = content(client, session, 'hub_overview')['image_versions']
        assert snapshot['reported_by'] == 'codex'
        assert snapshot['advisory_only'] is True
        assert snapshot['items'][0]['state'] == 'update_available'
        for endpoint in ('overview', 'image-versions'):
            response = client.get('/api/hub/v1/' + endpoint,
                                  headers={'Authorization': 'Bearer ' + AUTH.codex_token})
            assert response.status_code == 200
            value = response.json()
            if endpoint == 'overview':
                assert value['runs'] == [] and not value['capabilities']['production']
                value = value['image_versions']
            assert value['items'][0]['state'] == 'update_available'
            assert value['reported_by'] == 'codex'
            assert response.headers['cache-control'] == 'no-store'
        assert client.get('/api/hub/v1/image-versions').status_code == 401
