import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from engineering_hub.auth import AuthConfig, SessionStore, human_transaction_guard
from engineering_hub.bound_oauth_draft import BoundOAuthDraftStore
from engineering_hub.cloudflare_access import CloudflareIdentity, _record_verified_access
from engineering_hub.repository import HubRepository


def fixture(tmp_path, mode):
    repo = HubRepository(tmp_path / 'hub.db')
    store = SessionStore(repo.path, repository=repo)
    options = {'beau_github_id': 42}
    if mode != 'github':
        options.update(human_auth_mode=mode, cloudflare_issuer='https://fixture.cloudflareaccess.com',
                       cloudflare_audience='hub', cloudflare_allowed_emails=('owner@example.com',))
    if mode == 'github_cloudflare':
        options.update(github_client_id='fixture', github_client_secret='fixture', bound_access_subject='subject')
    config = AuthConfig('https://hub.test', **options)
    identity = CloudflareIdentity('subject', 'owner@example.com', 'human:cf:' + 'a' * 64, int(time.time()) + 300)
    if mode == 'github':
        session = store.create_session(42)
        token, csrf, actor = session['cookie_token'], session['csrf_token'], 'human:42'
        revoke = lambda: store.delete_session(token)
    elif mode == 'cloudflare_access':
        session = store.create_cloudflare_session(config, identity)
        token, csrf, actor = session['cookie_token'], session['csrf_token'], identity.actor
        revoke = lambda: store.delete_cloudflare_session(token)
    else:
        bound = BoundOAuthDraftStore(store)
        token, csrf = bound.create_session(config.cloudflare_issuer, config.cloudflare_audience, identity.subject, 42, 42)
        actor = identity.actor
        revoke = lambda: bound.delete_session(token)
    headers = [(b'origin', config.origin.encode()), (b'x-hub-csrf', csrf.encode()),
               (b'cookie', f'{config.cookie_name}={token}'.encode())]
    scope = {'type': 'http', 'method': 'POST', 'path': '/', 'headers': headers, 'state': {}}
    if mode == 'cloudflare_access':
        scope['state']['cloudflare_identity'] = identity
    elif mode == 'github_cloudflare':
        headers.append((b'cf-access-jwt-assertion', b'synthetic'))
        _record_verified_access(scope, config.cloudflare_config, identity)
    return repo, store, config, Request(scope), actor, revoke


@pytest.mark.parametrize('mode', ['github', 'cloudflare_access', 'github_cloudflare'])
@pytest.mark.parametrize('end', ['logout', 'expire'])
def test_exact_human_session_is_required_at_precommit(tmp_path, mode, end):
    repo, store, config, request, actor, revoke = fixture(tmp_path, mode)
    guard = human_transaction_guard(request, config, store, actor)
    with repo._transaction(before_commit=guard) as db:
        db.execute('CREATE TABLE test_admission(value INTEGER)')
    if end == 'logout':
        revoke()
    else:
        table = {'github': 'auth_sessions', 'cloudflare_access': 'auth_access_sessions',
                 'github_cloudflare': 'auth_bound_oauth_sessions'}[mode]
        with store._db() as db:
            db.execute(f'UPDATE {table} SET expires=0')
    with pytest.raises(HTTPException) as error:
        with repo._transaction(before_commit=guard) as db:
            db.execute('INSERT INTO test_admission VALUES (1)')
    assert error.value.status_code == 401
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM test_admission').fetchone()[0] == 0


def test_logout_serializes_after_admitted_writer(tmp_path):
    repo, store, config, request, actor, revoke = fixture(tmp_path, 'github')
    guard = human_transaction_guard(request, config, store, actor)
    started, finished = Event(), Event()
    def logout():
        started.set()
        revoke()
        finished.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with repo._transaction(before_commit=guard) as db:
            db.execute('CREATE TABLE admission(value INTEGER)')
            future = executor.submit(logout)
            assert started.wait(2)
            assert not finished.wait(0.05)
        future.result(timeout=2)
    assert finished.is_set()
    with pytest.raises(HTTPException):
        with repo._transaction(before_commit=guard):
            pass


def test_new_session_for_same_actor_does_not_revive_old_guard(tmp_path):
    repo, store, config, request, actor, revoke = fixture(tmp_path, 'github')
    guard = human_transaction_guard(request, config, store, actor)
    revoke()
    store.create_session(42)
    with pytest.raises(HTTPException):
        with repo._transaction(before_commit=guard):
            pass


@pytest.mark.parametrize('mode', ['cloudflare_access', 'github_cloudflare'])
def test_access_assertion_expiry_is_rechecked_before_commit(tmp_path, mode, monkeypatch):
    repo, store, config, request, actor, _ = fixture(tmp_path, mode)
    guard = human_transaction_guard(request, config, store, actor)
    # Access-only uses the injected session clock; the bound Access verifier
    # independently uses the current wall clock for its original assertion.
    if mode == 'cloudflare_access':
        store.clock = lambda: time.time() + 301
    else:
        future = time.time() + 301
        monkeypatch.setattr('engineering_hub.cloudflare_access.time.time', lambda: future)
    with pytest.raises(HTTPException):
        with repo._transaction(before_commit=guard):
            pass


@pytest.mark.parametrize('mode', ['cloudflare_access', 'github_cloudflare'])
def test_access_binding_is_rechecked_before_commit(tmp_path, mode):
    repo, store, config, request, actor, _ = fixture(tmp_path, mode)
    guard = human_transaction_guard(request, config, store, actor)
    table = 'auth_access_sessions' if mode == 'cloudflare_access' else 'auth_bound_oauth_sessions'
    with store._db() as db:
        db.execute(f"UPDATE {table} SET binding=?", ('b' * 64,))
    with pytest.raises(HTTPException):
        with repo._transaction(before_commit=guard):
            pass
