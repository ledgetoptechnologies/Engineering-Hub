import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from engineering_hub import staging_controller
from engineering_hub.repository import HubError
from engineering_hub.staging_api import attach_staging_routes
from tests.test_hub_staging_controller import env
from tests.test_profile_worker_chain import bound_request


@pytest.mark.asyncio
async def test_owner_handoff_preserves_original_unknown_without_effect(env, monkeypatch):
    controller, client, resources, _, repo = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = client.put_source
    async def lost(payload):
        await original(payload)
        raise TimeoutError()
    monkeypatch.setattr(client, 'put_source', lost)
    with pytest.raises(TimeoutError):
        await controller.submit('codex', request)
    with repo._connection() as db:
        row = db.execute('SELECT * FROM hub_staging_submissions WHERE request_id=?', (request['request_id'],)).fetchone()
        original_data = json.loads(row['data'])
        before = tuple(row)
    guard_calls = []
    result = controller.source_intent_handoffs('human:1', admission_guard=lambda db: guard_calls.append(True))
    item, = result['items']
    assert guard_calls == [True]
    assert item['source_id'] == original_data['source_id'] and item['request_id'] == request['request_id']
    assert item['source_binding'] == original_data['source_binding']
    assert item['source_identity_assurance'] == 'journal_only'
    assert item['worker_ledger_authority'] == 'unverified'
    assert item['execution_authorization'] is False and item['automatic_retry_or_release'] is False
    assert client.deposits == 1 and client.posts == 0
    with repo._connection() as db:
        assert tuple(db.execute('SELECT * FROM hub_staging_submissions WHERE id=?', (row['id'],)).fetchone()) == before
    with pytest.raises(HubError):
        controller.source_intent_handoffs('codex', admission_guard=lambda db: None)
    with pytest.raises(HubError):
        controller.source_intent_handoffs('human:2', admission_guard=lambda db: None)
    with pytest.raises(HubError):
        controller.source_intent_handoffs('human:1')
    with pytest.raises(HubError, match='guard_denied'):
        controller.source_intent_handoffs('human:1', admission_guard=lambda db: (_ for _ in ()).throw(HubError('guard_denied', 403)))


@pytest.mark.asyncio
async def test_valid_uuid_replacement_is_labeled_journal_only(env, monkeypatch):
    controller, client, _, _, repo = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = client.put_source
    async def lost(payload):
        await original(payload)
        raise TimeoutError()
    monkeypatch.setattr(client, 'put_source', lost)
    with pytest.raises(TimeoutError):
        await controller.submit('codex', request)
    replacement = str(uuid.uuid4())
    with repo._transaction() as db:
        row = db.execute('SELECT id,data FROM hub_staging_submissions WHERE request_id=?', (request['request_id'],)).fetchone()
        item = json.loads(row['data'])
        item['source_id'] = replacement
        db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (json.dumps(item), row['id']))
    handoff, = controller.source_intent_handoffs('human:1', admission_guard=lambda db: None)['items']
    assert handoff['source_id'] == replacement and handoff['source_identity_assurance'] == 'journal_only'
    assert handoff['worker_ledger_authority'] == 'unverified' and handoff['automatic_retry_or_release'] is False


@pytest.mark.asyncio
async def test_corrupt_handoff_never_returns_untrusted_content(env, monkeypatch):
    controller, client, _, _, repo = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = client.put_source
    async def lost(payload):
        await original(payload)
        raise TimeoutError()
    monkeypatch.setattr(client, 'put_source', lost)
    with pytest.raises(TimeoutError):
        await controller.submit('codex', request)
    with repo._transaction() as db:
        row = db.execute('SELECT id,data FROM hub_staging_submissions WHERE request_id=?', (request['request_id'],)).fetchone()
        item = json.loads(row['data'])
        item['source_id'] = 'untrusted-secret-like-text'
        db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (json.dumps(item), row['id']))
    result = controller.source_intent_handoffs('human:1', admission_guard=lambda db: None)
    assert result['items'] == [{'submission_id': row['id'], 'state': 'unreadable'}]
    assert 'untrusted-secret-like-text' not in json.dumps(result)


@pytest.mark.asyncio
async def test_http_handoff_requires_admin_and_live_read_guard(env, monkeypatch):
    controller, client, _, _, _ = env
    monkeypatch.setattr(staging_controller, '_PROFILE_PROTOCOL_READY', True)
    request, _ = bound_request(env)
    original = client.put_source
    async def lost(payload):
        await original(payload)
        raise TimeoutError()
    monkeypatch.setattr(client, 'put_source', lost)
    with pytest.raises(TimeoutError):
        await controller.submit('codex', request)
    app = FastAPI()
    @app.exception_handler(HubError)
    async def error(request, exc):
        return JSONResponse({'code': exc.code}, status_code=exc.status)
    def principal(request, mutation=False):
        return request.headers.get('X-Test-Human', 'human:2')
    def read_guard(request, actor):
        def check(db):
            if request.headers.get('X-Test-Expired') == 'yes':
                raise HubError('session_expired', 401)
        return check
    attach_staging_routes(app, controller, principal, principal, human_read_admission=read_guard)
    with TestClient(app) as http:
        endpoint = '/api/hub/v1/staging/source-intent-handoffs'
        assert http.get(endpoint, headers={'X-Test-Human': 'human:2'}).status_code == 403
        expired = http.get(endpoint, headers={'X-Test-Human': 'human:1', 'X-Test-Expired': 'yes'})
        assert expired.status_code == 401 and 'source_binding' not in expired.text
        response = http.get(endpoint, headers={'X-Test-Human': 'human:1'})
        assert response.status_code == 200 and response.json()['items'][0]['request_id'] == request['request_id']
        assert response.json()['worker_authority_verified'] is False


def test_owner_pages_are_bounded_even_with_unrelated_or_corrupt_rows(env):
    controller, _, _, _, repo = env
    identities = sorted(str(uuid.uuid4()) for _ in range(3))
    with repo._transaction() as db:
        for index, identity in enumerate(identities):
            phase = 'queued' if index == 0 else 'deposit_unknown'
            db.execute('INSERT INTO hub_staging_submissions VALUES(?,?,?,?,?)',
                       (identity, 'codex', str(uuid.uuid4()), '0' * 64, json.dumps({'phase': phase})))
    first = controller.source_intent_handoffs('human:1', limit=1, admission_guard=lambda db: None)
    assert first['items'] == [] and first['next_cursor'] == identities[0]
    second = controller.source_intent_handoffs('human:1', after=first['next_cursor'], limit=1, admission_guard=lambda db: None)
    assert second['items'] == [{'submission_id': identities[1], 'state': 'unreadable'}]
    assert second['next_cursor'] == identities[1]
    third = controller.source_intent_handoffs('human:1', after=second['next_cursor'], limit=1, admission_guard=lambda db: None)
    assert third['items'] == [{'submission_id': identities[2], 'state': 'unreadable'}] and third['next_cursor'] is None


def test_deeply_nested_corrupt_row_is_redacted(env):
    import sys
    controller, _, _, _, repo = env
    identity = str(uuid.uuid4())
    nested = '[' * 600 + '"private-marker"' + ']' * 600
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_staging_submissions VALUES(?,?,?,?,?)',
                   (identity, 'codex', str(uuid.uuid4()), '0' * 64, nested))
    old_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(400)
        result = controller.source_intent_handoffs('human:1', admission_guard=lambda db: None)
    finally:
        sys.setrecursionlimit(old_limit)
    assert result['items'] == [{'submission_id': identity, 'state': 'unreadable'}]
    assert 'private-marker' not in json.dumps(result)


def test_composed_app_owner_session_expiry_logout_and_no_worker_effect(tmp_path, monkeypatch):
    import base64
    import hashlib
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from hub_staging_service.source_provenance import verify
    from tests.test_hub_staging_controller import payload
    from tests.test_shared_staging_preparation import config_file

    config = config_file(tmp_path)
    values = json.loads(config.read_text())
    values['admin_actors'] = ['human:42']
    config.write_text(json.dumps(values))
    settings = Settings(tmp_path / 'app.sqlite3', AuthSettings('test-hermes', 'test-codex', claude_token='test-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42), staging_config_path=config)
    app = create_app(settings)
    controller = app.state.modular_staging.controller
    assert controller is not None
    repo = app.state.repository
    request = payload(repo)
    binding = verify(base64.b64decode(request['source_zip_base64']), request['source_commit_object_base64'], request['source_commit'])
    selected = {'project': request['project'], 'recipe': request['recipe'], 'generation': 1, 'digest': 'sha256:' + 'a' * 64}
    public = {k: v for k, v in request.items() if k not in {'source_zip_base64', 'source_commit_object_base64'}}
    public.update(source_binding=binding, topic=None, reviewed_profile=selected)
    identity = str(uuid.uuid4())
    item = dict(public, id=identity, actor='codex', source_id=str(uuid.uuid4()), command_id=str(uuid.uuid4()),
                phase='deposit_unknown', job_id=None, resource_ids={})
    fingerprint = hashlib.sha256(staging_controller._json(public).encode()).hexdigest()
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_staging_submissions VALUES(?,?,?,?,?)',
                   (identity, 'codex', request['request_id'], fingerprint, staging_controller._json(item)))
    async def forbidden(*args, **kwargs):
        raise AssertionError('handoff must not contact the worker')
    monkeypatch.setattr(controller.client, '_request', forbidden)
    endpoint = '/api/hub/v1/staging/source-intent-handoffs'
    with TestClient(app, base_url=settings.auth_config.origin) as http:
        session = app.state.sessions.create_session(42)
        http.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
        response = http.get(endpoint)
        assert response.status_code == 200 and response.json()['items'][0]['submission_id'] == identity
        assert response.json()['items'][0]['source_identity_assurance'] == 'journal_only'
        assert http.get(endpoint, headers={'Authorization': 'Bearer test-codex'}).status_code in (401, 403)
        monkeypatch.setattr(controller.resources, 'admin_actors', frozenset({'human:1'}))
        assert http.get(endpoint).status_code == 403
        monkeypatch.setattr(controller.resources, 'admin_actors', frozenset({'human:42'}))
        now = app.state.sessions.clock
        original_validate = staging_controller._valid_source_binding
        def expire_during_read(value):
            result = original_validate(value)
            app.state.sessions.clock = lambda: now() + 86400 * 1000
            return result
        monkeypatch.setattr(staging_controller, '_valid_source_binding', expire_during_read)
        response = http.get(endpoint)
        assert response.status_code == 401 and 'source_binding' not in response.text
        app.state.sessions.clock = now
        monkeypatch.setattr(staging_controller, '_valid_source_binding', original_validate)
        original_handoffs = controller.source_intent_handoffs
        def logout_after_auth(*args, **kwargs):
            app.state.sessions.delete_session(session['cookie_token'])
            return original_handoffs(*args, **kwargs)
        monkeypatch.setattr(controller, 'source_intent_handoffs', logout_after_auth)
        response = http.get(endpoint)
        assert response.status_code == 401 and 'source_binding' not in response.text
