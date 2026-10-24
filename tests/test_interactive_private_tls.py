"""Actual Hub session -> private TLS service -> immutable synthetic pointcloud."""
import hashlib
import json
import ssl

from fastapi.testclient import TestClient
import httpx

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.components import ProjectComponentRepository
from hub_staging_assets.server import AssetCatalog, tls_context
from tests.test_staging_assets_service import asset_files, tls_files, running, TOKEN


def test_authenticated_hub_review_and_actual_tls_asset_transport(tmp_path):
    content = json.dumps({'schema': 'pointcloud.v1', 'points': [[0, 0, 0], [1, 2, 3]]}).encode()
    root, source, manifest, asset_id, values = asset_files(tmp_path, content, 'application/json')
    cert, key = tls_files(tmp_path)
    (tmp_path / 'interactive-service.json').write_text(json.dumps({'schema': 1, 'enabled': True,
        'grants': {'treedar': {'codex': ['review_read', 'review_create'],
                              'human:42': ['review_read', 'review_answer']}}}))
    settings = Settings(tmp_path / 'hub.sqlite3', AuthSettings('hermes-token', 'codex-token'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    app = create_app(settings)
    component = ProjectComponentRepository(app.state.repository).create_component('human:42',
        'treedar', 'field-review', 'Field review', 'create-component', status='active')
    with running(AssetCatalog(root, manifest), context=tls_context(cert, key)) as worker:
        service = app.state.interactive_service
        service.assets = {asset_id: {'project': 'treedar', 'component_id': component['id'],
            'size': len(content), 'sha256': values[asset_id]['sha256'], 'media_type': 'application/json'}}
        service.pin_assets(app.state.repository)
        service.client = httpx.AsyncClient(base_url=f'https://127.0.0.1:{worker.server_port}',
            headers={'Authorization': 'Bearer ' + TOKEN}, verify=ssl.create_default_context(cafile=str(cert)),
            trust_env=False, follow_redirects=False, timeout=3)
        with TestClient(app, base_url='https://hub.test') as client:
            payload = {'project': 'treedar', 'component_id': component['id'], 'component_revision': 1,
                'topic': 'field-review', 'dataset_digest': 'sha256:' + hashlib.sha256(content).hexdigest(),
                'title': 'Classify tree', 'client_request_id': 'create-review',
                'items': [{'id': 'tree-1', 'title': 'Tree 1', 'prompt': 'Species?', 'asset_id': asset_id}]}
            response = client.post('/api/hub/v1/interactive-reviews', json=payload,
                                   headers={'Authorization': 'Bearer codex-token'})
            assert response.status_code == 201, response.text
            path = '/api/hub/v1/interactive-reviews/' + response.json()['id'] + '/assets/' + asset_id
            assert client.get(path).status_code == 401
            session = app.state.sessions.create_session(42)
            client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
            full = client.get(path)
            assert full.status_code == 200 and full.content == content
            assert full.headers['etag'] == '"sha256:' + hashlib.sha256(content).hexdigest() + '"'
            assert client.head(path).headers['content-length'] == str(len(content))
            part = client.get(path, headers={'Range': 'bytes=0-7'})
            assert part.status_code == 206 and part.content == content[:8]
            assert part.headers['content-range'] == f'bytes 0-7/{len(content)}'
            assert client.get(path, headers={'Range': 'bytes=0-1,5-6'}).status_code == 416
            source.write_bytes(b'changed')
            assert client.get(path).status_code == 503
