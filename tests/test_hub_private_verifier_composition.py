"""Real private UDS + public TCP auth composition; only provider JWKS is simulated."""
import asyncio
import sys

import httpx
import pytest

from engineering_hub.app import create_app
from engineering_hub.cloudflare_access import CloudflareAccessConfig, CloudflareAccessVerifier
from engineering_hub.frontends import HubFrontends
from engineering_hub.ingress import HubIngressMiddleware
from engineering_hub.private_gateway import PrivateBrowserGateway
from tests.test_hub_access_verifier_composition import settings
from tests.test_hub_cloudflare_access import ISSUER, EMAILS, keys, jwk, token, verifier


@pytest.mark.parametrize('boundary', ['frontends', 'gateway', 'ingress'])
@pytest.mark.parametrize('bad', ['callback', 'subclass', 'config'])
def test_private_verifier_binding_refused_before_wrapper_ownership(tmp_path, boundary, bad):
    config = settings(tmp_path)
    core = create_app(config)
    binding = config.auth_config.cloudflare_config
    if bad == 'callback':
        access = lambda _: True
    elif bad == 'subclass':
        class Substitute(CloudflareAccessVerifier):
            pass
        access = Substitute(binding)
    else:
        access = CloudflareAccessVerifier(CloudflareAccessConfig(ISSUER, 'other', EMAILS))
    with pytest.raises(ValueError, match='explicit_access_verifier_binding_required'):
        if boundary == 'frontends':
            HubFrontends(core, access_verifier=access)
        elif boundary == 'gateway':
            PrivateBrowserGateway(core, agent_auth=config.agent_auth,
                cloudflare_config=binding, access_verifier=access)
        else:
            HubIngressMiddleware(core, mode='browser', agent_auth=config.agent_auth,
                cloudflare_config=binding, access_verifier=access)
    assert getattr(core.state, 'hub_frontends', None) is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux private UDS/listeners')
async def test_real_private_uds_and_public_listener_keep_signature_session_csrf(tmp_path, keys):
    from engineering_hub.private_browser_socket import create_private_browser_client
    from engineering_hub import split_server as runner

    config = settings(tmp_path)
    access, requests = verifier({'keys': [jwk(keys[0])]})
    core = create_app(config, access_verifier=access)
    pair = HubFrontends(core, access_verifier=access)
    assert pair.browser.ingress.checker is access
    directory = tmp_path / 'socket'
    directory.mkdir(mode=0o700)
    endpoint = directory / 'browser.sock'
    private_stop, public_stop = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    private_ready, public_ready = loop.create_future(), loop.create_future()
    private = asyncio.create_task(runner.serve_private_coordinator(
        lambda: pair, socket_path=endpoint, agent_host='127.0.0.1', agent_port=0,
        stop_event=private_stop, install_signals=False,
        on_ready=lambda address, path: private_ready.set_result(path)))
    public = None
    try:
        await asyncio.wait_for(asyncio.shield(private_ready), 8)
        public = asyncio.create_task(runner.serve_public_browser(
            lambda: create_private_browser_client(endpoint), browser_port=0,
            stop_event=public_stop, install_signals=False,
            on_ready=lambda address: public_ready.set_result(address)))
        address = await asyncio.wait_for(asyncio.shield(public_ready), 8)
        # Connect to actual TCP/UDS; the explicit Host/Origin are the browser's
        # configured HTTPS origin. Cookie is sent explicitly over this disposable
        # HTTP test transport; production still emits a Secure session cookie.
        origin = config.auth_config.origin
        headers = {'Host': httpx.URL(origin).netloc.decode(), 'Origin': origin,
                   'CF-Access-Jwt-Assertion': token(keys[0])}
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{address[1]}',
                trust_env=False, follow_redirects=False) as browser:
            path = '/api/hub/v1'
            assert (await browser.post(path + '/auth/bootstrap')).status_code == 401
            for assertion in [token(keys[1]), token(keys[0], changes={'iss': 'https://other.cloudflareaccess.com'}),
                    token(keys[0], changes={'aud': 'other'}),
                    token(keys[0], changes={'email': 'outsider@example.com'}),
                    token(keys[0], changes={'exp': 1})]:
                assert (await browser.post(path + '/auth/bootstrap',
                    headers={**headers, 'CF-Access-Jwt-Assertion': assertion})).status_code == 401
            assert (await browser.post(path + '/auth/bootstrap',
                headers={**headers, 'Origin': 'https://other.example'})).status_code == 403
            response = await browser.post(path + '/auth/bootstrap', headers=headers)
            assert response.status_code == 200, response.text
            assert 'Secure' in response.headers['set-cookie']
            body = response.json()
            assert body['authenticated'] and body['csrf_token']
            cookie = browser.cookies.get(config.auth_config.cookie_name)
            human = {**headers, 'Cookie': config.auth_config.cookie_name + '=' + cookie}
            assert (await browser.get(path + '/auth/me', headers=human)).json()['authenticated']
            # A real session never replaces the per-request Access assertion.
            assert (await browser.get(path + '/auth/me',
                headers={'Host': headers['Host'], 'Cookie': human['Cookie']})).status_code == 401
            assert (await browser.post(path + '/auth/logout', headers=human)).status_code == 403
            assert (await browser.post(path + '/auth/logout',
                headers={**human, 'X-Hub-CSRF': body['csrf_token']})).status_code == 200
        assert len(requests) == 1
        assert str(requests[0].url) == ISSUER + '/cdn-cgi/access/certs'
    finally:
        if public is not None:
            public_stop.set()
            await asyncio.wait_for(public, 8)
        private_stop.set()
        await asyncio.wait_for(private, 8)
    assert pair.state == 'closed' and not endpoint.exists()
