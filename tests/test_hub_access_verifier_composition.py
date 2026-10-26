"""Trusted composition seam: real signatures/bootstrap; no external Access calls."""
from dataclasses import replace
import secrets

import httpx
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.cloudflare_access import CloudflareAccessConfig, CloudflareAccessVerifier
from tests.test_hub_cloudflare_access import ISSUER, AUD, EMAILS, keys, jwk, token, verifier


def settings(tmp_path):
    return Settings(tmp_path / 'hub.sqlite3',
        AuthSettings(secrets.token_urlsafe(32), secrets.token_urlsafe(32)),
        AuthConfig('https://hub.example.invalid', human_auth_mode='cloudflare_access',
            cloudflare_issuer=ISSUER, cloudflare_audience=AUD,
            cloudflare_allowed_emails=EMAILS))


@pytest.mark.asyncio
async def test_bound_real_verifier_bootstrap_and_csrf(tmp_path, keys):
    config = settings(tmp_path)
    access, requests = verifier({'keys': [jwk(keys[0])]})
    app = create_app(config, access_verifier=access)
    headers = {'CF-Access-Jwt-Assertion': token(keys[0]), 'Origin': config.auth_config.origin}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url=config.auth_config.origin) as client:
        assert (await client.post('/api/hub/v1/auth/bootstrap')).status_code == 401
        invalid = {**headers, 'CF-Access-Jwt-Assertion': token(keys[0], changes={'aud': 'wrong'})}
        assert (await client.post('/api/hub/v1/auth/bootstrap', headers=invalid)).status_code == 401
        response = await client.post('/api/hub/v1/auth/bootstrap', headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body['authenticated'] is True and body['csrf_token'] and client.cookies
        assert len(requests) == 1
        assert (await client.post('/api/hub/v1/auth/logout', headers=headers)).status_code == 403
        response = await client.post('/api/hub/v1/auth/logout',
            headers={**headers, 'X-Hub-CSRF': body['csrf_token']})
        assert response.status_code == 200 and response.json()['authenticated'] is False


@pytest.mark.parametrize('bad', ['callback', 'subclass', 'wrong-config', 'wrong-ingress', 'wrong-mode'])
def test_unbound_verifier_refused_before_creating_state(tmp_path, bad):
    config = settings(tmp_path)
    access = CloudflareAccessVerifier(config.auth_config.cloudflare_config)
    if bad == 'callback':
        access = lambda _: True
    elif bad == 'subclass':
        class Substitute(CloudflareAccessVerifier):
            pass
        access = Substitute(config.auth_config.cloudflare_config)
    elif bad == 'wrong-config':
        access = CloudflareAccessVerifier(CloudflareAccessConfig(ISSUER, 'other', EMAILS))
    elif bad == 'wrong-ingress':
        config = replace(config, ingress_mode='agents')
    else:
        config = replace(config, auth_config=None)
    with pytest.raises(ValueError, match='explicit_access_verifier_binding_required'):
        create_app(config, access_verifier=access)
    assert list(tmp_path.iterdir()) == []
