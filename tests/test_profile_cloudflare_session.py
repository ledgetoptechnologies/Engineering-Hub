import engineering_hub.ingress
from tests.test_hub_cloudflare_auth import hub, sign_in
from tests.test_hub_api import BASE, COMMIT

def test_cloudflare_profile_get_without_csrf_and_expiry(hub, monkeypatch):
    from engineering_hub.staging_resources import StagingResourceStore
    from tests.test_staging_profile_task_binding import PROFILE
    client, app, config, assertion, _ = hub
    headers, identity = sign_in(client, assertion())
    actor = identity['actor']
    repo = app.state.repository
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}))
    store.set_policy('human:1', '3d-viewer', 0, {'codex':['read'], actor:['read']})
    store.enroll_profile('human:1', '3d-viewer', 'static', 0, PROFILE)
    task = repo.create_task('human:1', '3d-viewer', 'Profile review', 'profile-cf-expiry')
    task = repo.claim_task('codex', task['id'], task['revision'])
    repo.set_candidate('codex', task['id'], task['revision'], COMMIT, {'recipe':'static','generation':1})
    path = f"{BASE}/tasks/{task['id']}/staging-profile"
    access_only = {'Cf-Access-Jwt-Assertion':headers['Cf-Access-Jwt-Assertion']}
    assert client.get(path, headers=access_only).status_code == 200
    clock, original = app.state.sessions.clock, repo._staging_profile_read
    def expire(db, principal, project):
        original(db, principal, project)
        app.state.sessions.clock = lambda: clock() + 86400*1000
    monkeypatch.setattr(repo, '_staging_profile_read', expire)
    assert client.get(path, headers=access_only).status_code == 401

import pytest

@pytest.mark.asyncio
async def test_bound_github_cloudflare_profile_get_without_csrf_and_expiry(tmp_path, monkeypatch):
    import json, time
    from urllib.parse import parse_qs, urlsplit
    import httpx, jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from engineering_hub.cloudflare_access import CloudflareAccessVerifier
    from engineering_hub.frontends import HubFrontends
    from engineering_hub.staging_resources import StagingResourceStore
    from tests.test_hub_frontends import Feed
    from tests.test_staging_profile_task_binding import PROFILE
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
    token = jwt.encode({'iss':config.cloudflare_issuer, 'aud':['aud'], 'sub':'subject', 'email':'owner@example.com',
                        'type':'app', 'iat':now-1, 'exp':now+120}, key, algorithm='RS256', headers={'kid':'fixture'})
    async with pair.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.browser), base_url=config.origin, follow_redirects=False) as client:
            headers = {'CF-Access-Jwt-Assertion':token}
            login = await client.get('/api/hub/v1/auth/login', headers=headers)
            state = parse_qs(urlsplit(login.headers['location']).query)['state'][0]
            assert (await client.get('/api/hub/v1/auth/callback', params={'state':state,'code':'ok'}, headers=headers)).status_code == 303
            actor = (await client.get('/api/hub/v1/auth/me', headers=headers)).json()['actor']
            repo = core.state.repository
            store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}))
            store.set_policy('human:1', '3d-viewer', 0, {'codex':['read'], actor:['read']})
            store.enroll_profile('human:1', '3d-viewer', 'static', 0, PROFILE)
            task = repo.create_task('human:1', '3d-viewer', 'Profile review', 'profile-bound-expiry')
            task = repo.claim_task('codex', task['id'], task['revision'])
            repo.set_candidate('codex', task['id'], task['revision'], COMMIT, {'recipe':'static','generation':1})
            path = f"{BASE}/tasks/{task['id']}/staging-profile"
            assert (await client.get(path, headers=headers)).status_code == 200
            clock, original = core.state.sessions.clock, repo._staging_profile_read
            def expire(db, principal, project):
                original(db, principal, project)
                core.state.sessions.clock = lambda: clock() + 86400*1000
            monkeypatch.setattr(repo, '_staging_profile_read', expire)
            assert (await client.get(path, headers=headers)).status_code == 401

