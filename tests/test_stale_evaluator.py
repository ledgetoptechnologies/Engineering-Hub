import json
from types import SimpleNamespace

import pytest

from agent_message_bus.agent_sessions import AgentSessionRepository
from agent_message_bus.knowledge import KnowledgeRepository
from engineering_hub.components import ProjectComponentRepository
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.session_hub_bridge import SessionHubBridge
from engineering_hub.stale_evaluator import StaleEvaluator
from engineering_hub.stale_service_authority import StaleServiceGrant
from tests.test_dispatch_coordination import setup_dispatch
from tests.test_stale_service_authority import owned
from tests.test_session_hub_bridge import OWNER, PROJECT, Clock, unified


@pytest.fixture
def evaluator_env(tmp_path):
    directory = tmp_path / 'owned'
    directory.mkdir(mode=0o700)
    path = directory / 'hub.sqlite3'
    owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    repo = HubRepository(path, pilot_owner=owner)
    clock = Clock()
    tick = SimpleNamespace(value=500.0)
    sessions = AgentSessionRepository(tmp_path / 'sessions.sqlite3', OWNER, clock)
    projects = ProjectRepository(repo)
    components = ProjectComponentRepository(repo)
    knowledge = KnowledgeRepository(tmp_path / 'knowledge.sqlite3')
    knowledge.configure_write_admission(projects.knowledge_write_admission)
    bridge = SessionHubBridge(sessions, repo, projects, components, knowledge)
    grant = StaleServiceGrant(repo, clock=clock, monotonic=lambda: tick.value,
                              boot_id=lambda: '11111111-1111-4111-8111-111111111111')
    service = StaleEvaluator(grant, bridge, interval=1, batch=2)
    env = SimpleNamespace(clock=clock, tick=tick, sessions=sessions, repo=repo, projects=projects,
                          components=components, knowledge=knowledge, bridge=bridge, grant=grant, evaluator=service)
    yield env
    service.stop()
    bridge.close()
    owner.close()


def approve(env):
    grant = env.grant.owner_configure(OWNER, 0, [PROJECT], 120)
    config = env.evaluator.configure(OWNER, 0, grant['revision'], True)
    env.evaluator.scan_once()  # Establish a conservative boot-bound observation.
    return grant, config


def advance(env, seconds):
    env.clock.value += seconds
    env.tick.value += seconds


def test_opt_in_required_and_liveness_is_advisory(evaluator_env):
    env = evaluator_env
    _, _, task, _, _, dispatch, _, item = setup_dispatch(env)
    grant = env.grant.owner_configure(OWNER, 0, [PROJECT], 120)
    advance(env, 15)
    assert env.evaluator.scan_once() is None
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'
    env.evaluator.configure(OWNER, 0, grant['revision'], True)
    env.evaluator.scan_once()
    advance(env, 15)
    with env.repo._connection() as db:
        outbox = db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0]
    assert env.evaluator.scan_once() == 0  # End-of-cycle cursor wraps on the next scan.
    assert env.evaluator.scan_once() == 1
    result = env.repo._read(item['id'], 'role_dispatch')
    assert result['state'] == 'stale' and result['wake_delivery'] == 'not_attempted'
    intent = env.repo._read(result['attention_intent_id'], 'orchestration_attention')
    evidence = intent['evaluation_evidence']
    assert evidence['source'] == 'automatic_advisory' and evidence['observed_elapsed_seconds'] >= 14
    assert evidence['report_window_seconds'] == 10 and len(evidence['state_fingerprint']) == 64
    assert env.repo._read(task['id'], 'tracking_task') == task
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0] == outbox
    assert env.evaluator.evaluate(PROJECT, item['id']) is False


def test_clock_jump_requires_new_grant(evaluator_env):
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    grant, config = approve(env)
    env.clock.value += 11
    with pytest.raises(HubError, match='clock_untrusted'):
        env.evaluator.evaluate(PROJECT, item['id'])
    env.clock.value -= 11
    with pytest.raises(HubError, match='reapproval_required'):
        env.evaluator.configure(OWNER, config['revision'], grant['revision'], True)
    renewed = env.grant.owner_configure(OWNER, grant['revision'], [PROJECT], 120)
    env.evaluator.configure(OWNER, config['revision'], renewed['revision'], True)
    advance(env, 15)
    assert env.evaluator.evaluate(PROJECT, item['id']) is True


def test_revoke_or_pause_denies_without_mutation(evaluator_env):
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    grant, _ = approve(env)
    advance(env, 15)
    env.grant.owner_revoke(OWNER, grant['revision'])
    with pytest.raises(HubError, match='grant_inactive'):
        env.evaluator.evaluate(PROJECT, item['id'])
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'


def test_worker_stops_before_bridge_close(evaluator_env):
    env = evaluator_env
    approve(env)
    env.evaluator.start()
    env.evaluator.stop()
    assert env.evaluator._thread is None
    env.bridge._owned()


def test_reconstruction_requires_fresh_grant_and_opt_in(evaluator_env):
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    grant, config = approve(env)
    replacement = StaleEvaluator(env.grant, env.bridge, interval=1)
    try:
        assert replacement.scan_once() is None
        with pytest.raises(HubError, match='reapproval_required'):
            replacement.configure(OWNER, config['revision'], grant['revision'], True)
        renewed = env.grant.owner_configure(OWNER, grant['revision'], [PROJECT], 120)
        replacement.configure(OWNER, config['revision'], renewed['revision'], True)
        advance(env, 15)
        assert replacement.evaluate(PROJECT, item['id']) is True
    finally:
        replacement.stop()


def test_clock_jump_during_evaluation_rolls_back(evaluator_env, monkeypatch):
    from engineering_hub.dispatch import DispatchRepository
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    approve(env)
    advance(env, 15)
    old = DispatchRepository._evaluate_checkpoint
    def with_clock_jump(self, *args, **kwargs):
        result = old(self, *args, **kwargs)
        env.clock.value += 10
        return result
    monkeypatch.setattr(DispatchRepository, '_evaluate_checkpoint', with_clock_jump)
    with pytest.raises(HubError, match='clock_untrusted'):
        env.evaluator.evaluate(PROJECT, item['id'])
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='orchestration_attention'").fetchone()[0] == 0


def test_unattributable_corrupt_json_is_skipped_without_disclosure(evaluator_env):
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    approve(env)
    with env.repo._transaction() as db:
        db.execute("INSERT INTO hub_entities(id,kind,data) VALUES('invalid','role_dispatch','{')")
    advance(env, 15)
    assert env.evaluator.scan_once() == 0
    assert env.evaluator.scan_once() == 1
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'stale'
    assert 'invalid_dispatch_data_seen' not in env.evaluator.status(OWNER)


def test_authority_second_clock_read_failure_requires_new_grant(evaluator_env):
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    grant, config = approve(env)
    original = env.grant.clock
    calls = 0
    def fail_second():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HubError('stale_service_clock_untrusted', 409)
        return original()
    env.grant.clock = fail_second
    with pytest.raises(HubError, match='clock_untrusted'):
        env.evaluator.evaluate(PROJECT, item['id'])
    env.grant.clock = original
    with pytest.raises(HubError, match='reapproval_required'):
        env.evaluator.configure(OWNER, config['revision'], grant['revision'], True)
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'


def test_legacy_ledger_denies_owner_evaluator_endpoint(unified):
    client, app, _, csrf = unified
    path = '/api/hub/v1/orchestration/stale-evaluator'
    assert client.get(path).status_code == 503
    payload = dict(expected_revision=0, grant_revision=1, enabled=True)
    assert client.put(path, json=payload).status_code == 403
    assert client.put(path, headers=csrf, json=payload).status_code == 503
    other = app.state.sessions.create_session(43)
    client.cookies.set('hub_session', other['cookie_token'])
    assert client.get(path).status_code == 403
    client.cookies.clear()
    assert client.get(path).status_code == 401
    from engineering_hub.private_gateway import browser_route_allowed
    assert browser_route_allowed('GET', path) and browser_route_allowed('PUT', path)
    assert not browser_route_allowed('POST', path)


def test_owner_http_real_opt_in_and_disable(owned, tmp_path):
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
        grant_path = '/api/hub/v1/orchestration/stale-service-grant'
        path = '/api/hub/v1/orchestration/stale-evaluator'
        assert client.get(path).json()['state'] == 'disabled'
        assert client.put(grant_path, headers=csrf, json=dict(expected_revision=0, projects=[PROJECT], lifetime_seconds=60)).status_code == 200
        payload = dict(expected_revision=0, grant_revision=1, enabled=True)
        assert client.put(path, json=payload).status_code == 403
        assert client.put(path, headers=csrf, json=payload).status_code == 200
        assert client.get(path).json()['worker_running'] is True
        assert client.put(path, headers=csrf, json=dict(expected_revision=1, grant_revision=1, enabled=False)).status_code == 200
        assert client.get(path).json()['worker_running'] is False
    assert app.state.stale_evaluator._thread is None


def test_auto_evaluation_serializes_target_grant_revocation(evaluator_env, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from engineering_hub.dispatch import DispatchRepository
    env = evaluator_env
    _, _, _, _, roles, _, _, item = setup_dispatch(env)
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    approve(env)
    advance(env, 15)
    entered, release, attempted = (threading.Event() for _ in range(3))
    original = DispatchRepository._evaluate_checkpoint
    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(DispatchRepository, '_evaluate_checkpoint', held)
    def revoke():
        attempted.set()
        return env.bridge.grant_revoke(OWNER, installation, 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        evaluating = pool.submit(env.evaluator.evaluate, PROJECT, item['id'])
        assert entered.wait(10)
        revoking = pool.submit(revoke)
        assert attempted.wait(10)
        assert not revoking.done()
        release.set()
        assert evaluating.result(timeout=10) is True
        assert revoking.result(timeout=10)['state'] == 'revoked'
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'stale'


def test_closed_task_does_not_create_attention(evaluator_env):
    env = evaluator_env
    _, _, task, _, _, _, _, item = setup_dispatch(env)
    approve(env)
    with env.repo._transaction() as db:
        task['status'] = 'completion_verified'  # Synthetic status race.
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(task), task['id']))
    advance(env, 15)
    assert env.evaluator.evaluate(PROJECT, item['id']) is False
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='orchestration_attention'").fetchone()[0] == 0


def test_project_rotation_and_cursor_are_bounded(evaluator_env, monkeypatch):
    import uuid
    env = evaluator_env
    env.projects.create_project(OWNER, 'second-project', 'Second', 'second-project')
    grant = env.grant.owner_configure(OWNER, 0, [PROJECT, 'second-project'], 120)
    env.evaluator.configure(OWNER, 0, grant['revision'], True)
    with env.repo._transaction() as db:
        for project in (PROJECT, 'second-project'):
            for number in range(5):
                identity = str(uuid.uuid4())
                data = json.dumps(dict(id=identity, project=project, state='working'))
                db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,'role_dispatch',?)", (identity, data))
        for number in range(10):
            identity = str(uuid.uuid4())
            data = json.dumps(dict(id=identity, project=PROJECT, state='done'))
            db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,'role_dispatch',?)", (identity, data))
    visited = []
    monkeypatch.setattr(env.evaluator, 'evaluate', lambda project, identity: visited.append((project, identity)))
    for _ in range(6):
        assert env.evaluator.scan_once() <= 2
    assert len(visited) == 10 and len(set(visited)) == 10
    assert [p for p, _ in visited[:4]] == [PROJECT, PROJECT, 'second-project', 'second-project']


def test_service_observes_committed_target_heartbeat(evaluator_env):
    from tests.test_dispatch_staleness import allow_progress, heartbeat
    env = evaluator_env
    _, worker, task, _, roles, _, _, item = setup_dispatch(env)
    allow_progress(env, roles)
    approve(env)
    advance(env, 8)
    heartbeat(env, worker, task, 0, 'service-heartbeat')
    advance(env, 3)
    assert env.evaluator.evaluate(PROJECT, item['id']) is False
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'
    advance(env, 15)
    assert env.evaluator.evaluate(PROJECT, item['id']) is True


def test_stop_waits_for_inflight_worker(evaluator_env, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from engineering_hub.dispatch import DispatchRepository
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    approve(env)
    advance(env, 15)
    entered, release = threading.Event(), threading.Event()
    original = DispatchRepository._evaluate_checkpoint
    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(DispatchRepository, '_evaluate_checkpoint', held)
    env.evaluator.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert entered.wait(10)
        stopping = pool.submit(env.evaluator.stop)
        assert not stopping.done()
        env.bridge._owned()
        release.set()
        stopping.result(timeout=10)
    assert env.evaluator._thread is None
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'stale'


def test_archived_target_role_project_skips_evaluation(evaluator_env):
    from engineering_hub.roles import _identity
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    env.projects.create_project(OWNER, 'other-project', 'Other', 'other-project')
    with env.repo._transaction() as db:
        role = env.repo._get(db, _identity('backend'), 'coordination_role')
        role['projects'].append('other-project')  # Synthetic prior multi-project role configuration.
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(role), role['id']))
    env.projects.archive_project(OWNER, 'other-project', 1)
    grant = env.grant.owner_configure(OWNER, 0, [PROJECT], 120)
    env.evaluator.configure(OWNER, 0, grant['revision'], True)
    advance(env, 15)
    assert env.evaluator.evaluate(PROJECT, item['id']) is False
    assert env.evaluator.status(OWNER)['last_error'] == 'target_role_unavailable'
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'


def test_heartbeat_racing_evaluation_is_not_lost(evaluator_env, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from engineering_hub.dispatch import DispatchRepository
    from engineering_hub.task_progress import TaskProgress
    from tests.test_dispatch_staleness import allow_progress, heartbeat
    env = evaluator_env
    _, worker, task, _, roles, _, _, item = setup_dispatch(env)
    allow_progress(env, roles)
    approve(env)
    advance(env, 15)
    entered, release, attempted = (threading.Event() for _ in range(3))
    original = DispatchRepository._evaluate_checkpoint
    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(DispatchRepository, '_evaluate_checkpoint', held)
    def progress():
        attempted.set()
        return heartbeat(env, worker, task, 0, 'racing-heartbeat')
    with ThreadPoolExecutor(max_workers=2) as pool:
        evaluating = pool.submit(env.evaluator.evaluate, PROJECT, item['id'])
        assert entered.wait(10)
        writing = pool.submit(progress)
        assert attempted.wait(10) and not writing.done()
        release.set()
        assert evaluating.result(timeout=10) is True
        assert writing.result(timeout=10)['revision'] == 1
    assert TaskProgress(env.repo).owner_get(OWNER, task['id'])['revision'] == 1


def test_transient_target_clock_failure_never_marks_stale(evaluator_env, monkeypatch):
    from agent_message_bus.agent_sessions import AgentSessionError
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    grant, config = approve(env)
    advance(env, 15)
    original = env.bridge._live
    calls = 0
    def faulty(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AgentSessionError('session_clock_unavailable', 503)
        return original(*args, **kwargs)
    monkeypatch.setattr(env.bridge, '_live', faulty)
    with env.repo._connection() as db:
        audit = db.execute('SELECT count(*) FROM hub_audit').fetchone()[0]
    with pytest.raises(AgentSessionError, match='session_clock_unavailable'):
        env.evaluator.evaluate(PROJECT, item['id'])
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'
    with env.repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_audit').fetchone()[0] == audit
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='orchestration_attention'").fetchone()[0] == 0
    with pytest.raises(HubError, match='reapproval_required'):
        env.evaluator.configure(OWNER, config['revision'], grant['revision'], True)


def test_revoked_target_grant_is_advisory_stale(evaluator_env):
    env = evaluator_env
    _, _, _, _, roles, _, _, item = setup_dispatch(env)
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    grant = env.grant.owner_configure(OWNER, 0, [PROJECT], 7200)
    env.evaluator.configure(OWNER, 0, grant['revision'], True)
    assert env.evaluator.evaluate(PROJECT, item['id']) is False
    env.bridge.grant_revoke(OWNER, installation, 1)
    assert env.evaluator.evaluate(PROJECT, item['id']) is False
    advance(env, 3605)
    assert env.evaluator.evaluate(PROJECT, item['id']) is True
    result = env.repo._read(item['id'], 'role_dispatch')
    assert result['stale_basis'] == 'target_lease_or_authorization_lost'
    assert result['wake_delivery'] == 'not_attempted'


def test_project_scoped_scan_does_not_visit_other_project(evaluator_env, monkeypatch):
    import uuid
    env = evaluator_env
    approve(env)
    in_scope, outside = str(uuid.uuid4()), str(uuid.uuid4())
    with env.repo._transaction() as db:
        for identity, project in ((in_scope, PROJECT), (outside, 'other-project')):
            payload = json.dumps(dict(id=identity, project=project, state='working', sensitive='private'))
            db.execute("INSERT INTO hub_entities(id,kind,data) VALUES(?,'role_dispatch',?)", (identity, payload))
    visited = []
    monkeypatch.setattr(env.evaluator, 'evaluate', lambda project, identity: visited.append((project, identity)))
    assert env.evaluator.scan_once() == 1
    assert visited == [(PROJECT, in_scope)]
    assert 'invalid_dispatch_data_seen' not in env.evaluator.status(OWNER)


def test_preapproval_wall_jump_cannot_mark_live_lease_stale(evaluator_env):
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    env.clock.value += 3600  # Synthetic wall jump before either owner approval or observation.
    approve(env)
    advance(env, 15)
    assert env.evaluator.evaluate(PROJECT, item['id']) is False
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'


def test_within_tolerance_forward_skew_does_not_expire_window_early(evaluator_env):
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    approve(env)
    advance(env, 12)
    env.clock.value += 2
    assert env.evaluator.evaluate(PROJECT, item['id']) is False
    advance(env, 2)
    assert env.evaluator.evaluate(PROJECT, item['id']) is True


def test_max_window_anchor_survives_grant_renewal(evaluator_env, monkeypatch):
    from engineering_hub.dispatch import DispatchRepository
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    with env.repo._transaction() as db:
        item['window_seconds'] = 86400  # Synthetic supported max-window dispatch.
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), item['id']))
    grant = env.grant.owner_configure(OWNER, 0, [PROJECT], 86400)
    env.evaluator.configure(OWNER, 0, grant['revision'], True)
    monkeypatch.setattr(DispatchRepository, '_target_live', lambda *args: True)
    assert env.evaluator.evaluate(PROJECT, item['id']) is False
    advance(env, 86399)
    assert env.evaluator.evaluate(PROJECT, item['id']) is False
    renewed = env.grant.owner_configure(OWNER, grant['revision'], [PROJECT], 86400)
    env.evaluator.configure(OWNER, 1, renewed['revision'], True)
    advance(env, 6)
    assert env.evaluator.evaluate(PROJECT, item['id']) is True


def test_anchor_refresh_reuses_one_row_without_audit_or_outbox_growth(evaluator_env):
    from engineering_hub.roles import _identity
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    approve(env)
    with env.repo._connection() as db:
        audit = db.execute('SELECT count(*) FROM hub_audit').fetchone()[0]
        outbox = db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0]
    for index in range(20):
        with env.repo._transaction() as db:
            role = env.repo._get(db, _identity('backend'), 'coordination_role')
            role['standing_rules'] = 'Synthetic observation %d' % index
            db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(role), role['id']))
        assert env.evaluator.evaluate(PROJECT, item['id']) is False
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='stale_evaluator_anchor'").fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM hub_audit').fetchone()[0] == audit
        assert db.execute('SELECT count(*) FROM hub_outbox').fetchone()[0] == outbox


def test_missing_evidence_update_rolls_back_stale_transition(evaluator_env):
    env = evaluator_env
    _, _, _, _, _, _, _, item = setup_dispatch(env)
    approve(env)
    with env.repo._transaction() as db:
        db.execute("CREATE TRIGGER synthetic_ignore_attention_update BEFORE UPDATE ON hub_entities WHEN NEW.kind='orchestration_attention' BEGIN SELECT RAISE(IGNORE); END")
    advance(env, 15)
    with pytest.raises(HubError, match='evidence_unavailable'):
        env.evaluator.evaluate(PROJECT, item['id'])
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'
    with env.repo._connection() as db:
        assert db.execute("SELECT count(*) FROM hub_entities WHERE kind='orchestration_attention'").fetchone()[0] == 0


def test_archived_other_role_project_does_not_starve_second_project(evaluator_env):
    import uuid
    from engineering_hub.roles import _identity
    from engineering_hub.session_hub_bridge import CAPABILITIES
    env = evaluator_env
    _, _, task, _, roles, _, request, item = setup_dispatch(env)
    env.projects.create_project(OWNER, 'other-project', 'Other', 'other-project')
    env.projects.create_project(OWNER, 'second-project', 'Second', 'second-project')
    installation = roles.owner_get(OWNER, 'backend')['installations'][0]
    env.bridge.grant_put(OWNER, installation, 1, [PROJECT, 'second-project'], list(CAPABILITIES))
    # These second-project records are synthetic, but the evaluator transition is real.
    second_task = dict(task, id=str(uuid.uuid4()), project='second-project')
    second_request = dict(request, id=str(uuid.uuid4()), project='second-project')
    second_dispatch = dict(item, id=str(uuid.uuid4()), project='second-project',
                           task_id=second_task['id'], request_id=second_request['id'], role_id='backend-second')
    with env.repo._transaction() as db:
        role = env.repo._get(db, _identity('backend'), 'coordination_role')
        second_role = dict(role, id=_identity('backend-second'), role_id='backend-second', projects=['second-project'])
        role['projects'].append('other-project')
        db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(role), role['id']))
        for kind, record in (('coordination_role', second_role), ('tracking_task', second_task),
                             ('orchestration_request', second_request), ('role_dispatch', second_dispatch)):
            db.execute('INSERT INTO hub_entities(id,kind,data) VALUES(?,?,?)',
                       (record['id'], kind, json.dumps(record)))
    env.projects.archive_project(OWNER, 'other-project', 1)
    grant = env.grant.owner_configure(OWNER, 0, [PROJECT, 'second-project'], 120)
    env.evaluator.configure(OWNER, 0, grant['revision'], True)
    assert env.evaluator.scan_once() == 1  # First project skipped.
    assert env.evaluator.scan_once() == 1  # Second project establishes an anchor.
    advance(env, 15)
    assert env.evaluator.scan_once() == 0  # Each UUID cursor wraps.
    assert env.evaluator.scan_once() == 0
    assert env.evaluator.scan_once() == 1
    assert env.evaluator.scan_once() == 1
    result = env.repo._read(second_dispatch['id'], 'role_dispatch')
    assert result['state'] == 'stale' and result['wake_delivery'] == 'not_attempted'
    assert env.evaluator.status(OWNER)['last_error'] == 'target_role_unavailable'
    assert env.repo._read(item['id'], 'role_dispatch')['state'] == 'dispatched'
