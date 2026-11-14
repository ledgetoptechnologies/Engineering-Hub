import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from engineering_hub.interactive_reviews_api import attach_interactive_review_routes
from engineering_hub.repository import HubError
from tests.test_hub_interactive_reviews import env, payload, PROJECT


BASE = '/api/hub/v1/interactive-reviews'
AGENT = {'Authorization': 'Bearer codex'}
HUMAN = {'X-Human': 'human:1', 'X-CSRF': 'verified'}


def client(env, admission=True):
    app = FastAPI()
    @app.exception_handler(HubError)
    async def handler(request, error):
        return JSONResponse({'code': error.code}, status_code=error.status)
    def actor(request, mutation=False):
        if request.headers.get('authorization') == 'Bearer codex':
            return 'codex'
        identity = request.headers.get('x-human')
        if identity is None:
            raise HubError('unauthorized', 401)
        if mutation and request.headers.get('x-csrf') != 'verified':
            raise HubError('csrf_required', 403)
        return identity
    def human(request, mutation=False):
        identity = actor(request, mutation)
        env[4]._actor(identity, human=True)
        return identity
    # This isolated API fixture uses synthetic identities, not real sessions.
    attach_interactive_review_routes(app, env[0], actor, human,
        human_admission=(lambda request, principal: lambda db: None) if admission is True else
                        (lambda request, principal: None) if admission == 'noncallable' else None)
    return TestClient(app)


def test_agent_creation_human_phone_choice_text_and_completion(env):
    api = client(env)
    response = api.post(BASE, headers=AGENT, json=payload(env))
    assert response.status_code == 201, response.text
    item = response.json()
    listed = api.get(BASE, headers=HUMAN, params={'project': PROJECT}).json()['items'][0]
    assert listed['item_count'] == 2 and 'items' not in listed
    path = BASE + '/' + item['id']
    assert api.get(path, headers=HUMAN).json()['items'] == item['items']
    first = api.post(path + '/answers', headers=HUMAN, json={'expected_revision': 1,
        'client_request_id': 'phone-choice', 'answers': [{'item_id': 'label', 'value': 'yes'}]})
    assert first.status_code == 200 and first.json()['revision'] == 2
    done = api.post(path + '/answers', headers=HUMAN, json={'expected_revision': 2,
        'client_request_id': 'phone-note', 'answers': [{'item_id': 'note', 'value': 'Human note'}], 'complete': True})
    assert done.status_code == 200 and done.json()['state'] == 'completed'
    state = api.get(path + '/state', headers=HUMAN).json()
    assert state['answered_count'] == 2 and state['answers']['note']['by'] == 'human:1'


def test_openapi_success_schemas_match_actual_wire_responses(env):
    from jsonschema import Draft202012Validator

    api = client(env)
    paths = api.app.openapi()['paths']

    def check(path, method, response):
        assert response.status_code in (200, 201)
        schema = paths[path][method]['responses'][str(response.status_code)]['content']['application/json']['schema']
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(response.json())

    created = api.post(BASE, headers=AGENT, json=payload(env))
    check(BASE, 'post', created)
    identity = created.json()['id']
    path = BASE + '/' + identity
    check(BASE, 'get', api.get(BASE, headers=HUMAN, params={'project': PROJECT}))
    check(BASE + '/{review_id}', 'get', api.get(path, headers=HUMAN))
    check(BASE + '/{review_id}/state', 'get', api.get(path + '/state', headers=HUMAN))
    answered = api.post(path + '/answers', headers=HUMAN, json={'expected_revision': 1,
        'client_request_id': 'schema-answer', 'answers': [{'item_id': 'label', 'value': 'yes'}]})
    check(BASE + '/{review_id}/answers', 'post', answered)
    check(BASE + '/{review_id}/state', 'get', api.get(path + '/state', headers=HUMAN))


def test_logout_csrf_and_agent_answer_denials(env):
    api = client(env)
    item = api.post(BASE, headers=AGENT, json=payload(env)).json()
    path = BASE + '/' + item['id']
    answer = {'expected_revision': 1, 'client_request_id': 'answer',
              'answers': [{'item_id': 'label', 'value': 'yes'}]}
    assert api.get(path).status_code == 401
    assert api.post(path + '/answers', headers={'X-Human': 'human:1'}, json=answer).status_code == 403
    assert api.post(path + '/answers', headers=AGENT, json=answer).status_code == 403
    assert api.post(BASE, headers=HUMAN, json=payload(env)).status_code == 403
    env[3].remove(('human:1', PROJECT, 'review_read'))
    assert api.get(path, headers=HUMAN).status_code == 403
    assert api.post(path + '/answers', headers=HUMAN, json=answer).status_code == 403


def test_closed_schema_no_arbitrary_code_urls_and_bounded_answer_batch(env):
    api = client(env)
    for extra in ({'code': 'print(1)'}, {'worker_url': 'https://example.test'},
                  {'schema': 'executable.v1'}, {'component_revision': True}):
        assert api.post(BASE, headers=AGENT, json=payload(env, **extra)).status_code == 422
    bad = payload(env, items=[{'id': 'x', 'title': 'x', 'prompt': 'x', 'asset_url': 'https://example.test'}])
    assert api.post(BASE, headers=AGENT, json=bad).status_code == 422
    item = api.post(BASE, headers=AGENT, json=payload(env)).json()
    assert api.post(BASE + '/' + item['id'] + '/answers', headers=HUMAN,
        json={'expected_revision': 1, 'client_request_id': 'large',
              'answers': [{'item_id': 'item' + str(i), 'value': 'x'} for i in range(101)]}).status_code == 422
    stream = (b'x' * 600000 for _ in range(2))
    assert api.post(BASE, headers={**AGENT, 'Content-Type': 'application/json'}, content=stream).status_code == 413


def test_component_version_change_is_visible_as_conflict_not_old_answers(env):
    api = client(env)
    item = api.post(BASE, headers=AGENT, json=payload(env)).json()
    env[1].update_component('human:1', env[2]['id'], 1, description='new context')
    response = api.get(BASE + '/' + item['id'], headers=HUMAN)
    assert response.status_code == 409 and response.json()['code'] == 'review_component_changed'


@pytest.mark.parametrize('admission', [False, 'noncallable'])
def test_answer_fails_closed_without_transaction_guard(env, admission):
    api = client(env, admission=admission)
    item = api.post(BASE, headers=AGENT, json=payload(env)).json()
    response = api.post(BASE + '/' + item['id'] + '/answers', headers=HUMAN,
        json={'expected_revision': 1, 'client_request_id': 'answer',
              'answers': [{'item_id': 'label', 'value': 'yes'}]})
    assert response.status_code == 503
    assert response.json()['code'] == 'interactive_review_admission_unavailable'
    assert env[0].get_review('human:1', item['id'])['revision'] == 1
