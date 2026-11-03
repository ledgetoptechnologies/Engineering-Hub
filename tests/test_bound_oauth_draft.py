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

@pytest.mark.asyncio
async def test_bound_oauth_router_signed_access_identity(monkeypatch, tmp_path):
    import json, time
    from urllib.parse import parse_qs, urlsplit
    import httpx, jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi import FastAPI
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.auth import AuthConfig, SessionStore, build_auth_router
    from engineering_hub.cloudflare_access import CloudflareAccessVerifier
    from engineering_hub.ingress import HubIngressMiddleware
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='fixture', alg='RS256', use='sig')
    config = AuthConfig('https://hub.example', github_client_id='id', github_client_secret='secret',
        beau_github_id=123, human_auth_mode='github_cloudflare', cloudflare_issuer='https://fixture.cloudflareaccess.com',
        cloudflare_audience='aud', cloudflare_allowed_emails=('owner@example.com',), bound_access_subject='subject')
    checker = CloudflareAccessVerifier(config.cloudflare_config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'keys':[jwk]})))
    monkeypatch.setattr('engineering_hub.ingress.CloudflareAccessVerifier', lambda _: checker)
    class Provider:
        async def exchange(self, code, verifier):
            assert code == 'code' and verifier
            return 123
    core = FastAPI()
    core.include_router(build_auth_router(config, SessionStore(tmp_path/'hub.db'), Provider()))
    wrapped = HubIngressMiddleware(core, mode='browser', agent_auth=AuthSettings('h','c',claude_token='cl'), cloudflare_config=config.cloudflare_config)
    def token(subject):
        now = int(time.time())
        return jwt.encode({'iss':config.cloudflare_issuer,'aud':[config.cloudflare_audience], 'sub':subject,
            'email':'owner@example.com','type':'app','iat':now-1,'exp':now+120}, key, algorithm='RS256', headers={'kid':'fixture'})
    right, other = token('subject'), token('other')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core), base_url=config.origin) as direct:
        assert (await direct.get('/api/hub/v1/auth/login', headers={'CF-Access-Jwt-Assertion':right})).status_code == 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url=config.origin, follow_redirects=False) as c:
        login = await c.get('/api/hub/v1/auth/login', headers={'CF-Access-Jwt-Assertion':right})
        assert login.status_code == 302
        state = parse_qs(urlsplit(login.headers['location']).query)['state'][0]
        path = '/api/hub/v1/auth/callback'
        assert (await c.get(path, params={'state':state, 'code':'code'}, headers={'CF-Access-Jwt-Assertion':other})).status_code == 401
        assert (await c.get(path, params={'state':state, 'code':'code'}, headers={'CF-Access-Jwt-Assertion':right})).status_code == 303
        assert (await c.get('/api/hub/v1/auth/me', headers={'CF-Access-Jwt-Assertion':right})).json()['authenticated'] is True
        assert (await c.get('/api/hub/v1/auth/me', headers={'CF-Access-Jwt-Assertion':other})).json()['authenticated'] is False
        assert (await c.get(path, params={'state':state, 'code':'code'}, headers={'CF-Access-Jwt-Assertion':right})).status_code == 400

@pytest.mark.asyncio
async def test_full_hub_frontdoors_bound_signin_and_csrf(tmp_path):
    import json, time
    from urllib.parse import parse_qs, urlsplit
    import httpx, jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from engineering_hub.cloudflare_access import CloudflareAccessVerifier
    from engineering_hub.frontends import HubFrontends
    from tests.test_hub_frontends import Feed
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid='fixture', alg='RS256', use='sig')
    config = AuthConfig('https://hub.example', github_client_id='id', github_client_secret='secret', beau_github_id=123,
        human_auth_mode='github_cloudflare', cloudflare_issuer='https://fixture.cloudflareaccess.com',
        cloudflare_audience='aud', cloudflare_allowed_emails=('owner@example.com',), bound_access_subject='subject')
    checker = CloudflareAccessVerifier(config.cloudflare_config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'keys':[jwk]})))
    class Provider:
        async def exchange(self, code, verifier):
            return 123
    core = create_app(Settings(tmp_path/'hub.db', AuthSettings('h','c',claude_token='cl'), config), github_client=Provider(), event_feed=Feed())
    pair = HubFrontends(core, access_verifier=checker)
    now = int(time.time())
    def token(subject):
        return jwt.encode({'iss':config.cloudflare_issuer, 'aud':['aud'], 'sub':subject, 'email':'owner@example.com',
                           'type':'app', 'iat':now-1, 'exp':now+120}, key, algorithm='RS256', headers={'kid':'fixture'})
    good, bad = token('subject'), token('other')
    async with pair.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.browser), base_url=config.origin, follow_redirects=False) as c:
            headers = {'CF-Access-Jwt-Assertion':good}
            login = await c.get('/api/hub/v1/auth/login', headers=headers)
            assert login.status_code == 302
            state = parse_qs(urlsplit(login.headers['location']).query)['state'][0]
            assert (await c.get('/api/hub/v1/auth/callback', params={'state':state,'code':'ok'}, headers=headers)).status_code == 303
            me = (await c.get('/api/hub/v1/auth/me', headers=headers)).json()
            import hashlib
            expected_actor = 'human:cf:' + hashlib.sha256((config.cloudflare_issuer + '\0' + config.cloudflare_audience + '\0subject').encode()).hexdigest()
            assert me['authenticated'] and me['actor'] == expected_actor and me['github_id'] == 123
            assert (await c.post('/api/hub/v1/auth/logout', headers=headers)).status_code == 403
            assert (await c.get('/api/hub/v1/auth/me', headers={'CF-Access-Jwt-Assertion':bad})).json()['authenticated'] is False
            assert (await c.post('/api/hub/v1/auth/logout', headers={**headers,'Origin':config.origin,'X-Hub-CSRF':me['csrf_token']})).status_code == 200
            assert not (await c.get('/api/hub/v1/auth/me', headers=headers)).json()['authenticated']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.agents), base_url=config.origin) as agent:
            assert (await agent.get('/api/hub/v1/auth/me', headers={'Authorization':'Bearer c'})).status_code == 403
            assert (await agent.get('/api/hub/v1/tasks', headers={'Authorization':'Bearer c'})).status_code == 200


def test_deployment_v3_explicit_and_incomplete_denied(tmp_path, monkeypatch):
    import json
    from engineering_hub.deployment_config import load_deployment_config
    path = tmp_path/'settings.json'
    document = {'schema_version':3,'base_url':'https://hub.example',
        'cloudflare_access':{'issuer':'https://fixture.cloudflareaccess.com','audience':'aud','allowed_emails':['owner@example.com']},
        'agent_allowed_hosts':['localhost'], 'agent_allowed_ips':['127.0.0.1'],
        'human_auth':{'mode':'github_cloudflare','github_client_id':'id','github_id':123,'access_subject':'subject'}}
    path.write_text(json.dumps(document))
    monkeypatch.delenv('HUB_GITHUB_CLIENT_SECRET', raising=False)
    with pytest.raises(ValueError):
        load_deployment_config(path)
    monkeypatch.setenv('HUB_GITHUB_CLIENT_SECRET', 'fixture-secret')
    config = load_deployment_config(path)
    assert config.auth_config.human_auth_mode == 'github_cloudflare'
    assert config.auth_config.beau_github_id == 123
    document['human_auth']['unexpected'] = 'reject'
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_deployment_config(path)
