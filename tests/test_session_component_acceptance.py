"""Focused synthetic acceptance for per-installation component grants.

These tests exercise the real session bridge and repository transaction fences;
they do not contact a Hub deployment or grant any live installation access.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import secrets
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from engineering_hub.repository import HubError


OWNER = 'human:42'
PROJECT = 'engineering-hub'
OTHER_PROJECT = 'project-alpha'
COMPONENT_CAPS = ('component.read', 'component.write')


class Clock:
    def __init__(self):
        self.value = 1700000000.0

    def __call__(self):
        return self.value


@pytest.fixture
def bridge_env(tmp_path):
    from agent_message_bus.agent_sessions import AgentSessionRepository
    from agent_message_bus.knowledge import KnowledgeRepository
    from engineering_hub.components import ProjectComponentRepository
    from engineering_hub.projects import ProjectRepository
    from engineering_hub.repository import HubRepository
    from engineering_hub.session_hub_bridge import SessionHubBridge

    clock = Clock()
    sessions = AgentSessionRepository(tmp_path / 'sessions.sqlite3', OWNER, clock)
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    projects = ProjectRepository(repo)
    components = ProjectComponentRepository(repo)
    knowledge = KnowledgeRepository(tmp_path / 'knowledge.sqlite3')
    knowledge.configure_write_admission(projects.knowledge_write_admission)
    bridge = SessionHubBridge(sessions, repo, projects, components, knowledge)
    yield SimpleNamespace(clock=clock, sessions=sessions, repo=repo, projects=projects,
                          components=components, knowledge=knowledge, bridge=bridge)
    bridge.close()


def enroll(env, name, *, family='codex', projects=(PROJECT,), capabilities=COMPONENT_CAPS):
    secret = 'ehi_' + secrets.token_urlsafe(32)
    pending = env.sessions.request_enrollment(
        family, name, name + '-pc', hashlib.sha256(secret.encode()).hexdigest())
    env.sessions.approve(OWNER, pending['id'], pending['revision'])
    issued = env.sessions.exchange(secret, label=name + '-session')
    grant = env.bridge.grant_put(OWNER, pending['id'], 0, list(projects), list(capabilities))
    assert grant['state'] == 'active'
    return SimpleNamespace(installation_id=pending['id'], secret=secret,
                           token=issued['token'], session_id=issued['session']['id'],
                           grant_revision=grant['revision'])


def component_call(env, session, tool, arguments, operation):
    """Run the same bridge admission and project-scope checks as MCP wrappers."""
    with env.bridge.admit(session.token, tool) as admitted:
        env.bridge._scope(tool, arguments, admitted)
        return operation(admitted.principal.actor)


def create_component(env, session, project, slug, request_key, *, legacy_project_id=None):
    payload = {'project': project, 'slug': slug, 'legacy_project_id': legacy_project_id}
    return component_call(
        env, session, 'hub_component_create', {'payload': payload},
        lambda actor: env.components.create_component(
            actor, project, slug, 'Component ' + slug, request_key,
            legacy_project_id=legacy_project_id))


def update_component(env, session, item, expected_revision, **fields):
    payload = {'component_id': item['id'], 'expected_revision': expected_revision, **fields}
    return component_call(
        env, session, 'hub_component_update', {'payload': payload},
        lambda actor: env.components.update_component(
            actor, item['id'], expected_revision, **fields))


def retire_component(env, session, item, expected_revision):
    payload = {'component_id': item['id'], 'expected_revision': expected_revision}
    return component_call(
        env, session, 'hub_component_retire', {'payload': payload},
        lambda actor: env.components.retire_component(actor, item['id'], expected_revision))


def component_evidence(env, component_id):
    with env.repo._connection() as db:
        audit = db.execute(
            'SELECT sequence,action,revision FROM hub_audit WHERE entity_id=? ORDER BY sequence',
            (component_id,)).fetchall()
        provenance = db.execute(
            'SELECT a.action,a.revision,p.installation_id,p.session_id,p.family,'
            'p.grant_revision,p.capability FROM hub_audit a '
            'JOIN hub_session_provenance p ON p.audit_sequence=a.sequence '
            'WHERE a.entity_id=? ORDER BY a.sequence', (component_id,)).fetchall()
    return ([dict(row) for row in audit], [dict(row) for row in provenance])


def component_counts(env, component_id):
    audit, provenance = component_evidence(env, component_id)
    with env.repo._connection() as db:
        item = db.execute('SELECT count(*) FROM hub_project_components WHERE id=?',
                          (component_id,)).fetchone()[0]
        requests = db.execute('SELECT count(*) FROM hub_component_requests WHERE component_id=?',
                              (component_id,)).fetchone()[0]
    return item, requests, len(audit), len(provenance)


def component_catalog_counts(env):
    with env.repo._connection() as db:
        return (
            db.execute('SELECT count(*) FROM hub_project_components').fetchone()[0],
            db.execute('SELECT count(*) FROM hub_component_requests').fetchone()[0],
            db.execute("SELECT count(*) FROM hub_audit WHERE action LIKE 'component.%'").fetchone()[0],
            db.execute('SELECT count(*) FROM hub_session_provenance').fetchone()[0],
        )


def test_two_installations_and_sibling_sessions_scope_component_lifecycle_and_provenance(bridge_env):
    env = bridge_env
    pc_a = enroll(env, 'codex-pc-a')
    pc_a_sibling = env.sessions.exchange(pc_a.secret, label='codex-pc-a-sibling')
    sibling = SimpleNamespace(installation_id=pc_a.installation_id,
                              token=pc_a_sibling['token'],
                              session_id=pc_a_sibling['session']['id'])
    pc_b = enroll(env, 'codex-pc-b')

    item = create_component(env, pc_a, PROJECT, 'pc-a-component', 'same-request-key')
    assert item['created_by'] == 'installation:' + pc_a.installation_id
    assert create_component(env, pc_a, PROJECT, 'pc-a-component', 'same-request-key') == item
    # Sibling sessions share the installation-scoped idempotency namespace.
    assert create_component(env, sibling, PROJECT, 'pc-a-component', 'same-request-key') == item

    # A different installation may use the same request key independently.
    other = create_component(env, pc_b, PROJECT, 'pc-b-component', 'same-request-key')
    assert other['id'] != item['id']
    assert other['created_by'] == 'installation:' + pc_b.installation_id

    before_denials = component_catalog_counts(env)
    with pytest.raises(HubError) as denied_project:
        create_component(env, pc_a, OTHER_PROJECT, 'out-of-scope', 'out-of-scope-project')
    assert getattr(denied_project.value, 'code', None) == 'hub_project_forbidden'
    # A legacy-project reference is a second project scope and cannot escape
    # the grant even when the component itself belongs to an allowed project.
    with pytest.raises(HubError) as denied_reference:
        update_component(env, pc_a, item, 1, legacy_project_id=OTHER_PROJECT)
    assert getattr(denied_reference.value, 'code', None) == 'hub_project_forbidden'
    with pytest.raises(HubError) as idempotency_conflict:
        component_call(
            env, pc_a, 'hub_component_create',
            {'payload': {'project': PROJECT, 'slug': 'different', 'legacy_project_id': None}},
            lambda actor: env.components.create_component(
                actor, PROJECT, 'different', 'Different', 'same-request-key'))
    assert getattr(idempotency_conflict.value, 'code', None) == 'idempotency_conflict'
    assert component_catalog_counts(env) == before_denials

    edited = update_component(env, sibling, item, 1, name='Sibling edit')
    assert edited['revision'] == 2 and edited['id'] == item['id'] and edited['slug'] == item['slug']
    with pytest.raises(HubError) as stale:
        update_component(env, pc_b, item, 1, description='stale')
    assert getattr(stale.value, 'code', None) == 'revision_conflict'

    retired = retire_component(env, sibling, edited, 2)
    assert retired['status'] == 'retired' and retired['revision'] == 3
    listed = component_call(
        env, pc_a, 'hub_component_list', {'project': PROJECT},
        lambda _actor: env.components.list_components(PROJECT))
    assert item['id'] not in {value['id'] for value in listed['items']}
    including_retired = component_call(
        env, pc_a, 'hub_component_list', {'project': PROJECT},
        lambda _actor: env.components.list_components(PROJECT, include_retired=True))
    assert any(value['id'] == item['id'] and value['status'] == 'retired'
               for value in including_retired['items'])
    restored = update_component(env, pc_a, retired, 3, status='active')
    assert (restored['id'], restored['slug'], restored['created_by'], restored['revision']) == (
        item['id'], item['slug'], item['created_by'], 4)

    audit, provenance = component_evidence(env, item['id'])
    assert [(row['action'], row['revision']) for row in audit] == [
        ('component.created', 1), ('component.updated', 2),
        ('component.retired', 3), ('component.updated', 4)]
    assert [(row['action'], row['revision']) for row in provenance] == [
        ('component.created', 1), ('component.updated', 2),
        ('component.retired', 3), ('component.updated', 4)]
    assert [row['installation_id'] for row in provenance] == [pc_a.installation_id] * 4
    assert [row['session_id'] for row in provenance] == [
        pc_a.session_id, sibling.session_id, sibling.session_id, pc_a.session_id]
    assert all(row['family'] == 'codex' and row['grant_revision'] == pc_a.grant_revision
               and row['capability'] == 'component.write' for row in provenance)

    # Revoking PC A's Hub grant blocks component writes from both A sessions;
    # their underlying session identities and PC B's grant remain active.
    before_revoke = component_catalog_counts(env)
    revoked = env.bridge.grant_revoke(OWNER, pc_a.installation_id, pc_a.grant_revision)
    assert revoked['state'] == 'revoked'
    for principal in (pc_a, sibling):
        identity = env.sessions.authenticate_session(principal.token)
        assert identity.installation_id == pc_a.installation_id
        assert identity.session_id == principal.session_id
        with pytest.raises(HubError) as denied:
            update_component(env, principal, item, 4, name='revoked write')
        assert getattr(denied.value, 'code', None) == 'hub_grant_required'
    assert component_catalog_counts(env) == before_revoke
    other_edit = update_component(env, pc_b, other, 1, name='PC B remains active')
    assert other_edit['revision'] == 2
    _other_audit, other_provenance = component_evidence(env, other['id'])
    assert [row['action'] for row in other_provenance] == ['component.created', 'component.updated']
    assert all(row['installation_id'] == pc_b.installation_id
               and row['family'] == 'codex' for row in other_provenance)


def test_grant_revocation_waits_for_inflight_component_commit_then_denies_followup(
        bridge_env, monkeypatch):
    env = bridge_env
    session = enroll(env, 'revoke-race-pc')
    entered_save = threading.Event()
    release_save = threading.Event()
    contention_witness = threading.Event()
    revoke_returned = threading.Event()

    original_save = env.components._save

    def paused_save(db, actor, action, item):
        result = original_save(db, actor, action, item)
        entered_save.set()
        if not release_save.wait(5):
            raise TimeoutError('component fixture release timed out')
        return result

    monkeypatch.setattr(env.components, '_save', paused_save)
    original_lock = env.sessions._lock

    def write_component():
        return create_component(env, session, PROJECT, 'revoke-race', 'create')

    def revoke_grant():
        # The writer is paused inside bridge admission while holding this exact
        # repository lock. Prove the revoker cannot acquire it before proceeding.
        acquired = original_lock.acquire(blocking=False)
        if acquired:
            original_lock.release()
            raise AssertionError('revoker did not contend with the active writer')
        contention_witness.set()
        result = env.bridge.grant_revoke(OWNER, session.installation_id, session.grant_revision)
        revoke_returned.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write_component)
        revoker = None
        try:
            assert entered_save.wait(5)
            revoker = pool.submit(revoke_grant)
            assert contention_witness.wait(5)
            assert not revoke_returned.is_set()
        finally:
            release_save.set()
        created = writer.result(timeout=5)
        revoked = revoker.result(timeout=5)

    assert revoked['state'] == 'revoked'
    before_denied_write = component_counts(env, created['id'])
    with pytest.raises(HubError) as denied:
        update_component(env, session, created, 1, name='must not commit')
    assert getattr(denied.value, 'code', None) == 'hub_grant_required'
    assert component_counts(env, created['id']) == before_denied_write


def test_project_archive_waits_for_component_commit_then_blocks_followup(
        bridge_env, monkeypatch):
    env = bridge_env
    project = OTHER_PROJECT
    session = enroll(env, 'archive-race-pc', projects=(project,))
    entered_save = threading.Event()
    release_save = threading.Event()
    contention_witness = threading.Event()
    archive_returned = threading.Event()
    thread_ids = {}

    original_save = env.components._save

    def paused_save(db, actor, action, item):
        result = original_save(db, actor, action, item)
        entered_save.set()
        if not release_save.wait(5):
            raise TimeoutError('component fixture release timed out')
        return result

    original_write_connection = env.repo._write_connection

    @contextmanager
    def observed_write_connection():
        with original_write_connection() as db:
            if threading.get_ident() == thread_ids.get('archive'):
                db.execute('PRAGMA busy_timeout=0')
                try:
                    db.execute('BEGIN IMMEDIATE')
                except sqlite3.OperationalError as exc:
                    assert exc.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                    contention_witness.set()
                else:
                    db.rollback()
                    raise AssertionError('archive did not contend with the active writer')
                finally:
                    db.execute('PRAGMA busy_timeout=10000')
            yield db

    monkeypatch.setattr(env.components, '_save', paused_save)
    monkeypatch.setattr(env.repo, '_write_connection', observed_write_connection)

    def write_component():
        thread_ids['component'] = threading.get_ident()
        return create_component(env, session, project, 'archive-race', 'create')

    def archive_project():
        thread_ids['archive'] = threading.get_ident()
        result = env.projects.archive_project(OWNER, project, 1)
        archive_returned.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write_component)
        archiver = None
        try:
            assert entered_save.wait(5)
            archiver = pool.submit(archive_project)
            assert contention_witness.wait(5)
            assert not archive_returned.is_set()
        finally:
            release_save.set()
        created = writer.result(timeout=5)
        archived = archiver.result(timeout=5)

    assert archived['archived'] is True and archived['revision'] == 2
    with env.repo._connection() as db:
        component_sequence = db.execute(
            "SELECT sequence FROM hub_audit WHERE entity_id=? AND action='component.created'",
            (created['id'],)).fetchone()['sequence']
        archive_sequence = db.execute(
            "SELECT sequence FROM hub_audit WHERE entity_id=? AND action='project.archived'",
            (project,)).fetchone()['sequence']
    assert component_sequence < archive_sequence
    before_denied_write = component_counts(env, created['id'])
    with pytest.raises(HubError) as denied:
        update_component(env, session, created, 1, name='must not commit')
    assert getattr(denied.value, 'code', None) == 'project_archived'
    assert component_counts(env, created['id']) == before_denied_write


@pytest.fixture
def live_app_env(tmp_path):
    from agent_message_bus.agent_sessions_config import AgentSessionsConfig
    from agent_message_bus.app import create_app as create_bus
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from fastapi.testclient import TestClient
    from mcp.server.transport_security import TransportSecuritySettings

    origin = 'https://staging.test'
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', claude_token='synthetic-claude')
    clock = Clock()
    bus = create_bus(
        db_path=tmp_path / 'bus.sqlite3', audit_path=tmp_path / 'audit.jsonl',
        file_root=tmp_path / 'files', auth=auth, startup_cleanup=False,
        push_events_operational=False,
        agent_sessions_config=AgentSessionsConfig(
            tmp_path / 'connections.sqlite3', OWNER, origin, hub_bridge=True),
        transport_security_settings=TransportSecuritySettings(
            allowed_hosts=['staging.test'], allowed_origins=[origin]))
    app = create_app(
        Settings(tmp_path / 'hub.sqlite3', auth,
                 AuthConfig(origin, beau_github_id=42)), bus_app=bus)
    bus.state.agent_sessions.clock = clock
    with TestClient(app, base_url=origin) as client:
        cookie = app.state.sessions.create_session(42)
        client.cookies.set('hub_session', cookie['cookie_token'])
        csrf = {'Origin': origin, 'X-Hub-CSRF': cookie['csrf_token']}
        yield SimpleNamespace(client=client, sessions=bus.state.agent_sessions,
                              bridge=app.state.session_hub_bridge,
                              projects=app.state.project_registry, csrf=csrf)


def open_mcp_session(client, token):
    headers = {'Authorization': 'Bearer ' + token,
               'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': '2025-06-18'}
    initialized = client.post('/mcp', headers=headers, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                   'clientInfo': {'name': 'synthetic-component-acceptance', 'version': '1'}}})
    assert initialized.status_code == 200
    headers['Mcp-Session-Id'] = initialized.headers['Mcp-Session-Id']
    client.post('/mcp', headers=headers,
                json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    return headers


def test_real_mcp_surface_has_components_but_project_mutations_stay_human_only(live_app_env):
    env = live_app_env
    session = enroll(env, 'component-tool-pc')
    headers = open_mcp_session(env.client, session.token)
    response = env.client.post('/mcp', headers=headers,
                               json={'jsonrpc': '2.0', 'id': 2,
                                     'method': 'tools/list', 'params': {}})
    assert response.status_code == 200
    names = {tool['name'] for tool in response.json()['result']['tools']}
    assert {'hub_component_list', 'hub_component_get', 'hub_component_create',
            'hub_component_update', 'hub_component_retire'} <= names
    assert not {'hub_project_create', 'hub_project_update', 'hub_project_archive',
                'hub_project_delete'} & names

    token_headers = {'Authorization': 'Bearer ' + session.token}
    denied_create = env.client.post(
        '/api/hub/v1/projects', headers=token_headers,
        json={'project_id': 'agent-created', 'name': 'Agent-created',
              'client_request_id': 'agent-project'})
    assert denied_create.status_code == 403
    assert denied_create.json()['detail']['code'] == 'human_session_required'
    denied_archive = env.client.post(
        '/api/hub/v1/projects/' + OTHER_PROJECT + '/archive', headers=token_headers,
        json={'expected_revision': 1})
    assert denied_archive.status_code == 403
    assert denied_archive.json()['detail']['code'] == 'human_session_required'
    with env.projects._connection() as db:
        assert db.execute('SELECT 1 FROM hub_projects WHERE id=?', ('agent-created',)).fetchone() is None
        archived_project = env.projects._fetch(db, OTHER_PROJECT)
        assert archived_project['archived'] is False and archived_project['revision'] == 1

    created = env.client.post(
        '/api/hub/v1/projects', headers=env.csrf,
        json={'project_id': 'human-created', 'name': 'Human-created',
              'client_request_id': 'human-project'})
    assert created.status_code == 201
    archived = env.client.post(
        '/api/hub/v1/projects/human-created/archive', headers=env.csrf,
        json={'expected_revision': 1})
    assert archived.status_code == 200 and archived.json()['archived'] is True
