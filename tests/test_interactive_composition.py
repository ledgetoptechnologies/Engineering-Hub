import json

from fastapi.testclient import TestClient

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.interactive_service import InteractiveService
from engineering_hub.repository import HubRepository
from engineering_hub.private_gateway import browser_route_allowed


def test_real_session_agent_creation_csrf_answers_restart_and_logout(tmp_path):
    config = tmp_path / 'interactive-service.json'
    config.write_text(json.dumps({'schema': 1, 'enabled': True, 'grants': {'treedar': {
        'codex': ['review_read', 'review_create'], 'human:42': ['review_read', 'review_answer']}}}))
    settings = Settings(tmp_path / 'hub.sqlite3', AuthSettings('hermes-token', 'codex-token'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    app = create_app(settings)
    item = app.state.project_registry  # No catalog startup seeds.
    assert item.get_project('treedar')['id'] == 'treedar'
    from engineering_hub.components import ProjectComponentRepository
    component = ProjectComponentRepository(app.state.repository).create_component('human:42',
        'treedar', 'field-review', 'Field review', 'component-create', status='active')
    payload = {'project': 'treedar', 'component_id': component['id'], 'component_revision': 1,
        'topic': 'field-review', 'dataset_digest': 'sha256:' + 'a'*64, 'title': 'Classify trees',
        'client_request_id': 'review-create', 'items': [{'id': 'tree-1', 'title': 'Tree 1',
            'prompt': 'Select species', 'choices': [{'id': 'oak', 'label': 'Oak'}, {'id': 'pine', 'label': 'Pine'}]}]}
    root = '/api/hub/v1/interactive-reviews'
    with TestClient(app, base_url='https://hub.test') as client:
        created = client.post(root, headers={'Authorization': 'Bearer codex-token'}, json=payload)
        assert created.status_code == 201, created.text
        review = created.json()
        path = root + '/' + review['id']
        assert client.get(path).status_code == 401
        assert client.get(path, headers={'Authorization': 'Bearer hermes-token'}).status_code == 403
        session = app.state.sessions.create_session(42)
        client.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
        assert client.get(path).status_code == 200
        answer = {'expected_revision': 1, 'client_request_id': 'answer-1',
                  'answers': [{'item_id': 'tree-1', 'value': 'oak'}], 'complete': True}
        assert client.post(path + '/answers', json=answer).status_code == 403
        headers = {'Origin': 'https://hub.test', 'X-Hub-CSRF': session['csrf_token']}
        saved = client.post(path + '/answers', headers=headers, json=answer)
        assert saved.status_code == 200, saved.text
        assert saved.json()['answers']['tree-1']['by'] == 'human:42'
        assert saved.json()['state'] == 'completed'
        assert client.post(path + '/answers', headers=headers, json=answer).status_code == 200
        # Logout removes admission to the same review, not merely its UI link.
        assert client.post('/api/hub/v1/auth/logout', headers=headers).status_code in (200, 204)
        assert client.get(path).status_code == 401
    restarted = create_app(settings)
    assert restarted.state.interactive_reviews.get_review('codex', review['id'])['answers']['tree-1']['value'] == 'oak'


def test_asset_uuid_cannot_be_rebound_after_restart(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    asset = {'project': 'treedar', 'component_id': 'a'*36, 'size': 4,
             'sha256': 'a'*64, 'media_type': 'application/json'}
    first = InteractiveService(assets={'asset': asset.copy()}, state='configured')
    first.pin_assets(repo)
    same = InteractiveService(assets={'asset': asset.copy()}, state='configured')
    same.pin_assets(repo)
    assert same.assets
    changed = InteractiveService(assets={'asset': dict(asset, sha256='b'*64)}, state='configured')
    changed.pin_assets(repo)
    assert changed.state == 'asset_binding_invalid' and changed.assets == {}
    first.pin_assets(repo)
    assert first.assets


def test_closed_browser_interaction_routes():
    review = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    root = '/api/hub/v1/interactive-reviews/' + review
    assert browser_route_allowed('GET', root)
    assert browser_route_allowed('GET', root + '/state')
    assert browser_route_allowed('HEAD', root + '/assets/' + review)
    assert browser_route_allowed('POST', root + '/answers')
    assert not browser_route_allowed('POST', '/api/hub/v1/interactive-reviews')
    assert not browser_route_allowed('DELETE', root)
    assert not browser_route_allowed('GET', root + '/proxy/arbitrary')
