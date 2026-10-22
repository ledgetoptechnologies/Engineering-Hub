"""Actual standalone Hub REST/MCP interactive-data boundaries; no live services."""
import base64
from io import BytesIO
import json
import uuid

from PIL import Image
import pytest

from tests.test_hub_api import BASE, agent_headers, hub, human_headers
from engineering_hub.private_gateway import browser_route_allowed


def rpc(client, method, params=None, token='test-codex'):
    headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    return client.post('/mcp/', headers=headers, json={'jsonrpc': '2.0', 'id': 1,
        'method': method, 'params': params or {}})


def call(client, name, args=None, token='test-codex'):
    response = rpc(client, 'tools/call', {'name': name, 'arguments': args or {}}, token)
    assert response.status_code == 200, response.text
    result = response.json()['result']
    if not result.get('isError'):
        return result.get('structuredContent', json.loads(result['content'][0]['text']))
    return result


def raster():
    output = BytesIO()
    with Image.new('RGB', (4, 4), 'green') as image:
        image.save(output, format='PNG')
    return base64.b64encode(output.getvalue()).decode('ascii')


def create_set(client):
    return call(client, 'hub_label_set_create', {'set_id': 'trees', 'project': 'treedar',
        'title': 'Crown review', 'instructions': '<script>plain text only</script>',
        'choices': [{'key': 'oak', 'label': 'Oak'}, {'key': 'pine', 'label': 'Pine'}],
        'items': [{'id': 'tree-1', 'meta': {'confidence': 0.9}}]}, token='test-claude')


def upload(client):
    return call(client, 'hub_label_set_put_image', {'set_id': 'trees', 'item_id': 'tree-1',
        'content_base64': raster(), 'media_type': 'image/png'})


def assert_raster_headers(response):
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'image/png'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['content-security-policy'] == "default-src 'none'; sandbox"
    assert response.content.startswith(b'\x89PNG\r\n\x1a\n')


def test_mcp_label_creation_auth_identity_and_discovery(hub):
    client, app, _ = hub
    created = create_set(client)
    assert created['items'][0] == {'id': 'tree-1', 'meta': {'confidence': 0.9}, 'label': None, 'revision': 0}
    assert app.state.repository.list_audit()[-1]['actor'] == 'claude'
    assert upload(client)['content_type'] == 'image/png'
    assert app.state.repository.list_audit()[-1]['actor'] == 'codex'
    assert call(client, 'hub_label_set_results', {'set_id': 'trees'})['id'] == 'trees'
    assert call(client, 'hub_label_set_list', {'project': 'treedar'})['items'][0]['item_count'] == 1
    tools = rpc(client, 'tools/list').json()['result']['tools']
    names = {tool['name'] for tool in tools}
    assert {'hub_label_set_create', 'hub_label_set_put_image', 'hub_label_set_results',
            'hub_label_set_list', 'hub_entity_put_image', 'hub_entity_images'} <= names
    assert not {'hub_label_set_label', 'hub_decision_answer', 'hub_label_set_delete'} & names
    for tool in tools:
        assert not {'actor', 'identity', 'github_id'} & set(tool['inputSchema'].get('properties', {}))
    assert rpc(client, 'tools/call', {'name': 'hub_label_set_list', 'arguments': {'project': 'treedar'}}, None).status_code == 401


def test_authenticated_image_reads_and_strict_csp_survive_app_middleware(hub):
    client, app, settings = hub
    create_set(client)
    upload(client)
    paths = ['/label-sets?project=treedar', '/label-sets/trees', '/label-sets/trees/results',
             '/label-sets/trees/items/tree-1/image']
    for path in paths:
        assert client.get(BASE + path).status_code == 401
        assert client.get(BASE + path, headers=agent_headers()).status_code == 200
    human_headers(client, app, settings)
    response = client.get(BASE + '/label-sets/trees/items/tree-1/image')
    assert_raster_headers(response)


def test_only_session_human_can_label_with_csrf_cas(hub):
    client, app, settings = hub
    create_set(client)
    upload(client)
    path = BASE + '/label-sets/trees/items/tree-1/label'
    payload = {'choice': 'oak', 'expected_revision': 0}
    for agent in ['codex', 'hermes', 'claude']:
        for choice in ['oak', None]:
            assert client.put(path, headers=agent_headers(agent), json=payload | {'choice': choice}).status_code == 403
    human = human_headers(client, app, settings)
    for headers in [{}, {'Origin': settings.auth_config.origin}, human | {'X-Hub-CSRF': 'bad'},
                    human | {'Origin': 'https://evil.test'}]:
        assert client.put(path, headers=headers, json=payload).status_code == 403
    saved = client.put(path, headers=human, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()['label']['by'] == 'human:42'
    assert saved.json()['revision'] == 1
    assert client.put(path, headers=human, json=payload).status_code == 409
    assert client.put(path, headers=human, json={'choice': None, 'expected_revision': True}).status_code == 422
    assert client.put(path, headers=human, json={'choice': None, 'expected_revision': 1, 'actor': 'human:99'}).status_code == 422
    cleared = client.put(path, headers=human, json={'choice': None, 'expected_revision': 1})
    assert cleared.status_code == 200 and cleared.json()['label'] is None and cleared.json()['revision'] == 2


@pytest.mark.parametrize('encoded', ['!', 'data:image/png;base64,aaaa', 'Y Q==', 'YQ', 'YR==', 'A' * 349529],
                         ids=['malformed', 'data-url', 'whitespace', 'missing-padding', 'noncanonical', 'oversized'])
def test_mcp_image_base64_bounded_and_no_payload_reflection(hub, encoded):
    client, app, _ = hub
    create_set(client)
    before = len(app.state.repository.list_audit())
    result = call(client, 'hub_label_set_put_image', {'set_id': 'trees', 'item_id': 'tree-1',
        'content_base64': encoded, 'media_type': 'image/png'})
    assert result['isError'] is True
    assert 'invalid_label_image' in str(result)
    assert len(app.state.repository.list_audit()) == before


@pytest.mark.parametrize('media_type,payload', [('image/svg+xml', b'<svg onload="alert(1)"/>'),
                                               ('image/png', b'<html>not raster data</html>')])
def test_mcp_rejects_markup_even_when_valid_base64(hub, media_type, payload):
    client, app, _ = hub
    create_set(client)
    result = call(client, 'hub_label_set_put_image', {'set_id': 'trees', 'item_id': 'tree-1',
        'content_base64': base64.b64encode(payload).decode(), 'media_type': media_type})
    assert result['isError'] is True
    assert 'invalid_label_image' in str(result)
    assert len(app.state.repository.list_audit()) == 1


def test_decision_context_free_answer_and_inert_agent_attachment(hub):
    client, app, settings = hub
    decision = call(client, 'hub_decision_request', {'project': 'treedar', 'title': 'Species?',
        'options': ['Oak', 'Other'], 'allow_text': True, 'details': '<script>literal context</script>',
        'links': [{'label': 'Reference', 'url': 'https://example.com/species'}]}, token='test-hermes')
    assert decision['details'] == '<script>literal context</script>'
    image_id = str(uuid.uuid4())
    attachment = call(client, 'hub_entity_put_image', {'image_id': image_id, 'kind': 'decision',
        'entity_id': decision['id'], 'expected_revision': decision['revision'],
        'content_base64': raster(), 'media_type': 'image/png', 'title': 'Crown reference'}, token='test-claude')
    assert attachment['by'] == 'claude'
    assert call(client, 'hub_entity_images', {'kind': 'decision', 'entity_id': decision['id']})['items'][0]['id'] == image_id
    assert client.get(BASE + '/attachments/' + image_id + '/image').status_code == 401
    human = human_headers(client, app, settings)
    assert_raster_headers(client.get(BASE + '/attachments/' + image_id + '/image'))
    context_path = BASE + '/decisions/' + decision['id'] + '/context'
    payload = {'expected_revision': attachment['entity_revision'], 'details': 'Human-reviewed explanation'}
    assert client.post(context_path, json=payload).status_code == 403
    context = client.post(context_path, headers=human, json=payload)
    assert context.status_code == 200, context.text
    answer_path = BASE + '/decisions/' + decision['id'] + '/answer'
    answer = {'expected_revision': context.json()['revision'], 'answer': 'Actually this is a maple'}
    assert client.post(answer_path, headers=agent_headers('claude'), json=answer).status_code == 403
    saved = client.post(answer_path, headers=human, json=answer)
    assert saved.status_code == 200, saved.text
    assert saved.json()['answer'] == 'Actually this is a maple'
    assert saved.json()['execution_authorization'] is False
    # Attachment upload advances context, not permission, and cannot mutate a closed decision.
    closed_upload = call(client, 'hub_entity_put_image', {'image_id': str(uuid.uuid4()), 'kind': 'decision',
        'entity_id': decision['id'], 'expected_revision': saved.json()['revision'],
        'content_base64': raster(), 'media_type': 'image/png'})
    assert closed_upload['isError'] is True and 'decision_closed' in str(closed_upload)


def test_protected_free_text_rejected_through_rest_and_mcp(hub):
    client, app, settings = hub
    payload = {'project': 'treedar', 'title': 'Choose?', 'options': ['Yes', 'No'],
               'protected': True, 'allow_text': True}
    human = human_headers(client, app, settings)
    response = client.post(BASE + '/decisions', headers=human, json=payload)
    assert response.status_code == 400, response.text
    assert 'invalid_options' in response.text
    result = call(client, 'hub_decision_request', payload)
    assert result['isError'] is True and 'invalid_options' in str(result)
    assert app.state.repository.list_decisions() == []
    assert app.state.repository.list_audit() == []
    assert app.state.repository.list_outbox() == []


def test_human_api_protected_legacy_record_remains_preset_only(hub):
    client, app, settings = hub
    repo = app.state.repository
    item = repo.create_decision('claude', 'treedar', 'Choose?', ['Yes', 'No'], protected=True)
    item['allow_text'] = True
    with repo._transaction() as db:
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), item['id']))
    path = BASE + '/decisions/' + item['id'] + '/answer'
    payload = {'expected_revision': item['revision'], 'answer': 'Off-menu'}
    assert client.post(path, headers=agent_headers(), json=payload).status_code == 403
    human = human_headers(client, app, settings)
    assert client.post(path, json=payload).status_code == 403
    audit, outbox = repo.list_audit(), repo.list_outbox()
    assert client.post(path, headers=human | {'Origin': 'https://evil.test'}, json=payload).status_code == 403
    assert client.post(path, headers=human, json=payload | {'expected_revision': 99}).status_code == 409
    assert client.post(path, headers=human, json=payload).status_code == 409
    assert repo.list_decisions() == [item]
    assert repo.list_audit() == audit and repo.list_outbox() == outbox
    payload['answer'] = 'Yes'
    saved = client.post(path, headers=human, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()['execution_authorization'] is False
    assert client.post(path, headers=human, json=payload).status_code == 409


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,hello', 'http://example.com'])
def test_agent_context_rejects_executable_and_non_https_links(hub, url):
    client, app, _ = hub
    result = call(client, 'hub_decision_request', {'project': 'treedar', 'title': 'Choose?',
        'options': ['A', 'B'], 'links': [{'label': 'Unsafe', 'url': url}]})
    assert result['isError'] is True
    assert app.state.repository.list_decisions() == []


def test_gateway_label_and_attachment_surface_is_closed():
    identity = str(uuid.uuid4())
    for method, path in [
        ('GET', '/api/hub/v1/label-sets'), ('GET', '/api/hub/v1/label-sets/trees'),
        ('GET', '/api/hub/v1/label-sets/trees/results'),
        ('GET', '/api/hub/v1/label-sets/trees/items/tree-1/image'),
        ('PUT', '/api/hub/v1/label-sets/trees/items/tree-1/label'),
        ('GET', f'/api/hub/v1/attachments/{identity}/image'),
        ('GET', f'/api/hub/v1/decisions/{identity}/attachments'),
        ('POST', f'/api/hub/v1/decisions/{identity}/context'),
    ]:
        assert browser_route_allowed(method, path)
    for method, path in [('POST', '/api/hub/v1/label-sets'), ('DELETE', '/api/hub/v1/label-sets/trees'),
        ('PUT', '/api/hub/v1/label-sets/trees/items/tree-1/image'),
        ('POST', f'/api/hub/v1/attachments/{identity}/image'), ('POST', '/mcp/'),
        ('GET', '/api/hub/v1/label-sets/trees/execute')]:
        assert not browser_route_allowed(method, path)
