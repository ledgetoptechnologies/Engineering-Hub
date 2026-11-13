"""Real session revocation while an authenticated request body is arriving."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig, require_human
from engineering_hub.components import ProjectComponentRepository
from engineering_hub.repository import HubError
from engineering_hub.staging_api import attach_staging_routes
from engineering_hub.staging_resources import StagingResourceStore


def run(coro):
    return asyncio.run(coro)


def setup(tmp_path):
    config = AuthConfig('https://hub.test', beau_github_id=42)
    settings = Settings(tmp_path / 'hub.sqlite3', AuthSettings('hermes-test', 'codex-test'), config)
    app = create_app(settings)
    return app, config


async def delayed_post(app, config, path, payload, *, on_body, method='POST'):
    session = app.state.sessions.create_session(42)
    raw = json.dumps(payload).encode()

    async def body():
        yield raw[:1]
        on_body(session)
        yield raw[1:]

    headers = {'origin': config.origin, 'x-hub-csrf': session['csrf_token'],
               'content-type': 'application/json',
               'cookie': f"{config.cookie_name}={session['cookie_token']}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.origin) as client:
        return await client.request(method, path, content=body(), headers=headers)


@pytest.mark.parametrize('expiry', [False, True])
def test_review_answer_rechecks_real_session_after_body(tmp_path, expiry):
    app, config = setup(tmp_path)
    repo = app.state.repository
    components = ProjectComponentRepository(repo)
    component = components.create_component('human:42', 'ledge-top-ops', 'viewer', 'Viewer', 'viewer', status='active')
    store = app.state.interactive_reviews
    store.authorize = lambda *args: True
    review = store.create_review('codex', project='ledge-top-ops', component_id=component['id'],
        component_revision=1, topic='synthetic', dataset_digest='sha256:' + 'a' * 64,
        title='Review', schema='choice-text.v1', client_request_id='create',
        items=[{'id': 'x', 'title': 'X', 'prompt': 'Answer'}])

    def revoke(session):
        if expiry:
            with app.state.sessions._db() as db:
                db.execute('UPDATE auth_sessions SET expires=0')
        else:
            app.state.sessions.delete_session(session['cookie_token'])

    response = run(delayed_post(app, config, f"/api/hub/v1/interactive-reviews/{review['id']}/answers",
        {'expected_revision': 1, 'client_request_id': 'answer',
         'answers': [{'item_id': 'x', 'value': 'yes'}]}, on_body=revoke))
    assert response.status_code == 401
    assert store.get_review('human:42', review['id'])['revision'] == 1


@pytest.mark.parametrize('expiry', [False, True])
def test_staging_policy_rechecks_real_session_after_body(tmp_path, expiry):
    # Attach the production staging routes and real session authentication to
    # a configured same-ledger resource store, without starting any worker.
    core, config = setup(tmp_path)
    resources = StagingResourceStore(core.state.repository, admin_actors=frozenset({'human:42'}))
    class Controller:
        pass
    controller = Controller()
    controller.resources = resources
    app = FastAPI()
    app.state.sessions = core.state.sessions

    @app.exception_handler(HubError)
    async def error(request, exc):
        return JSONResponse({'code': exc.code}, status_code=exc.status)

    def human(request, mutation=False):
        return require_human(request, config, app.state.sessions, mutation=mutation)

    def actor(request, mutation=False):
        if request.headers.get('authorization'):
            return AuthSettings('hermes-test', 'codex-test').agent(request)
        return human(request, mutation)

    attach_staging_routes(app, controller, actor, human)
    def revoke(session):
        if expiry:
            with app.state.sessions._db() as db:
                db.execute('UPDATE auth_sessions SET expires=0')
        else:
            app.state.sessions.delete_session(session['cookie_token'])

    response = run(delayed_post(app, config, '/api/hub/v1/staging/projects/engineering-hub/policy',
        {'expected_revision': 0, 'grants': {'codex': ['read']}}, on_body=revoke, method='PUT'))
    assert response.status_code == 401
    assert resources.get_policy('human:42', 'engineering-hub')['revision'] == 0


def test_preview_rechecks_after_awaited_session_binding(tmp_path):
    core, config = setup(tmp_path)
    app = FastAPI()
    app.state.sessions = core.state.sessions
    class Controller:
        pass
    class Broker:
        def issue(self, *args):
            pytest.fail('revoked session must not issue preview')
    async def binding(request):
        app.state.sessions.delete_session(request.cookies[config.cookie_name])
        await asyncio.sleep(0)
        return ('test', 9999999999)
    def human(request, mutation=False):
        return require_human(request, config, app.state.sessions, mutation=mutation)
    attach_staging_routes(app, Controller(), human, human, Broker(), binding)
    response = run(delayed_post(app, config,
        '/api/hub/v1/staging/resources/33762456-e740-4f80-bda9-776f8dbcf753/preview-session',
        {}, on_body=lambda session: None))
    assert response.status_code == 401

@pytest.mark.parametrize('operation', ['keep', 'plan', 'policy'])
@pytest.mark.parametrize('expiry', [False, True])
def test_staging_sync_writes_recheck_session_in_commit_transaction(tmp_path, operation, expiry, monkeypatch):
    """A session lost after HTTP recheck must roll back the same-ledger write."""
    import uuid
    from engineering_hub.auth import human_transaction_guard

    core, config = setup(tmp_path)
    repo = core.state.repository
    control = object()
    resources = StagingResourceStore(repo, admin_actors=frozenset({'human:42'}),
                                     control_principal=control)
    resources.set_policy('human:42', 'engineering-hub', 0,
                         {'human:42': ['read', 'retain', 'cleanup']})
    job = resources.bind_job(control, str(uuid.uuid4()), project='engineering-hub',
                             created_by='codex', source_commit='a' * 40)
    item = resources.register_resource(control, job['id'], 'build')
    item = resources.mark_resource_ready(control, item['id'], item['revision'],
                                         artifact_digest='sha256:' + 'b' * 64, size_bytes=1)
    resources.update_job(control, job['id'], job['revision'], terminal=True)
    item = resources.inspect_resource(control, item['id'])
    app = FastAPI()
    app.state.sessions = core.state.sessions
    class Controller:
        pass
    controller = Controller()
    controller.resources = resources

    @app.exception_handler(HubError)
    async def error(request, exc):
        return JSONResponse({'code': exc.code}, status_code=exc.status)

    def human(request, mutation=False):
        return require_human(request, config, app.state.sessions, mutation=mutation)

    attach_staging_routes(app, controller, human, human,
        human_admission=lambda request, principal: human_transaction_guard(request, config, app.state.sessions, principal))
    name = {'keep': 'set_keep', 'plan': 'cleanup_plan', 'policy': 'set_policy'}[operation]
    original = getattr(resources, name)
    def revoked_before_transaction(*args, **kwargs):
        if expiry:
            with app.state.sessions._db() as db:
                db.execute('UPDATE auth_sessions SET expires=0')
        else:
            app.state.sessions.delete_session(session['cookie_token'])
        return original(*args, **kwargs)
    monkeypatch.setattr(resources, name, revoked_before_transaction)
    session = app.state.sessions.create_session(42)
    headers = {'origin': config.origin, 'x-hub-csrf': session['csrf_token'],
               'cookie': f"{config.cookie_name}={session['cookie_token']}"}
    target, payload, method = {
        'keep': (f"/api/hub/v1/staging/resources/{item['id']}/keep",
                 {'mode': 'pinned', 'reason': 'synthetic', 'expected_revision': item['revision']}, 'POST'),
        'plan': ('/api/hub/v1/staging/cleanup/plan', {'project': 'engineering-hub'}, 'POST'),
        'policy': ('/api/hub/v1/staging/projects/engineering-hub/policy',
                   {'expected_revision': 1, 'grants': {'human:42': ['read']}}, 'PUT'),
    }[operation]
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.origin) as client:
            return await client.request(method, target, headers=headers, json=payload)
    response = run(request())
    assert response.status_code == 401, response.text
    assert resources.inspect_resource(control, item['id'])['keep'] is None
    assert resources.get_policy('human:42', 'engineering-hub')['revision'] == 1
    with repo._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM hub_staging_cleanup_plans').fetchone()[0] == 0

def test_human_sync_staging_write_fails_closed_without_admission_guard(tmp_path):
    core, config = setup(tmp_path)
    resources = StagingResourceStore(core.state.repository, admin_actors=frozenset({'human:42'}))
    class Controller:
        pass
    controller = Controller()
    controller.resources = resources
    app = FastAPI()
    app.state.sessions = core.state.sessions
    @app.exception_handler(HubError)
    async def error(request, exc):
        return JSONResponse({'code': exc.code}, status_code=exc.status)
    def human(request, mutation=False):
        return require_human(request, config, app.state.sessions, mutation=mutation)
    attach_staging_routes(app, controller, human, human)
    response = run(delayed_post(app, config, '/api/hub/v1/staging/projects/engineering-hub/policy',
        {'expected_revision': 0, 'grants': {'codex': ['read']}}, on_body=lambda session: None, method='PUT'))
    assert response.status_code == 503
    assert resources.get_policy('human:42', 'engineering-hub')['revision'] == 0
