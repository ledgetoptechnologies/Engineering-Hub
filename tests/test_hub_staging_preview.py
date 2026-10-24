"""Capability/SQLite/ASGI boundaries; browser execution is a separate gate."""
import hashlib
import json
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from engineering_hub.repository import HubError, HubRepository
from engineering_hub.staging_resources import StagingResourceStore
from engineering_hub.staging_preview import PreviewBroker


ACTOR = 'human:1'
SESSION = hashlib.sha256(b'fixture-session-not-a-real-cookie').hexdigest()
HUB = 'https://hub.example'
PREVIEW = 'https://preview.example'


@pytest.fixture
def env(tmp_path):
    now = [1800000000.0]
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    control = object()
    resources = StagingResourceStore(repo, admin_actors=frozenset({ACTOR}),
        control_principal=control, clock=lambda: now[0])
    resources.set_policy(ACTOR, 'engineering-hub', 0, {ACTOR: ['read', 'preview', 'cleanup', 'retain']})
    job = resources.bind_job(control, str(uuid4()), project='engineering-hub',
        created_by='codex', source_commit='a' * 40, artifact_digest='sha256:' + 'b' * 64)
    item = resources.register_resource(control, job['id'], 'preview')
    resources.mark_resource_ready(control, item['id'], item['revision'],
        preview_path='/staging/previews/fixture/', size_bytes=100)
    resources.update_job(control, job['id'], job['revision'], terminal=True)
    live = [True]
    checks = []

    def session_alive(actor, session_hash):
        checks.append((actor, session_hash))
        return live[0] and actor == ACTOR and session_hash == SESSION

    broker = PreviewBroker(repo, resources, hub_origin=HUB, preview_origin=PREVIEW,
        session_alive=session_alive, clock=lambda: now[0])
    return broker, resources, repo, control, now, live, checks, item['id']


def issue(env, **kwargs):
    broker, _, _, _, now, _, _, resource_id = env
    return broker.issue(ACTOR, resource_id, SESSION, now[0] + 600, **kwargs)


async def get(env, url, *, fetch=None, method='GET', headers=None):
    broker = env[0]

    async def default_fetch(resource_id, path):
        assert resource_id == env[-1]
        return b'<html>fixture</html>', 'text/html'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=broker.app(fetch or default_fetch)),
                                 base_url=PREVIEW, follow_redirects=False) as client:
        return await client.request(method, url, headers=headers)


def test_issue_is_bounded_and_stores_only_hash_not_capability_or_outbox(env):
    before_audit = env[2].list_audit()
    launch = issue(env)
    token = urlsplit(launch['preview_url']).path.split('/')[2]
    assert len(token) == 43
    with env[2]._connection() as db:
        row = db.execute('SELECT token_hash,data FROM hub_staging_preview_tickets').fetchone()
        assert row['token_hash'] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in row['data'] and token not in row['token_hash']
        data = json.loads(row['data'])
        assert data['expires'] == env[4][0] + 300
        assert data['session_hash'] == SESSION and data['actor'] == ACTOR
        assert data['id'] == env[-1] and data['project'] == 'engineering-hub'
    assert env[2].list_audit() == before_audit


def test_human_only_issue_requires_exact_active_session_and_grants(env):
    broker, resources, _, _, now, live, _, resource_id = env
    for actor, hashed, expires in [('codex', SESSION, now[0] + 60),
                                    (ACTOR, 'raw-cookie', now[0] + 60),
                                    (ACTOR, SESSION, now[0]),
                                    (ACTOR, SESSION, float('nan'))]:
        with pytest.raises(HubError):
            broker.issue(actor, resource_id, hashed, expires)
    live[0] = False
    with pytest.raises(HubError):
        issue(env)
    live[0] = True
    resources.set_policy(ACTOR, 'engineering-hub', 1, {ACTOR: ['read']})
    with pytest.raises(HubError):
        issue(env)


@pytest.mark.asyncio
async def test_document_has_opaque_sandbox_cors_no_credentials_and_fixed_parent(env):
    launch = issue(env)
    response = await get(env, launch['preview_url'], headers={
        'Cookie': 'hub_session=must-not-be-forwarded', 'Authorization': 'Bearer not-a-worker-token',
        'Origin': 'null', 'Cf-Access-Jwt-Assertion': 'must-not-be-forwarded'})
    assert response.status_code == 200
    csp = response.headers['content-security-policy']
    assert csp.startswith('sandbox allow-scripts;')
    assert 'allow-same-origin' not in csp and 'allow-top-navigation' not in csp
    assert 'frame-ancestors ' + HUB in csp
    for directive in ('object-src', 'frame-src', 'worker-src', 'base-uri', 'form-action'):
        assert directive + " 'none'" in csp
    assert response.headers['access-control-allow-origin'] == '*'
    assert 'access-control-allow-credentials' not in response.headers
    assert 'set-cookie' not in response.headers
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert len(env[6]) == 3  # Issuance + before/after artifact read.


@pytest.mark.parametrize('name,mime,body', [
    ('assets/main.js', 'text/javascript', b'import "./other.js"'),
    ('assets/other.mjs', 'application/javascript', b'export default 1'),
    ('assets/main.css', 'text/css', b'body{}'),
    ('assets/font.woff2', 'font/woff2', b'fixture-font'),
    ('fixture.json', 'application/json', b'{"ok":true}'),
])
@pytest.mark.asyncio
async def test_static_module_font_and_json_assets_have_credentialless_cors(env, name, mime, body):
    launch = issue(env)['preview_url'].removesuffix('index.html') + name
    async def fetch(resource_id, path):
        assert path == name
        return body, mime
    response = await get(env, launch, fetch=fetch)
    assert response.status_code == 200 and response.content == body
    assert response.headers['access-control-allow-origin'] == '*'


@pytest.mark.parametrize('headers', [{}, {'Origin': 'null'}, {'Cookie': 'hub_session=fixture'},
                                    {'Authorization': 'Bearer fixture'}])
@pytest.mark.asyncio
async def test_other_credentials_never_grant_preview_without_ticket(env, headers):
    async def forbidden(*args):
        pytest.fail('Unauthenticated preview reached worker')
    response = await get(env, '/p/' + 'a' * 43 + '/index.html', headers=headers, fetch=forbidden)
    assert response.status_code == 404 and response.json() == {'detail': {'code': 'preview_unavailable'}}


@pytest.mark.parametrize('change', ['expired', 'revoked-session', 'revoked-policy', 'same-grant-new-policy',
                                    'resource-revision', 'artifact', 'missing-artifact', 'kind', 'project', 'job', 'deleting', 'deleted',
                                    'failed', 'provisioning', 'active-job', 'resource-expired', 'other-resource'])
@pytest.mark.asyncio
async def test_every_asset_rechecks_current_binding_and_permissions(env, change):
    launch = issue(env)['preview_url']
    broker, resources, repo, control, now, live, _, resource_id = env
    if change == 'expired':
        now[0] += 301
    elif change == 'revoked-session':
        live[0] = False
    elif change in ('revoked-policy', 'same-grant-new-policy'):
        resources.set_policy(ACTOR, 'engineering-hub', 1,
            {ACTOR: [] if change == 'revoked-policy' else ['read', 'preview', 'cleanup']})
    else:
        # Corrupted/stale authoritative fixtures must also fail closed; these
        # writes are test setup, not a production mutation API.
        with repo._transaction() as db:
            row = db.execute('SELECT data FROM hub_entities WHERE id=?', (resource_id,)).fetchone()
            item = json.loads(row['data'])
            if change == 'resource-revision':
                item['revision'] += 1
            elif change == 'artifact':
                item['artifact_digest'] = 'sha256:' + 'c' * 64
            elif change == 'missing-artifact':
                item['artifact_digest'] = None
            elif change == 'kind':
                item['kind'] = 'build'
            elif change == 'project':
                item['project'] = '3d-viewer'
            elif change == 'job':
                item['job_id'] = str(uuid4())
            elif change == 'active-job':
                item['protected'] = [{'reason': 'active_job', 'ref': item['job_id']}]
            elif change == 'resource-expired':
                item['expires_at'] = '2000-01-01T00:00:00+00:00'
            elif change == 'other-resource':
                item['id'] = str(uuid4())
            else:
                item['state'] = change
            db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), resource_id))
    async def forbidden(*args):
        pytest.fail('Revoked preview reached worker')
    assert (await get(env, launch, fetch=forbidden)).status_code == 404


@pytest.mark.asyncio
async def test_revocation_during_artifact_fetch_discards_returned_bytes(env):
    launch = issue(env)['preview_url']
    async def fetch(*args):
        env[5][0] = False
        return b'PRIVATE-BYTES', 'text/html'
    response = await get(env, launch, fetch=fetch)
    assert response.status_code == 404 and b'PRIVATE-BYTES' not in response.content


@pytest.mark.parametrize('suffix', ['../index.html', '%2e%2e/index.html', 'a//index.html',
    'a\\index.html', 'index.html?ticket=ignored', 'index.html%00', '%69ndex.html',
    'https://evil.example/index.html', 'index.html;evil', 'a' * 513 + '.html'])
@pytest.mark.asyncio
async def test_noncanonical_paths_and_query_credentials_deny_before_worker(env, suffix):
    launch = issue(env)['preview_url'].removesuffix('index.html') + suffix
    async def forbidden(*args):
        pytest.fail('Invalid preview path reached worker')
    assert (await get(env, launch, fetch=forbidden)).status_code == 404


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE', 'OPTIONS'])
@pytest.mark.asyncio
async def test_non_read_methods_deny(env, method):
    assert (await get(env, issue(env)['preview_url'], method=method)).status_code == 404


@pytest.mark.asyncio
async def test_exact_host_only_and_head_response(env):
    launch = issue(env)['preview_url']
    assert (await get(env, launch, headers={'Host': 'hub.example'})).status_code == 404
    assert (await get(env, launch, headers={'Host': 'preview.example.evil'})).status_code == 404
    head = await get(env, launch, method='HEAD')
    assert head.status_code == 200 and head.content == b''
    assert int(head.headers['content-length']) == len(b'<html>fixture</html>')


@pytest.mark.parametrize('content,mime', [(b'fixture', 'text/html;evil=1'),
    (b'fixture', 'application/javascript'), ('notbytes', 'text/html'),
    (b'x' * (8 * 1024**2 + 1), 'text/html')],
    ids=['mime-parameter', 'mime-confusion', 'not-bytes', 'oversized'])
@pytest.mark.asyncio
async def test_mime_confusion_and_unbounded_responses_fail_closed(env, content, mime):
    async def fetch(*args):
        return content, mime
    response = await get(env, issue(env)['preview_url'], fetch=fetch)
    assert response.status_code == 404
    assert 'location' not in response.headers and 'set-cookie' not in response.headers


@pytest.mark.asyncio
async def test_worker_errors_do_not_echo_urls_or_credentials(env):
    async def fetch(*args):
        raise RuntimeError('SECRET raw cookie and worker URL')
    response = await get(env, issue(env)['preview_url'], fetch=fetch)
    assert response.status_code == 404 and b'SECRET' not in response.content


def test_configuration_requires_separate_https_origin_and_bounded_limits(env):
    broker, resources, repo, _, _, _, _, _ = env
    for changes in ({'preview_origin': HUB}, {'preview_origin': 'http://preview.example'},
                    {'preview_origin': 'https://user:password@preview.example'},
                    {'preview_origin': 'https://preview.example/path'}, {'ttl': 301},
                    {'max_file_bytes': 8 * 1024**2 + 1}):
        args = dict(hub_origin=HUB, preview_origin=PREVIEW, session_alive=broker.session_alive)
        args.update(changes)
        with pytest.raises(ValueError):
            PreviewBroker(repo, resources, **args)


@pytest.mark.parametrize('hub,preview', [
    ('https://hub.example', 'https://hub.example:443'),
    ('https://hub.example:443', 'https://hub.example'),
    ('https://127.0.0.1', 'https://127.0.0.1:443'),
    ('https://[::1]', 'https://[0:0:0:0:0:0:0:1]:443'),
])
def test_default_port_or_ip_spelling_cannot_evade_origin_separation(env, hub, preview):
    with pytest.raises(ValueError):
        PreviewBroker(env[2], env[1], hub_origin=hub, preview_origin=preview,
                      session_alive=env[0].session_alive)


@pytest.mark.parametrize('origin', ['https://preview.example\\evil', 'https://preview.example;evil',
    'https://preview.example,evil', 'https://preview..example', 'https://-preview.example',
    'https://preview.example.', 'https://preview_example', 'https://2130706433',
    'https://127.1', 'https://0x7f000001', 'https://127.000.000.001',
    'https://[fe80::1%25eth0]', 'https://preview.example:0'])
def test_invalid_or_browser_ambiguous_preview_authorities_are_rejected(env, origin):
    with pytest.raises(ValueError):
        PreviewBroker(env[2], env[1], hub_origin=HUB, preview_origin=origin,
                      session_alive=env[0].session_alive)


@pytest.mark.asyncio
async def test_default_port_is_canonicalized_for_ticket_csp_and_host_dispatch(env):
    broker = PreviewBroker(env[2], env[1], hub_origin=HUB + ':443', preview_origin=PREVIEW + ':443',
        session_alive=env[0].session_alive, clock=lambda: env[4][0])
    env = (broker, *env[1:])
    launch = issue(env)['preview_url']
    assert launch.startswith(PREVIEW + '/p/')
    response = await get(env, launch)
    assert response.status_code == 200
    csp = response.headers['content-security-policy']
    assert 'frame-ancestors ' + HUB + ';' in csp and ':443' not in csp


@pytest.mark.parametrize('mode', ['pinned', 'until'])
@pytest.mark.asyncio
async def test_explicit_retention_allows_preview_after_original_expiry_without_extending_session(env, mode):
    from datetime import datetime, timezone

    broker, resources, repo, _, now, _, _, resource_id = env
    item = resources.get_resource(ACTOR, resource_id)
    original_expiry = datetime.fromisoformat(item['expires_at']).timestamp()
    until = datetime.fromtimestamp(original_expiry + 100, timezone.utc).isoformat() if mode == 'until' else None
    resources.set_keep(ACTOR, resource_id, mode, until=until, reason='Synthetic retention',
                       expected_revision=item['revision'])
    now[0] = original_expiry + 1
    session_expires = now[0] + 20
    launch = broker.issue(ACTOR, resource_id, SESSION, session_expires)
    assert datetime.fromisoformat(launch['expires_at']).timestamp() == session_expires
    assert (await get(env, launch['preview_url'])).status_code == 200
    now[0] = session_expires
    assert (await get(env, launch['preview_url'])).status_code == 404


@pytest.mark.asyncio
async def test_until_retention_caps_ticket_and_expired_keep_does_not_grant(env):
    from datetime import datetime, timezone

    broker, resources, _, _, now, _, _, resource_id = env
    item = resources.get_resource(ACTOR, resource_id)
    original_expiry = datetime.fromisoformat(item['expires_at']).timestamp()
    retained_until = original_expiry + 10
    resources.set_keep(ACTOR, resource_id, 'until',
        until=datetime.fromtimestamp(retained_until, timezone.utc).isoformat(),
        reason='Synthetic retention', expected_revision=item['revision'])
    now[0] = original_expiry + 1
    launch = issue(env)
    assert datetime.fromisoformat(launch['expires_at']).timestamp() == retained_until
    now[0] = retained_until
    assert (await get(env, launch['preview_url'])).status_code == 404
    with pytest.raises(HubError):
        issue(env)
