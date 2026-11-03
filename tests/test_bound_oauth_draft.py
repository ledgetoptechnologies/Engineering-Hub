"""Draft storage tests, NOT ingress or OAuth integration tests."""
import pytest
from engineering_hub.auth import SessionStore
from engineering_hub.bound_oauth_draft import BoundOAuthDraftStore


def test_state_binding_and_one_use(tmp_path):
    store = BoundOAuthDraftStore(SessionStore(tmp_path/'hub.db'))
    state, _ = store.create_state('https://issuer', 'aud', 'subject', 123)
    assert store.consume_state(state, 'https://issuer', 'aud', 'other', 123) is None
    assert store.consume_state(state, 'https://issuer', 'other', 'subject', 123) is None
    assert store.consume_state(state, 'https://issuer', 'aud', 'subject', 123)
    assert store.consume_state(state, 'https://issuer', 'aud', 'subject', 123) is None


def test_session_binding_revoke_and_expiry(tmp_path):
    now = [1000]
    backing = SessionStore(tmp_path/'hub.db', session_ttl=10, clock=lambda:now[0])
    store = BoundOAuthDraftStore(backing)
    with pytest.raises(ValueError):
        store.create_session('issuer', 'aud', 'subject', 123, 124)
    token, csrf = store.create_session('issuer', 'aud', 'subject', 123, 123)
    assert store.session_for(token, 'issuer', 'aud', 'subject', 123)['csrf_token'] == csrf
    for issuer, audience, subject, github in [('other','aud','subject',123),('issuer','other','subject',123),('issuer','aud','other',123),('issuer','aud','subject',124)]:
        assert store.session_for(token, issuer, audience, subject, github) is None
    now[0] += 10
    assert store.session_for(token, 'issuer', 'aud', 'subject', 123) is None
    store.delete_session(token)

@pytest.mark.asyncio
async def test_real_frontdoor_capability_rejects_direct_and_wrong_subject(monkeypatch):
    import time
    import httpx
    from fastapi import FastAPI, Request, HTTPException
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.cloudflare_access import CloudflareAccessConfig, CloudflareIdentity, AccessDenied
    from engineering_hub.ingress import HubIngressMiddleware
    from engineering_hub.bound_oauth_draft import require_enrolled_access
    config = CloudflareAccessConfig('https://hub-test.cloudflareaccess.com', 'aud', ('beau@example.com',))
    class Checker:
        def __init__(self, cfg):
            self.config = cfg
        async def verify(self, token):
            if token not in ('right', 'wrong'):
                raise AccessDenied()
            return CloudflareIdentity('subject' if token == 'right' else 'other', 'beau@example.com', 'actor', int(time.time()) + 60)
    monkeypatch.setattr('engineering_hub.ingress.CloudflareAccessVerifier', Checker)
    core = FastAPI()
    @core.get('/identity')
    def identity(request: Request):
        try:
            require_enrolled_access(request, config, 'subject')
        except PermissionError:
            raise HTTPException(403)
        return {'ok': True}
    wrapped = HubIngressMiddleware(core, mode='browser', agent_auth=AuthSettings('hermes', 'codex', claude_token='claude'), cloudflare_config=config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core), base_url='https://hub.test') as direct:
        assert (await direct.get('/identity', headers={'CF-Access-Jwt-Assertion':'right'})).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url='https://hub.test') as browser:
        assert (await browser.get('/identity')).status_code == 401
        assert (await browser.get('/identity', headers={'CF-Access-Jwt-Assertion':'wrong'})).status_code == 403
        assert (await browser.get('/identity', headers={'CF-Access-Jwt-Assertion':'right'})).status_code == 200
