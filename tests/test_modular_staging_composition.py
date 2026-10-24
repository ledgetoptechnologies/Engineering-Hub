"""Composition boundaries only; synthetic controllers, no worker/host effects."""
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.responses import JSONResponse

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig, SessionStore
from engineering_hub.cloudflare_access import CloudflareIdentity
from engineering_hub.frontends import HubFrontends
from engineering_hub.repository import HubRepository
from engineering_hub.staging_module import StagingModule, compose_staging
from engineering_hub.staging_preview import PreviewBroker
from engineering_hub.staging_resources import StagingResourceStore
from engineering_hub.work_queue import WorkAction, WorkQueue
from tests.test_hub_frontends import Feed


HUB = 'https://hub.example'
PREVIEW = 'https://preview.example'


def settings(tmp_path, *, access=False):
    auth = AuthConfig(HUB, human_auth_mode='cloudflare_access' if access else 'github',
        cloudflare_issuer='https://fixture-team.cloudflareaccess.com' if access else '',
        cloudflare_audience='fixture-hub' if access else '',
        cloudflare_allowed_emails=('operator@example.com',) if access else ())
    return Settings(tmp_path / 'hub.sqlite3', AuthSettings('fixture-hermes', 'fixture-codex',
        claude_token='fixture-claude'), auth)


@pytest.mark.asyncio
async def test_preview_exception_is_exact_host_browser_only_and_requires_active_frontend(tmp_path, monkeypatch):
    calls = []
    async def preview(scope, receive, send):
        calls.append(scope['path'])
        await JSONResponse({'isolated_preview': True})(scope, receive, send)
    module = StagingModule(status={'state': 'synthetic'}, preview_app=preview,
                          preview_broker=SimpleNamespace(preview_origin=PREVIEW))
    monkeypatch.setattr('engineering_hub.staging_module.compose_staging', lambda *args, **kwargs: module)
    monkeypatch.setattr('engineering_hub.staging_module.configure_preview', lambda *args, **kwargs: None)
    core = create_app(settings(tmp_path, access=True), event_feed=Feed())
    pair = HubFrontends(core)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.browser), base_url=PREVIEW) as browser, \
               httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents), base_url=PREVIEW) as agent:
        assert (await browser.get('/p/fixture/index.html')).status_code == 503
        async with pair.lifespan():
            assert (await browser.get('/p/fixture/index.html')).status_code == 200
            assert (await agent.get('/p/fixture/index.html', headers={'Authorization': 'Bearer fixture-codex'})).status_code == 403
            for headers in ({'Host': 'hub.example'}, {'Host': 'preview.example.evil'},
                            {'Host': 'hub.example', 'X-Forwarded-Host': 'preview.example'},
                            [('host', 'preview.example'), ('host', 'hub.example')]):
                assert (await browser.get('/p/fixture/index.html', headers=headers)).status_code == 401
            assert calls == ['/p/fixture/index.html']
        assert (await browser.get('/p/fixture/index.html')).status_code == 503


@pytest.mark.parametrize('provider', ['github', 'cloudflare_access'])
def test_preview_session_hash_tracks_actual_session_expiry_and_logout(tmp_path, provider):
    now = [1000.0]
    store = SessionStore(tmp_path / 'auth.sqlite3', session_ttl=100, clock=lambda: now[0])
    if provider == 'github':
        actor = 'human:1'
        session = store.create_session(1)
        delete = store.delete_session
    else:
        config = AuthConfig(HUB, human_auth_mode='cloudflare_access',
            cloudflare_issuer='https://fixture-team.cloudflareaccess.com', cloudflare_audience='fixture',
            cloudflare_allowed_emails=('operator@example.com',))
        actor = 'human:cf:' + 'a' * 64
        identity = CloudflareIdentity('fixture-subject', 'operator@example.com', actor, 5000)
        session = store.create_cloudflare_session(config, identity)
        delete = store.delete_cloudflare_session
    token = session['cookie_token']
    digest, expires = store.preview_session_binding(actor, token)
    assert digest == hashlib.sha256(token.encode()).hexdigest() and expires == 1100
    assert token not in digest and store.preview_session_alive(actor, digest) is True
    assert store.preview_session_alive('human:2', digest) is False
    reopened = SessionStore(store.path, clock=lambda: now[0])
    assert reopened.preview_session_alive(actor, digest) is True
    now[0] = expires
    assert reopened.preview_session_alive(actor, digest) is False
    now[0] = 1000
    delete(token)
    assert reopened.preview_session_alive(actor, digest) is False


def test_cloudflare_identity_pin_removal_revokes_preview_session_even_if_cookie_row_remains(tmp_path):
    store = SessionStore(tmp_path / 'auth.sqlite3', clock=lambda: 1000)
    config = AuthConfig(HUB, human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://fixture-team.cloudflareaccess.com', cloudflare_audience='fixture',
        cloudflare_allowed_emails=('operator@example.com',))
    identity = CloudflareIdentity('fixture-subject', 'operator@example.com', 'human:cf:' + 'a' * 64, 5000)
    session = store.create_cloudflare_session(config, identity)
    digest, _ = store.preview_session_binding(identity.actor, session['cookie_token'])
    with store._db() as db:
        db.execute('DELETE FROM auth_access_identities')
        assert db.execute('SELECT count(*) FROM auth_access_sessions').fetchone()[0] == 1
    assert store.preview_session_alive(identity.actor, digest) is False


def test_preview_rejects_same_browser_origin_with_explicit_default_https_port(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    resources = StagingResourceStore(repo)
    with pytest.raises(ValueError):
        PreviewBroker(repo, resources, hub_origin=HUB, preview_origin=HUB + ':443',
                      session_alive=lambda actor, digest: True)


@pytest.mark.parametrize('hashed', [None, '', 'not-hashed', 'A' * 64, 'a' * 64 + '\n'])
def test_preview_session_rejects_invalid_digest_inputs(tmp_path, hashed):
    store = SessionStore(tmp_path / 'auth.sqlite3')
    assert store.preview_session_alive('human:1', hashed) is False


def test_failed_optional_client_factory_leaves_existing_queue_policies_and_actions_intact(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    prior = lambda *args: False
    queue = WorkQueue(repo, {'existing': WorkAction('machine-fixture', project='engineering-hub')},
        target_verifier=prior, claim_verifier=prior, completion_verifier=prior,
        reconciliation_verifier=prior)
    path = tmp_path / 'staging-service.json'
    path.write_text(json.dumps(dict(schema=1, origin='http://127.0.0.1:8780',
        credential_file=str(tmp_path / 'never-read-token'), enabled=True,
        projects={'engineering-hub': {'recipes': ['static']}})))
    path.chmod(0o600)
    before = dict(queue.actions)
    def failed(config):
        raise RuntimeError('PRIVATE credential/config failure')
    result = compose_staging(repo, path=path, machine_id='machine-fixture', queue=queue,
                             client_factory=failed)
    assert result.controller is None and result.status == {'state': 'unavailable', 'enabled': False}
    assert 'PRIVATE' not in str(result.status)
    assert queue.actions == before
    assert all(getattr(queue, key) is prior for key in (
        'target_verifier', 'claim_verifier', 'completion_verifier', 'reconciliation_verifier'))


@pytest.mark.asyncio
async def test_staging_close_failure_still_closes_native_feed(tmp_path, monkeypatch):
    class Controller:
        async def close(self):
            raise RuntimeError('synthetic controller shutdown failure')
    module = StagingModule(controller=Controller(), config=SimpleNamespace(auto_run=False), status={})
    # API/MCP captures the explicit controller but performs no request in this test.
    module.controller.queue = None
    monkeypatch.setattr('engineering_hub.staging_module.compose_staging', lambda *args, **kwargs: module)
    monkeypatch.setattr('engineering_hub.staging_module.configure_preview', lambda *args, **kwargs: None)
    feed = Feed()
    core = create_app(settings(tmp_path), event_feed=feed)
    with pytest.raises(RuntimeError, match='shutdown failure'):
        async with core.router.lifespan_context(core):
            assert feed.starts == 1
    assert feed.stops == 1 and feed.available is False
