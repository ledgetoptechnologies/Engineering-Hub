import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.stale_service_authority import StaleServiceGrant
from tests.test_session_hub_bridge import unified, OWNER, PROJECT


class Clock:
    wall = 1000.0
    mono = 500.0
    boot = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def owned(tmp_path):
    parent = tmp_path / 'private'
    parent.mkdir(mode=0o700)
    path = parent / 'hub.sqlite3'
    owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    repo = HubRepository(path, pilot_owner=owner)
    ProjectRepository(repo)
    clock = Clock()
    grant = StaleServiceGrant(repo, clock=lambda: clock.wall, monotonic=lambda: clock.mono, boot_id=lambda: clock.boot)
    yield repo, owner, grant, clock
    owner.close()


def test_default_off_owner_grant_scoped_and_no_outbox(owned):
    repo, owner, grant, _ = owned
    assert grant.owner_get(OWNER)['state'] == 'disabled'
    capability = grant.bind()
    with pytest.raises(HubError, match='stale_service_grant_inactive'):
        with capability._admit(PROJECT, 0):
            pass
    with repo._connection() as db:
        before = db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0]
    config = grant.owner_configure(OWNER, 0, [PROJECT], 60)
    assert config['revision'] == 1 and config['scheduler'] == 'not_installed'
    with capability._admit(PROJECT, 1) as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(HubError, match='stale_service_project_forbidden'):
        with capability._admit('project-alpha', 1):
            pass
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0] == before
    assert grant.owner_get(OWNER)['state'] == 'authorized'


def test_unowned_ledger_cannot_enable_or_bind(tmp_path):
    repo = HubRepository(tmp_path / 'unowned.sqlite3')
    grant = StaleServiceGrant(repo)
    with pytest.raises(HubError, match='stale_service_owner_required'):
        grant.owner_configure(OWNER, 0, [PROJECT], 60)
    with pytest.raises(HubError, match='stale_service_owner_required'):
        grant.bind()


def test_expiry_at_precommit_rolls_back(owned):
    repo, _, grant, clock = owned
    config = grant.owner_configure(OWNER, 0, [PROJECT], 60)
    cap = grant.bind()
    with pytest.raises(HubError, match='stale_service_grant_inactive'):
        with cap._admit(PROJECT, config['revision']) as db:
            db.execute("INSERT INTO hub_entities(id,kind,data) VALUES('probe','synthetic','{}')")
            clock.mono += 60
    with repo._connection() as db:
        assert db.execute("SELECT 1 FROM hub_entities WHERE id='probe'").fetchone() is None


def test_monotonic_deadline_and_boot_change_fail_closed(owned):
    _, _, grant, clock = owned
    config = grant.owner_configure(OWNER, 0, [PROJECT], 60)
    cap = grant.bind()
    clock.wall += 1
    clock.mono += 60
    with pytest.raises(HubError, match='stale_service_grant_inactive'):
        with cap._admit(PROJECT, config['revision']):
            pass
    clock.mono -= 60
    clock.boot = '22222222-2222-4222-8222-222222222222'
    assert grant.owner_get(OWNER)['state'] == 'boot_changed'
    with pytest.raises(HubError, match='stale_service_grant_inactive'):
        with cap._admit(PROJECT, config['revision']):
            pass


def test_revoke_serializes_and_old_capability_stays_invalid(owned):
    _, _, grant, _ = owned
    config = grant.owner_configure(OWNER, 0, [PROJECT], 60)
    cap = grant.bind()
    entered, release, attempted, revoked = (threading.Event() for _ in range(4))
    def operation():
        with cap._admit(PROJECT, config['revision']):
            entered.set()
            assert release.wait(5)
    def revoke():
        attempted.set()
        result = grant.owner_revoke(OWNER, config['revision'])
        revoked.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(operation)
        assert entered.wait(5)
        revoking = pool.submit(revoke)
        assert attempted.wait(5) and not revoked.wait(.1)
        release.set()
        write.result(5)
        assert revoking.result(5)['revision'] == 2
    with pytest.raises(HubError, match='stale_service_grant_inactive'):
        with cap._admit(PROJECT, config['revision']):
            pass


def test_pause_and_stale_grant_revision_fail_closed(owned):
    repo, _, grant, _ = owned
    config = grant.owner_configure(OWNER, 0, [PROJECT], 60)
    cap = grant.bind()
    repo.set_pause(OWNER, True)
    with pytest.raises(HubError, match='stale_service_paused'):
        with cap._admit(PROJECT, config['revision']):
            pass
    repo.set_pause(OWNER, False)
    new = grant.owner_configure(OWNER, config['revision'], [PROJECT], 60)
    with pytest.raises(HubError, match='stale_service_grant_inactive'):
        with cap._admit(PROJECT, config['revision']):
            pass
    with cap._admit(PROJECT, new['revision']):
        pass


def test_archived_project_and_clock_rollback_are_denied(owned):
    repo, _, grant, clock = owned
    projects = ProjectRepository(repo)
    projects.archive_project(OWNER, '3d-viewer', 1)
    with pytest.raises(HubError, match='project_archived'):
        grant.owner_configure(OWNER, 0, ['3d-viewer'], 60)
    assert grant.owner_get(OWNER)['state'] == 'disabled'
    config = grant.owner_configure(OWNER, 0, [PROJECT], 60)
    clock.wall -= 1
    with pytest.raises(HubError, match='stale_service_grant_inactive'):
        with grant.bind()._admit(PROJECT, config['revision']):
            pass


def test_successor_owner_epoch_requires_fresh_approval(owned):
    repo, owner, grant, clock = owned
    config = grant.owner_configure(OWNER, 0, [PROJECT], 60)
    path = repo.path
    owner.close()
    successor = PilotOwner(path, allow_ephemeral_test_storage=True, require_existing=True)
    try:
        next_repo = HubRepository(path, pilot_owner=successor)
        next_grant = StaleServiceGrant(next_repo, clock=lambda: clock.wall, monotonic=lambda: clock.mono, boot_id=lambda: clock.boot)
        assert next_grant.owner_get(OWNER)['state'] == 'owner_changed'
        with pytest.raises(HubError, match='stale_service_grant_inactive'):
            with next_grant.bind()._admit(PROJECT, config['revision']):
                pass
    finally:
        successor.close()


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires real fork')
def test_inherited_process_capability_denied(owned):
    _, _, grant, _ = owned
    config = grant.owner_configure(OWNER, 0, [PROJECT], 60)
    cap = grant.bind()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            with cap._admit(PROJECT, config['revision']):
                os.write(write_fd, b'allowed')
        except HubError:
            os.write(write_fd, b'denied')
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 16)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0 and result == b'denied'
    with cap._admit(PROJECT, config['revision']):
        pass


def test_owner_http_default_off_and_legacy_ledger_refuses_enable(unified):
    client, app, _, csrf = unified
    path = '/api/hub/v1/orchestration/stale-service-grant'
    assert client.get(path).json()['state'] == 'disabled'
    payload = dict(expected_revision=0, projects=[PROJECT], lifetime_seconds=60)
    assert client.put(path, json=payload).status_code == 403
    assert client.put(path, headers={**csrf, 'Authorization': 'Bearer synthetic-codex'}, json=payload).status_code == 403
    assert client.put(path, headers=csrf, json=payload).status_code == 409
    assert client.get(path).json()['state'] == 'disabled'
    other = app.state.sessions.create_session(43)
    client.cookies.set('hub_session', other['cookie_token'])
    assert client.get(path).status_code == 403
    client.cookies.clear()
    assert client.get(path).status_code == 401
    from engineering_hub.private_gateway import browser_route_allowed
    assert browser_route_allowed('GET', path) and browser_route_allowed('PUT', path)
    assert browser_route_allowed('POST', path + '/revoke') and not browser_route_allowed('DELETE', path)


def test_owner_http_configure_and_revoke_real_enrolled_ledger(owned, tmp_path):
    from pathlib import Path
    from fastapi.testclient import TestClient
    from mcp.server.transport_security import TransportSecuritySettings
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig

    repo, _, _, _ = owned
    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    bus = create_bus(db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False, push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(allowed_hosts=['staging.test'], allowed_origins=[origin]))
    app = create_app(Settings(Path(repo.path), auth, AuthConfig(origin, beau_github_id=42)), repository=repo, bus_app=bus)
    with TestClient(app, base_url=origin) as client:
        owner_session = app.state.sessions.create_session(42)
        client.cookies.set('hub_session', owner_session['cookie_token'])
        csrf = {'Origin': origin, 'X-Hub-CSRF': owner_session['csrf_token']}
        path = '/api/hub/v1/orchestration/stale-service-grant'
        created = client.put(path, headers=csrf, json=dict(expected_revision=0, projects=[PROJECT], lifetime_seconds=60))
        assert created.status_code == 200 and created.json()['scheduler'] == 'not_installed'
        assert client.get(path).json()['state'] == 'authorized'
        assert client.post(path + '/revoke', json={'expected_revision': 1}).status_code == 403
        revoked = client.post(path + '/revoke', headers=csrf, json={'expected_revision': 1})
        assert revoked.status_code == 200 and revoked.json()['enabled'] is False
        assert client.get(path).json()['state'] == 'revoked'
