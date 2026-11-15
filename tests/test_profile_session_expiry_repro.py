from tests.test_hub_api import hub, human_headers, BASE, COMMIT

def test_expired_during_profile_read(hub, monkeypatch):
    from engineering_hub.staging_resources import StagingResourceStore
    from tests.test_staging_profile_task_binding import PROFILE
    client, app, settings = hub
    repo = app.state.repository
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}))
    store.set_policy('human:1', '3d-viewer', 0, {'codex':['read'], 'human:42':['read']})
    store.enroll_profile('human:1', '3d-viewer', 'static', 0, PROFILE)
    task = repo.create_task('human:1', '3d-viewer', 'Profile review', 'profile-expiry')
    task = repo.claim_task('codex', task['id'], task['revision'])
    repo.set_candidate('codex', task['id'], task['revision'], COMMIT, {'recipe':'static','generation':1})
    human_headers(client, app, settings)
    original = repo._staging_profile_read
    clock = app.state.sessions.clock
    def expire(db, actor, project):
        original(db, actor, project)
        app.state.sessions.clock = lambda: clock() + 86400*1000
    monkeypatch.setattr(repo, '_staging_profile_read', expire)
    response = client.get(f"{BASE}/tasks/{task['id']}/staging-profile")
    assert response.status_code == 401, response.status_code

def test_logout_after_http_auth_before_profile_transaction(hub, monkeypatch):
    from engineering_hub.staging_resources import StagingResourceStore
    from tests.test_staging_profile_task_binding import PROFILE
    client, app, settings = hub
    repo = app.state.repository
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}))
    store.set_policy('human:1', '3d-viewer', 0, {'codex':['read'], 'human:42':['read']})
    store.enroll_profile('human:1', '3d-viewer', 'static', 0, PROFILE)
    task = repo.create_task('human:1', '3d-viewer', 'Profile review', 'profile-logout')
    task = repo.claim_task('codex', task['id'], task['revision'])
    repo.set_candidate('codex', task['id'], task['revision'], COMMIT, {'recipe':'static','generation':1})
    human_headers(client, app, settings)
    token = client.cookies.get(settings.auth_config.cookie_name)
    original = repo.get_task_staging_profile
    def after_auth(*args, **kwargs):
        app.state.sessions.delete_session(token)
        return original(*args, **kwargs)
    monkeypatch.setattr(repo, 'get_task_staging_profile', after_auth)
    response = client.get(f"{BASE}/tasks/{task['id']}/staging-profile")
    assert response.status_code == 401

def test_profile_read_holds_writer_until_session_check(hub, monkeypatch):
    import sqlite3
    import threading
    from engineering_hub.staging_resources import StagingResourceStore
    from tests.test_staging_profile_task_binding import PROFILE
    client, app, settings = hub
    repo = app.state.repository
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}))
    store.set_policy('human:1', '3d-viewer', 0, {'codex':['read'], 'human:42':['read']})
    store.enroll_profile('human:1', '3d-viewer', 'static', 0, PROFILE)
    task = repo.create_task('human:1', '3d-viewer', 'Profile review', 'profile-read-first')
    task = repo.claim_task('codex', task['id'], task['revision'])
    repo.set_candidate('codex', task['id'], task['revision'], COMMIT, {'recipe':'static','generation':1})
    human_headers(client, app, settings)
    token = client.cookies.get(settings.auth_config.cookie_name)
    started, finished = threading.Event(), threading.Event()
    errors = []
    def logout():
        started.set()
        try:
            app.state.sessions.delete_session(token)
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()
    original = repo._staging_profile_read
    threads = []
    def concurrent_logout(db, actor, project):
        original(db, actor, project)
        if not threads:
            thread = threading.Thread(target=logout)
            threads.append(thread)
            thread.start()
            assert started.wait(3)
            with sqlite3.connect(str(repo.path), timeout=0) as competitor:
                try:
                    competitor.execute('BEGIN IMMEDIATE')
                except sqlite3.OperationalError as exc:
                    assert 'locked' in str(exc)
                else:
                    raise AssertionError('profile read did not hold SQLite writer')
            assert not finished.is_set()
    monkeypatch.setattr(repo, '_staging_profile_read', concurrent_logout)
    try:
        response = client.get(f"{BASE}/tasks/{task['id']}/staging-profile")
        assert response.status_code == 200
    finally:
        for thread in threads:
            thread.join(5)
    assert finished.is_set() and not errors
    assert app.state.sessions.session_for(token) is None


def test_enrollment_profile_read_checks_browser_session_without_csrf(hub, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engineering_hub.auth import require_human, human_transaction_guard
    from engineering_hub.staging_api import attach_staging_routes
    from engineering_hub.staging_resources import StagingResourceStore
    from tests.test_staging_profile_task_binding import PROFILE
    client, app, settings = hub
    repo, sessions = app.state.repository, app.state.sessions
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:42'}))
    store.enroll_profile('human:42', '3d-viewer', 'static', 0, PROFILE)
    class Controller:
        resources = store
    isolated = FastAPI()
    def human(request, mutation=False):
        return require_human(request, settings.auth_config, sessions, mutation=mutation)
    attach_staging_routes(isolated, Controller(), human, human,
        human_read_admission=lambda request, principal: human_transaction_guard(
            request, settings.auth_config, sessions, principal, mutation=False))
    session = sessions.create_session(42)
    with TestClient(isolated, base_url=settings.auth_config.origin) as reader:
        reader.cookies.set(settings.auth_config.cookie_name, session['cookie_token'])
        path = '/api/hub/v1/staging/projects/3d-viewer/profiles/static'
        assert reader.get(path).status_code == 200  # no Origin or CSRF header
        clock = sessions.clock
        original = store._recipe_name
        def expire(recipe):
            original(recipe)
            sessions.clock = lambda: clock() + 86400 * 1000
        monkeypatch.setattr(store, '_recipe_name', expire)
        assert reader.get(path).status_code == 401
        sessions.clock = clock
        monkeypatch.setattr(store, '_recipe_name', original)
        original_get = store.get_profile
        def logout_after_auth(*args, **kwargs):
            sessions.delete_session(session['cookie_token'])
            return original_get(*args, **kwargs)
        monkeypatch.setattr(store, 'get_profile', logout_after_auth)
        assert reader.get(path).status_code == 401
