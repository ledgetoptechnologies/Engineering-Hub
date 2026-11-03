"""Linux real bus + real frontdoor + signed fixture owner integration."""
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.transport_security import TransportSecuritySettings
from agent_message_bus.app import create_app as create_bus
from agent_message_bus.auth import AuthSettings
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.cloudflare_access import CloudflareAccessVerifier
from engineering_hub.frontends import HubFrontends
from tests.test_hub_frontends import Feed


@pytest.mark.asyncio
async def test_real_bus_owner_pairing_with_bound_human(tmp_path):
    origin = 'https://hub.example'
    issuer = 'https://fixture.cloudflareaccess.com'
    audience, subject = 'aud', 'subject'
    owner = 'human:cf:' + hashlib.sha256((issuer+'\0'+audience+'\0'+subject).encode()).hexdigest()
    auth = AuthSettings('test-hermes','test-codex',claude_token='test-claude')
    sessions = AgentSessionsConfig((tmp_path/'connections.db').absolute(), owner, origin)
    bus = create_bus(db_path=tmp_path/'bus.db', audit_path=tmp_path/'audit.jsonl', file_root=tmp_path/'files',
        auth=auth, push_events_operational=False,
        transport_security_settings=TransportSecuritySettings(allowed_hosts=['hub.example'],allowed_origins=[origin]),
        agent_sessions_config=sessions)
    config = AuthConfig(origin,github_client_id='fixture',github_client_secret='fixture',beau_github_id=123,
        human_auth_mode='github_cloudflare',cloudflare_issuer=issuer,cloudflare_audience=audience,
        cloudflare_allowed_emails=('owner@example.com',),bound_access_subject=subject)
    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())); jwk.update(kid='fixture',alg='RS256',use='sig')
    checker = CloudflareAccessVerifier(config.cloudflare_config,transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'keys':[jwk]})))
    class Github:
        async def exchange(self, code, verifier): return 123
    core = create_app(Settings(tmp_path/'hub.db',auth,config),bus_app=bus,event_feed=Feed(),github_client=Github())
    pair = HubFrontends(core,access_verifier=checker)
    now = int(time.time())
    assertion = jwt.encode({'iss':issuer,'aud':[audience],'sub':subject,'email':'owner@example.com','type':'app','iat':now-1,'exp':now+120},key,algorithm='RS256',headers={'kid':'fixture'})
    headers = {'CF-Access-Jwt-Assertion':assertion}
    async with pair.lifespan():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pair.browser),base_url=origin,follow_redirects=False) as client:
            login = await client.get('/api/hub/v1/auth/login',headers=headers)
            state = parse_qs(urlsplit(login.headers['location']).query)['state'][0]
            assert (await client.get('/api/hub/v1/auth/callback',params={'state':state,'code':'ok'},headers=headers)).status_code == 303
            me = (await client.get('/api/hub/v1/auth/me',headers=headers)).json()
            assert me['actor'] == owner
            cap = await client.get('/api/hub/v1/agent-connections/capabilities',headers=headers)
            assert cap.status_code == 200 and cap.json()['owner'] is True
            secret='ehi_'+'A'*43
            record=bus.state.agent_sessions.request_enrollment('codex','fixture','fixture',hashlib.sha256(secret.encode()).hexdigest())
            path='/api/hub/v1/agent-connections/installations/'+record['id']+'/approve'
            approved=await client.post(path,headers={**headers,'Origin':origin,'X-Hub-CSRF':me['csrf_token']},json={'expected_revision':record['revision']})
            assert approved.status_code == 200 and approved.json()['state'] == 'active'
