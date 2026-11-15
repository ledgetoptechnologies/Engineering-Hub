"""Real SQLite lifecycle/ACL/CAS checks; these tests perform no host effects."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
import json
import uuid

import pytest

from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.staging_resources import StagingResourceStore


OWNER = 'human:1'
GUEST = 'human:2'
PROJECT = 'engineering-hub'
COMMIT = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


@pytest.fixture
def env(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    projects = ProjectRepository(repo)
    control = object()
    now = [1800000000.0]
    store = StagingResourceStore(repo, admin_actors=frozenset({OWNER}),
                                 control_principal=control, clock=lambda: now[0])
    store.set_policy(OWNER, PROJECT, 0, {
        a: ['read', 'retain', 'cleanup', 'build', 'test', 'preview']
        for a in (OWNER, GUEST, 'codex', 'claude')})
    return store, control, now, repo, projects


def resource(env, *, actor='codex', kind='build', terminal=True, size=42,
             failed=False, project=PROJECT):
    store, control, now, _, _ = env
    job = store.bind_job(control, str(uuid.uuid4()), project=project,
                         created_by=actor, source_commit=COMMIT,
                         artifact_digest=DIGEST)
    item = store.register_resource(control, job['id'], kind, topic='topic')
    item = store.mark_resource_ready(control, item['id'], item['revision'],
                                     size_bytes=size, failed=failed)
    if terminal:
        store.update_job(control, job['id'], job['revision'], terminal=True)
        item = store.get_resource(OWNER, item['id'])
    return item, job


def expired(env, **kwargs):
    item, job = resource(env, **kwargs)
    env[2][0] += 31 * 86400
    return item, job


def test_fail_closed_without_explicit_admin_or_grants(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    store = StagingResourceStore(repo)
    with pytest.raises(HubError, match='staging_admin_required'):
        store.set_policy(OWNER, PROJECT, 0, {OWNER: ['read']})
    with pytest.raises(HubError, match='staging_forbidden'):
        store.authorize(OWNER, PROJECT, 'read')
    with pytest.raises(HubError, match='staging_control_required'):
        store.bind_job(None, str(uuid.uuid4()), project=PROJECT,
                       created_by=OWNER, source_commit=COMMIT)


def test_dynamic_projects_do_not_grant_permission(env):
    store, _, _, _, projects = env
    projects.create_project(OWNER, 'landscripting', 'LandScript', 'create')
    with pytest.raises(HubError, match='staging_forbidden'):
        store.authorize('codex', 'landscripting', 'build')
    store.set_policy(OWNER, 'landscripting', 0, {'codex': ['build']})
    assert store.authorize('codex', 'landscripting', 'build')
    with pytest.raises(HubError, match='staging_forbidden'):
        store.authorize('codex', 'landscripting', 'read')


def test_policy_cas_and_explicit_owner_read(env):
    store = env[0]
    assert store.get_policy(OWNER, PROJECT)['revision'] == 1
    with pytest.raises(HubError, match='revision_conflict'):
        store.set_policy(OWNER, PROJECT, 0, {})
    with pytest.raises(HubError, match='staging_admin_required'):
        store.set_policy(GUEST, PROJECT, 1, {})
    store.set_policy(OWNER, PROJECT, 1, {})
    with pytest.raises(HubError, match='staging_forbidden'):
        store.list_resources(OWNER, PROJECT)


def test_trusted_binding_is_immutable_and_durable(env):
    store, control, _, repo, _ = env
    item, job = resource(env)
    assert item['created_by'] == 'codex'
    assert item['source_commit'] == COMMIT
    with pytest.raises(HubError, match='staging_control_required'):
        store.register_resource(object(), job['id'], 'preview')
    with pytest.raises(HubError, match='staging_job_binding_conflict'):
        store.bind_job(control, job['id'], project=PROJECT,
                       created_by='claude', source_commit=COMMIT,
                       artifact_digest=DIGEST)
    reopened = StagingResourceStore(repo, admin_actors=frozenset({OWNER}))
    assert reopened.get_resource(OWNER, item['id']) == item


def test_default_retention_and_workspace_terminal_clock(env):
    store, control, now, _, _ = env
    workspace, job = resource(env, kind='workspace', terminal=False)
    assert workspace['expires_at'] is None
    assert workspace['protected'] == [{'reason': 'active_job', 'ref': job['id']}]
    now[0] += 300
    store.update_job(control, job['id'], job['revision'], terminal=True)
    updated = store.get_resource(OWNER, workspace['id'])
    assert datetime.fromisoformat(updated['expires_at']).timestamp() == now[0] + 86400
    for kind, days in [('preview', 3), ('build', 14), ('test_run', 30)]:
        item, _ = resource(env, kind=kind)
        assert (datetime.fromisoformat(item['expires_at']) -
                datetime.fromisoformat(item['created_at'])).total_seconds() == days * 86400


def test_agents_cannot_retain_or_cleanup_peer_resources(env):
    store = env[0]
    item, _ = expired(env, actor='claude')
    with pytest.raises(HubError, match='staging_resource_not_owned'):
        store.set_keep('codex', item['id'], 'until',
                       until=datetime.fromtimestamp(env[2][0] + 1000, timezone.utc).isoformat(),
                       reason='needed', expected_revision=item['revision'])
    plan = store.cleanup_plan('codex', PROJECT)
    assert plan['remove'] == []
    assert plan['kept'] == [{'id': item['id'], 'why': 'not_owned'}]


def test_pins_require_admin_and_agents_cannot_remove_pin(env):
    store = env[0]
    item, _ = expired(env)
    with pytest.raises(HubError, match='staging_admin_required'):
        store.set_keep('codex', item['id'], 'pinned', reason='needed', expected_revision=item['revision'])
    item = store.set_keep(OWNER, item['id'], 'pinned', reason='important', expected_revision=item['revision'])
    with pytest.raises(HubError, match='staging_human_keep_required'):
        store.set_keep('codex', item['id'], 'none', expected_revision=item['revision'])
    assert store.cleanup_plan(OWNER, PROJECT)['kept'] == [{'id': item['id'], 'why': 'kept'}]


def test_keep_until_bounded_expiry_and_cas(env):
    store, _, now, _, _ = env
    item, _ = expired(env)
    for seconds in (-1, 91 * 86400):
        with pytest.raises(HubError, match='invalid_staging_keep_time'):
            store.set_keep('codex', item['id'], 'until',
                           until=datetime.fromtimestamp(now[0] + seconds, timezone.utc).isoformat(),
                           reason='waiting', expected_revision=item['revision'])
    kept = store.set_keep('codex', item['id'], 'until',
                          until=datetime.fromtimestamp(now[0] + 100, timezone.utc).isoformat(),
                          reason='waiting', expected_revision=item['revision'])
    with pytest.raises(HubError, match='revision_conflict'):
        store.set_keep('codex', item['id'], 'none', expected_revision=item['revision'])
    assert store.cleanup_plan('codex', PROJECT)['remove'] == []
    now[0] += 101
    assert store.cleanup_plan('codex', PROJECT)['remove'][0]['id'] == kept['id']


@pytest.mark.parametrize('mode,delta', [('none', None), ('until', 50), ('until', 200)])
def test_agent_cannot_clear_shorten_or_replace_human_retention(env, mode, delta):
    store, _, now, _, _ = env
    item, _ = expired(env)
    kept = store.set_keep(OWNER, item['id'], 'until',
        until=datetime.fromtimestamp(now[0] + 100, timezone.utc).isoformat(),
        reason='Human review', expected_revision=item['revision'])
    until = datetime.fromtimestamp(now[0] + delta, timezone.utc).isoformat() if delta else None
    with pytest.raises(HubError, match='staging_human_keep_required') as error:
        store.set_keep('codex', item['id'], mode, until=until, reason='Agent change',
                       expected_revision=kept['revision'])
    assert error.value.status == 403
    assert store.get_resource(OWNER, item['id']) == kept


@pytest.mark.parametrize('protection', ['current_release', 'rollback_target', 'held_campaign', 'decision_evidence', 'latest_failure'])
def test_protection_prevents_cleanup_and_keep_removal(env, protection):
    store, control, _, _, _ = env
    item, _ = expired(env)
    item = store.set_protection(control, item['id'], item['revision'], [{'reason': protection, 'ref': 'known-record'}])
    assert store.cleanup_plan(OWNER, PROJECT)['remove'] == []
    with pytest.raises(HubError, match='staging_resource_protected'):
        store.set_keep(OWNER, item['id'], 'none', expected_revision=item['revision'])


def test_active_job_and_latest_failure_stay_protected(env):
    store, control, now, _, _ = env
    item, job = expired(env, terminal=False, failed=True)
    assert store.cleanup_plan(OWNER, PROJECT)['remove'] == []
    store.update_job(control, job['id'], job['revision'], terminal=True)
    assert store.cleanup_plan(OWNER, PROJECT)['kept'] == [{'id': item['id'], 'why': 'protected'}]


def test_unknown_size_is_not_reported_as_zero(env):
    item, _ = expired(env, size=None)
    plan = env[0].cleanup_plan(OWNER, PROJECT)
    assert plan['total_bytes'] is None
    assert plan['remove'][0]['size_bytes'] is None


def test_exact_plan_digest_actor_and_expiry(env):
    store, _, now, _, _ = env
    expired(env)
    plan = store.cleanup_plan('codex', PROJECT)
    with pytest.raises(HubError, match='staging_forbidden'):
        store.cleanup_begin('claude', plan['plan_id'], plan['digest'])
    with pytest.raises(HubError, match='staging_plan_digest_conflict'):
        store.cleanup_begin('codex', plan['plan_id'], 'different')
    now[0] += 600
    with pytest.raises(HubError, match='staging_plan_expired'):
        store.cleanup_begin('codex', plan['plan_id'], plan['digest'])


def test_revocation_and_archive_invalidate_outstanding_plan(env):
    store, _, _, _, projects = env
    project = 'client-notes'
    projects.create_project(OWNER, project, 'Client', 'create')
    store.set_policy(OWNER, project, 0, {OWNER: ['read', 'cleanup'], 'codex': ['read', 'cleanup']})
    expired(env, project=project)
    plan = store.cleanup_plan('codex', project)
    policy = store.get_policy(OWNER, project)
    policy['grants'].pop('codex')
    store.set_policy(OWNER, project, policy['revision'], policy['grants'])
    with pytest.raises(HubError, match='staging_forbidden'):
        store.cleanup_begin('codex', plan['plan_id'], plan['digest'])
    item = projects.get_project(project)
    projects.archive_project(OWNER, project, item['revision'])
    with pytest.raises(HubError, match='project_archived'):
        store.get_resource(OWNER, plan['remove'][0]['id'])


def test_plan_rechecks_pin_and_revision_atomically(env):
    store = env[0]
    item, _ = expired(env)
    plan = store.cleanup_plan(OWNER, PROJECT)
    store.set_keep(OWNER, item['id'], 'pinned', reason='keep', expected_revision=item['revision'])
    result = store.cleanup_begin(OWNER, plan['plan_id'], plan['digest'])
    assert result['operations'] == []
    assert result['skipped'] == [{'id': item['id'], 'why': 'kept'}]


def test_deleted_receipts_remain_history_but_leave_default_inventory(env):
    store, control = env[:2]
    item, _ = expired(env)
    plan = store.cleanup_plan(OWNER, PROJECT)
    operation = store.cleanup_begin(OWNER, plan['plan_id'], plan['digest'])['operations'][0]
    store.cleanup_finish(control, plan['plan_id'], item['id'], operation['expected_revision'],
                         absent=True, receipt_id=str(uuid.uuid4()))
    assert store.list_resources(OWNER, PROJECT)['items'] == []
    assert store.cleanup_plan(OWNER, PROJECT)['kept'] == []
    assert store.get_resource(OWNER, item['id'])['state'] == 'deleted'
    assert store.list_resources(OWNER, PROJECT, state='deleted')['items'][0]['id'] == item['id']


def test_deletion_blocks_pin_and_replay_never_dispatches_again(env):
    store = env[0]
    item, _ = expired(env)
    plan = store.cleanup_plan(OWNER, PROJECT)
    first = store.cleanup_begin(OWNER, plan['plan_id'], plan['digest'])
    assert first['dispatch'] is True
    operation = first['operations'][0]
    with pytest.raises(HubError, match='staging_cleanup_in_progress'):
        store.set_keep(OWNER, item['id'], 'pinned', reason='too late', expected_revision=operation['expected_revision'])
    second = store.cleanup_begin(OWNER, plan['plan_id'], plan['digest'])
    assert second['dispatch'] is False and second['reconcile_required'] is True
    assert second['operations'] == first['operations']
    assert store.cleanup_plan(OWNER, PROJECT)['remove'] == []


def test_two_simultaneous_plans_dispatch_one_operation(env):
    store = env[0]
    expired(env)
    plans = [store.cleanup_plan(OWNER, PROJECT) for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda p: store.cleanup_begin(OWNER, p['plan_id'], p['digest']), plans))
    assert sum(len(r['operations']) for r in results) == 1


@pytest.mark.parametrize('absent,final', [(True, 'deleted'), (False, 'delete_failed')])
def test_trusted_receipt_cas_idempotence_and_tombstone(env, absent, final):
    store, control, _, repo, _ = env
    item, _ = expired(env)
    plan = store.cleanup_plan(OWNER, PROJECT)
    operation = store.cleanup_begin(OWNER, plan['plan_id'], plan['digest'])['operations'][0]
    receipt_id = str(uuid.uuid4())
    with pytest.raises(HubError, match='staging_control_required'):
        store.cleanup_finish(object(), plan['plan_id'], item['id'], operation['expected_revision'], absent=absent, receipt_id=receipt_id)
    result = store.cleanup_finish(control, plan['plan_id'], item['id'], operation['expected_revision'], absent=absent, receipt_id=receipt_id)
    assert result['state'] == final
    assert result['source_commit'] == item['source_commit']
    assert store.cleanup_finish(control, plan['plan_id'], item['id'], operation['expected_revision'], absent=absent, receipt_id=receipt_id) == result
    with pytest.raises(HubError, match='staging_receipt_conflict'):
        store.cleanup_finish(control, plan['plan_id'], item['id'], operation['expected_revision'], absent=not absent, receipt_id=receipt_id)
    assert store.cleanup_plan(OWNER, PROJECT)['remove'] == []
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_cleanup_receipts').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM hub_audit WHERE action='staging.cleanup_finish'").fetchone()[0] == 1


def preflight_cleanup(env):
    store, _, _, repo, _ = env
    item, _ = expired(env)
    plan = store.cleanup_plan(OWNER, PROJECT)
    operation = store.cleanup_begin(OWNER, plan['plan_id'], plan['digest'])['operations'][0]
    with repo._transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS hub_staging_dispatch_cleanup (id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        data = dict(state='preflight', plan_id=plan['plan_id'], resource_id=item['id'],
                    resource_revision=operation['expected_revision'])
        db.execute('INSERT INTO hub_staging_dispatch_cleanup VALUES(?,?)', (operation['operation_id'], json.dumps(data)))
    return item, plan, operation


def test_not_dispatched_receipt_restores_previous_state_once_and_never_claims_deletion(env):
    store, control, _, repo, _ = env
    item, plan, operation = preflight_cleanup(env)
    args = (plan['plan_id'], item['id'], operation['expected_revision'])
    kwargs = dict(receipt_id=operation['operation_id'], reason='preflight_failed')
    with pytest.raises(HubError, match='staging_control_required'):
        store.cleanup_not_dispatched(object(), *args, **kwargs)
    restored = store.cleanup_not_dispatched(control, *args, **kwargs)
    assert restored['state'] == item['state'] == 'ready'
    assert restored['revision'] == operation['expected_revision'] + 1
    assert store.cleanup_not_dispatched(control, *args, **kwargs) == restored
    with pytest.raises(HubError, match='staging_receipt_conflict'):
        store.cleanup_not_dispatched(control, *args, **(kwargs | {'reason': 'preflight_cancelled'}))
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_cleanup_receipts').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM hub_audit WHERE action='staging.cleanup_not_dispatched'").fetchone()[0] == 1
        journal = json.loads(db.execute('SELECT data FROM hub_staging_dispatch_cleanup').fetchone()[0])
        assert journal['state'] == 'not_dispatched' and 'payload' not in journal
    assert store.cleanup_plan(OWNER, PROJECT)['remove'][0]['id'] == item['id']


@pytest.mark.parametrize('change', ['dispatching', 'unknown', 'missing-previous', 'different-resource', 'payload'])
def test_not_dispatched_never_restores_uncertain_or_unbound_operation(env, change):
    store, control, _, repo, _ = env
    item, plan, operation = preflight_cleanup(env)
    with repo._transaction() as db:
        row = db.execute('SELECT data FROM hub_staging_dispatch_cleanup').fetchone()
        journal = json.loads(row[0])
        if change in ('dispatching', 'unknown'):
            journal['state'] = change
        elif change == 'different-resource':
            journal['resource_id'] = str(uuid.uuid4())
        elif change == 'payload':
            journal['payload'] = {}
        else:
            stored = json.loads(db.execute('SELECT data FROM hub_staging_cleanup_plans').fetchone()[0])
            stored['operations'][0].pop('previous_state')
            db.execute('UPDATE hub_staging_cleanup_plans SET data=?', (json.dumps(stored),))
        db.execute('UPDATE hub_staging_dispatch_cleanup SET data=?', (json.dumps(journal),))
    with pytest.raises(HubError, match='staging_cleanup_outcome_uncertain'):
        store.cleanup_not_dispatched(control, plan['plan_id'], item['id'], operation['expected_revision'],
            receipt_id=operation['operation_id'], reason='preflight_interrupted')
    assert store.get_resource(OWNER, item['id'])['state'] == 'deleting'


def test_not_dispatched_audit_failure_rolls_back_restoration_receipt_and_journal(env, monkeypatch):
    store, control, _, repo, _ = env
    item, plan, operation = preflight_cleanup(env)
    original = repo._emit
    monkeypatch.setattr(repo, '_emit', lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('fixture failure')))
    with pytest.raises(sqlite3.OperationalError):
        store.cleanup_not_dispatched(control, plan['plan_id'], item['id'], operation['expected_revision'],
            receipt_id=operation['operation_id'], reason='preflight_failed')
    assert store.get_resource(OWNER, item['id'])['state'] == 'deleting'
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_cleanup_receipts').fetchone()[0] == 0
        assert json.loads(db.execute('SELECT data FROM hub_staging_dispatch_cleanup').fetchone()[0])['state'] == 'preflight'
    monkeypatch.setattr(repo, '_emit', original)
    assert store.cleanup_not_dispatched(control, plan['plan_id'], item['id'], operation['expected_revision'],
        receipt_id=operation['operation_id'], reason='preflight_failed')['state'] == 'ready'


@pytest.mark.parametrize('valid_binding', [True, False])
def test_not_accepted_uses_distinct_exact_rejected_payload_proof_not_no_post_claim(env, valid_binding):
    store, control, _, repo, _ = env
    item, plan, operation = preflight_cleanup(env)
    with repo._transaction() as db:
        journal = json.loads(db.execute('SELECT data FROM hub_staging_dispatch_cleanup').fetchone()[0])
        journal.update(state='rejected', reason='worker_busy', payload={
            'command_id': operation['operation_id'], 'idempotency_key': operation['operation_id'],
            'resources': [{'resource_id': item['id'] if valid_binding else str(uuid.uuid4()), 'expected_revision': 1}]})
        db.execute('UPDATE hub_staging_dispatch_cleanup SET data=?', (json.dumps(journal),))
    with pytest.raises(HubError, match='staging_cleanup_outcome_uncertain'):
        store.cleanup_not_dispatched(control, plan['plan_id'], item['id'], operation['expected_revision'],
            receipt_id=operation['operation_id'], reason='preflight_failed')
    if valid_binding:
        restored = store.cleanup_not_accepted(control, plan['plan_id'], item['id'], operation['expected_revision'],
            receipt_id=operation['operation_id'], reason='worker_busy')
        assert restored['state'] == 'ready'
        with repo._connection() as db:
            assert db.execute("SELECT count(*) FROM hub_audit WHERE action='staging.cleanup_not_accepted'").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM hub_audit WHERE action='staging.cleanup_not_dispatched'").fetchone()[0] == 0
    else:
        with pytest.raises(HubError, match='staging_cleanup_outcome_uncertain'):
            store.cleanup_not_accepted(control, plan['plan_id'], item['id'], operation['expected_revision'],
                receipt_id=operation['operation_id'], reason='worker_busy')
        assert store.get_resource(OWNER, item['id'])['state'] == 'deleting'


def test_outbox_failure_rolls_back_deleting_and_plan(env, monkeypatch):
    store, _, _, repo, _ = env
    item, _ = expired(env)
    plan = store.cleanup_plan(OWNER, PROJECT)
    original = repo._emit
    def broken(*args):
        raise sqlite3.OperationalError('test outbox failure')
    monkeypatch.setattr(repo, '_emit', broken)
    with pytest.raises(sqlite3.OperationalError):
        store.cleanup_begin(OWNER, plan['plan_id'], plan['digest'])
    assert store.get_resource(OWNER, item['id'])['state'] == 'ready'
    monkeypatch.setattr(repo, '_emit', original)
    assert len(store.cleanup_begin(OWNER, plan['plan_id'], plan['digest'])['operations']) == 1


def test_filtered_pagination_is_stable_and_scope_filtered(env):
    store = env[0]
    items = [resource(env)[0] for _ in range(3)]
    first = store.list_resources(OWNER, PROJECT, kind='build', state='ready', limit=2)
    second = store.list_resources(OWNER, PROJECT, kind='build', state='ready', after=first['next_after'], limit=2)
    assert {r['id'] for r in first['items'] + second['items']} == {r['id'] for r in items}
    assert second['next_after'] is None
    store.set_policy(OWNER, PROJECT, 1, {OWNER: ['read']})
    assert store.list_resources('codex')['items'] == []


def test_unregistered_imported_entity_is_never_deleted(env):
    import json
    store, _, _, repo, _ = env
    item, _ = expired(env)
    imported = dict(item, id=str(uuid.uuid4()))
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_entities VALUES(?,?,?)',
                   (imported['id'], 'staging_resource', json.dumps(imported)))
    plan = store.cleanup_plan(OWNER, PROJECT)
    assert [r['id'] for r in plan['remove']] == [item['id']]
    assert {'id': imported['id'], 'why': 'unknown_ownership'} in plan['kept']
    with pytest.raises(HubError, match='not_found'):
        store.get_resource(OWNER, imported['id'])


def test_pinning_race_with_cleanup_has_single_winner(env):
    store = env[0]
    item, _ = expired(env)
    plan = store.cleanup_plan(OWNER, PROJECT)
    def pin():
        try:
            return store.set_keep(OWNER, item['id'], 'pinned', reason='keep', expected_revision=item['revision'])
        except HubError as exc:
            assert exc.status == 409
            return None
    with ThreadPoolExecutor(2) as pool:
        pinner = pool.submit(pin)
        cleaner = pool.submit(store.cleanup_begin, OWNER, plan['plan_id'], plan['digest'])
        pin_result, clean_result = pinner.result(), cleaner.result()
    assert (pin_result is not None) != bool(clean_result['operations'])
    actual = store.get_resource(OWNER, item['id'])
    assert actual['state'] == 'deleting' or actual['keep']['mode'] == 'pinned'


def test_job_reconciliation_preserves_resource_specific_protection(env):
    store, control, _, _, _ = env
    item, job = resource(env, terminal=False)
    protection = {'reason': 'decision_evidence', 'ref': 'decision-id'}
    store.set_protection(control, item['id'], item['revision'], [protection])
    store.update_job(control, job['id'], job['revision'], terminal=True)
    assert store.get_resource(OWNER, item['id'])['protected'] == [protection]


def test_new_resource_inherits_existing_job_protections(env):
    store, control, _, _, _ = env
    _, job = resource(env, terminal=False)
    protection = {'reason': 'held_campaign', 'ref': 'campaign'}
    store.update_job(control, job['id'], job['revision'], terminal=False, protected=[protection])
    item = store.register_resource(control, job['id'], 'preview')
    assert protection in item['protected']


def test_trusted_job_and_result_cas_and_state_are_enforced(env):
    store, control, _, _, _ = env
    item, job = resource(env)
    with pytest.raises(HubError, match='revision_conflict'):
        store.update_job(control, job['id'], job['revision'], terminal=True)
    with pytest.raises(HubError, match='staging_job_terminal'):
        store.register_resource(control, job['id'], 'preview')
    with pytest.raises(HubError, match='staging_resource_state_conflict'):
        store.mark_resource_ready(control, item['id'], item['revision'], size_bytes=20)


def test_trusted_inspection_survives_revocation_without_granting_access(env):
    store, control, _, _, _ = env
    item, job = resource(env)
    store.set_policy(OWNER, PROJECT, 1, {})
    with pytest.raises(HubError, match='staging_forbidden'):
        store.get_resource('codex', item['id'])
    assert store.inspect_resource(control, item['id']) == item
    assert store.inspect_job(control, job['id'])['revision'] == 2
    with pytest.raises(HubError, match='staging_control_required'):
        store.inspect_resource(object(), item['id'])
    with pytest.raises(HubError, match='staging_control_required'):
        store.inspect_job('codex', job['id'])


def preview(env, *, terminal=True):
    store, control, _, _, _ = env
    job = store.bind_job(control, str(uuid.uuid4()), project=PROJECT,
                         created_by='codex', source_commit=COMMIT)
    item = store.register_resource(control, job['id'], 'preview')
    item = store.mark_resource_ready(control, item['id'], item['revision'],
                                     artifact_digest=DIGEST, size_bytes=12,
                                     preview_path='/staging/previews/' + item['id'])
    if terminal:
        store.update_job(control, job['id'], job['revision'], terminal=True)
    return store.inspect_resource(control, item['id'])


def test_preview_binding_requires_both_grants_and_current_revision(env):
    store = env[0]
    item = preview(env)
    bound, revision = store.preview_binding(GUEST, item['id'])
    assert bound == item and revision == 1
    store.set_policy(OWNER, PROJECT, 1, {GUEST: ['read']})
    with pytest.raises(HubError, match='staging_forbidden'):
        store.preview_binding(GUEST, item['id'])
    store.set_policy(OWNER, PROJECT, 2, {GUEST: ['preview']})
    with pytest.raises(HubError, match='staging_forbidden'):
        store.preview_binding(GUEST, item['id'])
    store.set_policy(OWNER, PROJECT, 3, {GUEST: ['read', 'preview']})
    assert store.preview_binding(GUEST, item['id'])[1] == 4


def test_preview_binding_denies_active_job_expiry_and_deleting(env):
    store, _, now, _, _ = env
    active = preview(env, terminal=False)
    with pytest.raises(HubError, match='staging_preview_unavailable'):
        store.preview_binding(OWNER, active['id'])
    item = preview(env)
    now[0] += 3 * 86400
    with pytest.raises(HubError, match='staging_preview_expired'):
        store.preview_binding(OWNER, item['id'])
    until = datetime.fromtimestamp(now[0] + 60, timezone.utc).isoformat()
    kept = store.set_keep(OWNER, item['id'], 'until', until=until, reason='review', expected_revision=item['revision'])
    assert store.preview_binding(OWNER, item['id'])[0] == kept
    store.set_keep(OWNER, item['id'], 'none', expected_revision=kept['revision'])
    plan = store.cleanup_plan(OWNER, PROJECT)
    store.cleanup_begin(OWNER, plan['plan_id'], plan['digest'])
    with pytest.raises(HubError, match='staging_preview_unavailable'):
        store.preview_binding(OWNER, item['id'])


def test_preview_binding_requires_immutable_output_provenance(env):
    store, control, _, _, _ = env
    job = store.bind_job(control, str(uuid.uuid4()), project=PROJECT,
                         created_by='codex', source_commit=COMMIT)
    item = store.register_resource(control, job['id'], 'preview')
    store.mark_resource_ready(control, item['id'], item['revision'],
                              preview_path='/staging/previews/' + item['id'])
    store.update_job(control, job['id'], job['revision'], terminal=True)
    with pytest.raises(HubError, match='staging_preview_unavailable'):
        store.preview_binding(OWNER, item['id'])


def test_terminal_lookup_index_preserves_existing_audit_and_is_idempotent(tmp_path):
    repo = HubRepository(tmp_path / 'populated.sqlite3')
    entries = [(f'human:{i + 1}', 'work.failed', str(uuid.uuid4()), 2, '2026-01-01T00:00:00+00:00')
               for i in range(1000)]
    with repo._transaction() as db:
        db.executemany('INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES(?,?,?,?,?)', entries)
        before = [tuple(row) for row in db.execute('SELECT * FROM hub_audit ORDER BY sequence')]
    StagingResourceStore(repo)
    StagingResourceStore(repo)
    with repo._connection() as db:
        after = [tuple(row) for row in db.execute('SELECT * FROM hub_audit ORDER BY sequence')]
        indexes = db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='hub_audit_terminal_lookup'").fetchall()
    assert before == after and len(indexes) == 1


def test_terminal_lookup_index_creation_failure_rolls_back_staging_schema(tmp_path):
    repo = HubRepository(tmp_path / 'collision.sqlite3')
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_audit_terminal_lookup (value TEXT)')
        db.execute("INSERT INTO hub_audit(actor,action,entity_id,revision,created_at) VALUES('human:1','test','retained',1,'2026-01-01')")
        before = [tuple(row) for row in db.execute('SELECT * FROM hub_audit ORDER BY sequence')]
    with pytest.raises(sqlite3.OperationalError):
        StagingResourceStore(repo)
    with repo._connection() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM hub_audit ORDER BY sequence')] == before
        assert db.execute("SELECT type FROM sqlite_master WHERE name='hub_audit_terminal_lookup'").fetchone()['type'] == 'table'
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_staging_policy'").fetchone() is None
