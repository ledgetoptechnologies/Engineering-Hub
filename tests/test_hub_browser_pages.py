import pytest
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from engineering_hub.browser_pages import client_page_allowed, attach_browser_page_routes
from engineering_hub.private_gateway import browser_route_allowed


@pytest.mark.parametrize('path', ['/', '/inbox', '/work', '/planning', '/projects',
    '/projects/treedar', '/projects/client-notes', '/projects/unknown',
    '/projects/engineering-hub/plans', '/projects/treedar/activity',
    '/projects/treedar/labels/crowns', '/notes/runbooks/check.md', '/agents',
    '/agents/topics/auto-wake-v1-test', '/staging'])
def test_closed_client_paths(path):
    assert client_page_allowed('GET', path)
    assert client_page_allowed('HEAD', path)
    assert browser_route_allowed('GET', path)
    assert not client_page_allowed('POST', path)


@pytest.mark.parametrize('path', ['/api/hub/v1/tasks', '/assets/main.js', '/settings.json',
    '/projects/INVALID', '/projects/with_underscore', '/projects/treedar/unknown', '/projects/treedar/',
    '/notes/../settings.json', '/notes/%2e%2e/settings.json', '/notes//test',
    '/agents/topics/a/b', '/agents/topics/a?secret=x', '/notes/ü', '/notes/a\\b'])
def test_unknown_paths_not_spa(path):
    assert not client_page_allowed('GET', path)


def test_fixed_index_auth_assets_head_and_no_store(tmp_path):
    (tmp_path / 'index.html').write_text('PACKAGED_INDEX', encoding='utf-8')
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'app.js').write_text('FIXED_ASSET', encoding='utf-8')
    app = FastAPI()
    calls = []
    def human(request):
        calls.append(request.url.path)
        if request.headers.get('x-fixture-session') != 'yes':
            raise HTTPException(401)
    attach_browser_page_routes(app, ui_directory=tmp_path, human=human)
    app.mount('/', StaticFiles(directory=tmp_path, html=True))
    with TestClient(app) as client:
        assert client.get('/projects/treedar/notes').status_code == 401
        response = client.get('/projects/treedar/notes', headers={'x-fixture-session': 'yes'})
        assert response.text == 'PACKAGED_INDEX'
        assert response.headers['cache-control'] == 'no-store'
        assert client.head('/projects/treedar', headers={'x-fixture-session': 'yes'}).content == b''
        assert client.get('/assets/app.js').text == 'FIXED_ASSET'
        assert client.get('/api/unknown').status_code == 404
        # Project membership is resolved by the authenticated project API, not
        # by a second hard-coded page-route registry. The shell exposes no data.
        assert client.get('/projects/unknown').status_code == 401
        assert client.get('/projects/unknown', headers={'x-fixture-session': 'yes'}).text == 'PACKAGED_INDEX'
        assert client.get('/notes/a%2Fb', headers={'x-fixture-session': 'yes'}).status_code == 404


def test_bootstrap_shell_requires_ingress_proof_not_assertion_or_state(tmp_path):
    from engineering_hub.cloudflare_access import CloudflareAccessConfig
    (tmp_path / 'index.html').write_text('PACKAGED_INDEX', encoding='utf-8')
    app = FastAPI()
    config = CloudflareAccessConfig('https://fixture.cloudflareaccess.com',
                                   'fixture-audience', ('beau@example.com',))

    @app.middleware('http')
    async def unverified_identity(request, call_next):
        request.state.cloudflare_identity = {'actor': 'human:forged'}
        return await call_next(request)

    def human(request):
        raise HTTPException(401)

    attach_browser_page_routes(app, ui_directory=tmp_path, human=human,
                               verified_access_config=config)
    with TestClient(app) as client:
        for path in ('/', '/index.html'):
            assert client.get(path).status_code == 401
            assert client.get(path, headers={
                'CF-Access-Jwt-Assertion': 'not-verified',
                'CF-Access-Authenticated-User-Email': 'beau@example.com',
            }).status_code == 401
