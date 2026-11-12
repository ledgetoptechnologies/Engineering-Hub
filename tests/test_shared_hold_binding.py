"""Exact read-only hold recognition; real journals/queue, synthetic lost ACK."""
import json
import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from engineering_hub.execution_policy import SharedExecutionPolicies
from engineering_hub.repository import HubRepository
import engineering_hub.shared_hold_binding as hold_binding
from engineering_hub.shared_hold_binding import modular_hold_matches
from engineering_hub.staging_controller import StagingController, compose_queue
from engineering_hub.staging_resources import StagingResourceStore
from engineering_hub.work_queue import WorkAction
from tests.test_hub_staging_controller import Client, payload


def pending(repo, client):
    principal = object()
    resources = StagingResourceStore(repo, control_principal=principal)
    queue, actions = compose_queue(repo, None, {'engineering-hub': ['static']}, 'staging-pc')
    queue.actions['pilot'] = WorkAction('staging-pc', project='engineering-hub')
    policies = SharedExecutionPolicies(repo, queue)
    controller = StagingController(repo, queue, client, resources=resources,
        control_principal=principal, actions=actions, execution_policies=policies)
    return controller, policies


@pytest.fixture
async def held(tmp_path):
    path = tmp_path / 'hub.db'
    repo = HubRepository(path)
    principal = object()
    resources = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}), control_principal=principal)
    resources.set_policy('human:1', 'engineering-hub', 0, {'codex': ['read', 'build', 'test']})
    queue, actions = compose_queue(repo, None, {'engineering-hub': ['static']}, 'staging-pc')
    client = Client(); client.lost_ack = True
    old = StagingController(repo, queue, client, resources=resources,
        control_principal=principal, actions=actions)
    submission = await old.submit('codex', payload(repo))
    result = await old.run_once()
    assert result['phase'] == 'held' and client.posts == 1
    reopened = HubRepository(path)
    controller, policies = pending(reopened, client)
    assert controller.worker != old.worker
    return SimpleNamespace(repo=reopened, controller=controller, policies=policies,
        client=client, job_id=submission['job']['id'], resource_ids=submission['resource_ids'])


def recognize(state, db, reservation=None):
    row = db.execute('SELECT machine_id,job_id,generation FROM hub_work_machines WHERE job_id=?',
                     (state.job_id,)).fetchone()
    return modular_hold_matches(db, repo=state.repo, queue=state.controller.queue,
        policies=state.policies, modular_controller=state.controller,
        reservation=row if reservation is None else reservation)


def journal(db, state, change):
    row = db.execute("SELECT id,data FROM hub_staging_submissions WHERE json_extract(data,'$.job_id')=?",
                     (state.job_id,)).fetchone()
    item = json.loads(row['data']); change(item)
    db.execute('UPDATE hub_staging_submissions SET data=? WHERE id=?', (json.dumps(item), row['id']))


@pytest.mark.asyncio
async def test_lost_ack_restart_recognizes_but_preserves_all_rows_and_no_post(held):
    state = held
    with state.repo._transaction() as db:
        before = list(db.iterdump()); changes = db.total_changes
        with patch.object(state.repo, '_connection', side_effect=AssertionError('second connection')):
            assert recognize(state, db) is True
        assert db.total_changes == changes and list(db.iterdump()) == before
    assert state.client.posts == 1
    assert state.controller.queue.get_job(state.job_id)['state'] == 'held'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,claim', [('dispatching', True), ('accepted', True), ('queued', False)])
async def test_original_claimed_generation_or_queued_gap_is_only_preserved(held, phase, claim):
    with held.repo._transaction() as db:
        db.execute("UPDATE hub_work_jobs SET state='claimed',lease_until=lease_until+100 WHERE id=?", (held.job_id,))
        def change(item):
            item['phase'] = phase
            if not claim: item.pop('claim', None)
        journal(db, held, change)
        before = list(db.iterdump()); changes = db.total_changes
        assert recognize(held, db) is True
        assert db.total_changes == changes and list(db.iterdump()) == before
    assert held.client.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['job', 'actor', 'request', 'fingerprint', 'source', 'command', 'phase',
    'missingclaim', 'claimworker', 'claimfence', 'target', 'resourcekind', 'resourceid', 'duplicateids',
    'review_task', 'review_revision', 'binding_scheme', 'binding_digest'])
async def test_submission_tampering_retains_enrollment_veto(held, change):
    def mutate(item):
        if change == 'job': item['job_id'] = str(uuid.uuid4())
        elif change == 'actor': item['actor'] = 'claude'
        elif change == 'request': item['request_id'] = str(uuid.uuid4())
        elif change == 'fingerprint': item['topic'] = 'changed'
        elif change == 'source': item['source_digest'] = 'c' * 64
        elif change == 'command': item['command_id'] = 'invalid'
        elif change == 'phase': item['phase'] = 'verified'
        elif change == 'missingclaim': item.pop('claim')
        elif change == 'claimworker': item['claim']['worker'] = 'executor:staging-service:' + str(uuid.uuid4())
        elif change == 'claimfence': item['claim']['fence'] += 1
        elif change == 'target': item['claim']['target']['source_commit'] = 'b' * 40
        elif change == 'review_task': item['review_task_id'] = str(uuid.uuid4())
        elif change == 'review_revision': item['review_task_revision'] += 1
        elif change == 'binding_scheme': item['source_binding']['scheme'] = 'unknown'
        elif change == 'binding_digest': item['source_binding']['archive_sha256'] = 'c' * 64
        elif change == 'resourcekind': item['resource_ids']['foreign'] = item['resource_ids'].pop('workspace')
        elif change == 'resourceid': item['resource_ids']['workspace'] = str(uuid.uuid4())
        else: item['resource_ids']['workspace'] = item['resource_ids']['preview']
    with held.repo._transaction() as db:
        journal(db, held, mutate)
        before = list(db.iterdump()); changes = db.total_changes
        assert recognize(held, db) is False
        assert list(db.iterdump()) == before and db.total_changes == changes


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['task_id', 'submission_id', 'review_revision', 'review_revision_bool'])
async def test_coherent_target_rewrite_cannot_rebind_original_submission(held, field):
    # Update the claim and queue fingerprint too: denial must come from the
    # original submission binding, not merely a stale duplicate field.
    with held.repo._transaction() as db:
        row = db.execute('SELECT * FROM hub_work_jobs WHERE id=?', (held.job_id,)).fetchone()
        target = json.loads(row['target'])
        target['review_revision' if field == 'review_revision_bool' else field] = (
            True if field == 'review_revision_bool' else
            target['review_revision'] + 1 if field == 'review_revision' else str(uuid.uuid4()))
        target_json = json.dumps(target, sort_keys=True, separators=(',', ':'))
        fingerprint = json.dumps([row['action_key'], row['environment'], row['machine_id'], row['project'],
                                  bool(row['requires_execution_proof']), row['approval_id'], target_json],
                                 separators=(',', ':'))
        db.execute('UPDATE hub_work_jobs SET target=?,fingerprint=? WHERE id=?',
                   (target_json, fingerprint, held.job_id))
        def change(item):
            item['claim']['target'] = target
            if field == 'review_revision_bool':
                # True == 1 in Python: also make the submission revision 1
                # and recompute its fingerprint to test strict target typing.
                item['review_task_revision'] = 1
        journal(db, held, change)
        if field == 'review_revision_bool':
            row = db.execute('SELECT id,data FROM hub_staging_submissions WHERE json_extract(data,\'$.job_id\')=?',
                             (held.job_id,)).fetchone()
            item = json.loads(row['data'])
            public = {key: item[key] for key in ('request_id', 'review_task_id', 'review_task_revision',
                      'project', 'recipe', 'source_commit', 'source_digest', 'source_binding', 'topic')}
            digest = hashlib.sha256(json.dumps(public, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            db.execute('UPDATE hub_staging_submissions SET fingerprint=? WHERE id=?', (digest, row['id']))
        before = list(db.iterdump()); changes = db.total_changes
        assert recognize(held, db) is False
        assert list(db.iterdump()) == before and db.total_changes == changes


@pytest.mark.asyncio
@pytest.mark.parametrize('column,value', [('machine_id', 'other-host'), ('worker', 'codex:invalid'),
    ('state', 'succeeded'), ('fence', 999), ('fingerprint', 'changed'), ('project', 'other'),
    ('action_key', 'pilot'), ('environment', 'production'), ('requires_execution_proof', 0)])
async def test_queue_row_tampering_denied(held, column, value):
    with held.repo._transaction() as db:
        db.execute('UPDATE hub_work_jobs SET ' + column + '=? WHERE id=?', (value, held.job_id))
        assert recognize(held, db) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['entityowner', 'ownership', 'extraresource', 'terminaljob', 'deleting',
    'foreignprotection', 'cleanuppermitted', 'duplicatejournal', 'malformedjson', 'duplicatejsonkey', 'missingtable'])
async def test_resource_ambiguity_or_malformed_schema_denied(held, change):
    with held.repo._transaction() as db:
        identity = held.resource_ids['workspace']
        if change in ('entityowner', 'deleting', 'foreignprotection'):
            row = db.execute('SELECT data FROM hub_entities WHERE id=?', (identity,)).fetchone()
            item = json.loads(row['data'])
            if change == 'entityowner': item['created_by'] = 'claude'
            elif change == 'deleting': item['state'] = 'deleting'
            else: item['protected'].append({'reason': 'external', 'ref': str(uuid.uuid4())})
            db.execute('UPDATE hub_entities SET data=? WHERE id=?', (json.dumps(item), identity))
        elif change == 'ownership':
            db.execute("UPDATE hub_staging_resource_ownership SET source_commit=? WHERE id=?", ('d'*40, identity))
        elif change == 'extraresource':
            db.execute('INSERT INTO hub_staging_resource_ownership VALUES(?,?,?,?,?)',
                       (str(uuid.uuid4()), held.job_id, 'engineering-hub', 'codex', 'a'*40))
        elif change == 'terminaljob':
            row = db.execute('SELECT data FROM hub_staging_resource_jobs WHERE id=?', (held.job_id,)).fetchone()
            item = json.loads(row['data']); item['terminal'] = True
            db.execute('UPDATE hub_staging_resource_jobs SET data=? WHERE id=?', (json.dumps(item), held.job_id))
        elif change == 'cleanuppermitted':
            db.execute('INSERT INTO hub_staging_dispatch_cleanup VALUES(?,?)',
                       (str(uuid.uuid4()), json.dumps({'state': 'preflight', 'resource_id': identity})))
        elif change == 'duplicatejournal':
            row = db.execute('SELECT * FROM hub_staging_submissions').fetchone()
            db.execute('INSERT INTO hub_staging_submissions VALUES(?,?,?,?,?)',
                       (str(uuid.uuid4()), 'claude', str(uuid.uuid4()), row['fingerprint'], row['data']))
        elif change == 'malformedjson':
            db.execute('DROP INDEX hub_staging_submission_job')
            db.execute("UPDATE hub_staging_submissions SET data='not-json'")
        elif change == 'duplicatejsonkey':
            row = db.execute('SELECT data FROM hub_staging_submissions').fetchone()
            raw = row['data'][:-1] + ',"phase":"held"}'
            db.execute('UPDATE hub_staging_submissions SET data=?', (raw,))
        else: db.execute('DROP TABLE hub_staging_dispatch_cleanup')
        assert recognize(held, db) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['principal', 'client', 'actions', 'registry', 'worker'])
async def test_private_context_tamper_denied(held, change):
    if change == 'principal': held.controller.principal = object()
    elif change == 'client': held.controller.client = Client()
    elif change == 'actions': held.controller.actions.clear()
    elif change == 'registry': held.controller.execution_policies = object()
    else: held.controller.worker = object()
    with held.repo._transaction() as db:
        assert recognize(held, db) is False


@pytest.mark.asyncio
async def test_wrong_reservation_or_connection_never_matches(held, tmp_path):
    with held.repo._transaction() as db:
        assert recognize(held, db, {'machine_id': 'staging-pc', 'job_id': held.job_id, 'generation': 999}) is False
    other = HubRepository(tmp_path / 'other.db')
    with other._transaction() as db:
        assert modular_hold_matches(db, repo=held.repo, queue=held.controller.queue,
            policies=held.policies, modular_controller=held.controller,
            reservation={'machine_id': 'staging-pc', 'job_id': held.job_id, 'generation': 1}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate', ['source_id', 'command_id', 'cross-role'])
async def test_other_submission_cannot_alias_source_or_operation_identity(held, duplicate):
    with held.repo._transaction() as db:
        row = db.execute('SELECT * FROM hub_staging_submissions').fetchone()
        original = json.loads(row['data']); other = dict(original)
        for key in ('id', 'job_id', 'request_id', 'source_id', 'command_id'):
            other[key] = str(uuid.uuid4())
        if duplicate == 'cross-role': other['source_id'] = original['command_id']
        else: other[duplicate] = original[duplicate]
        db.execute('INSERT INTO hub_staging_submissions VALUES(?,?,?,?,?)',
                   (other['id'], 'claude', other['request_id'], row['fingerprint'], json.dumps(other)))
        assert recognize(held, db) is False


@pytest.mark.asyncio
async def test_nontransactional_connection_denied_without_changes(held):
    with held.repo._connection() as db:
        assert not db.in_transaction
        changes = db.total_changes
        assert recognize(held, db) is False
        assert db.total_changes == changes


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', ['MAX_HISTORY_ROWS', 'MAX_METADATA_BYTES', 'MAX_RECORD_BYTES'])
async def test_exact_scan_budget_accepts_and_one_over_denies_without_mutation(held, monkeypatch, limit):
    with held.repo._transaction() as db:
        # UTF-8 bytes, rather than character count, must drive the aggregate.
        db.execute('INSERT INTO hub_staging_dispatch_cleanup VALUES(?,?)',
            (str(uuid.uuid4()), json.dumps({'resource_id': 'unrelated', 'detail': 'é' * 13}, ensure_ascii=False)))
        records = [db.execute('SELECT target FROM hub_work_jobs WHERE id=?', (held.job_id,)).fetchone()[0]]
        records += [row[0] for row in db.execute('SELECT data FROM hub_staging_submissions')]
        records += [db.execute('SELECT data FROM hub_staging_resource_jobs WHERE id=?', (held.job_id,)).fetchone()[0]]
        records += [row[0] for row in db.execute("SELECT data FROM hub_entities WHERE kind='staging_resource'")]
        records += [row[0] for row in db.execute('SELECT data FROM hub_staging_dispatch_cleanup')]
        expected = {'MAX_HISTORY_ROWS': len(records),
                    'MAX_METADATA_BYTES': sum(len(record.encode('utf-8')) for record in records),
                    'MAX_RECORD_BYTES': max(len(record.encode('utf-8')) for record in records)}[limit]
        before = list(db.iterdump()); changes = db.total_changes
        monkeypatch.setattr(hold_binding, limit, expected)
        with patch.object(held.repo, '_connection', side_effect=AssertionError('secondary connection')):
            assert recognize(held, db) is True
            monkeypatch.setattr(hold_binding, limit, expected - 1)
            assert recognize(held, db) is False
        assert db.total_changes == changes and list(db.iterdump()) == before


@pytest.mark.parametrize('kind', ['characters', 'utf8', 'row', 'aggregate'])
def test_scan_budget_rejects_before_json_parse(monkeypatch, kind):
    monkeypatch.setattr(hold_binding, 'MAX_RECORD_BYTES', 32)
    budget = hold_binding._Budget()
    raw = '{"padding":"' + 'x' * 22 + '"}'
    if kind == 'utf8': raw = '{"é":"' + 'é' * 12 + '"}'
    elif kind == 'row':
        monkeypatch.setattr(hold_binding, 'MAX_HISTORY_ROWS', 0)
        raw = '{}'
    elif kind == 'aggregate':
        monkeypatch.setattr(hold_binding, 'MAX_METADATA_BYTES', 1)
        raw = '{}'
    with patch.object(hold_binding.json, 'loads', side_effect=AssertionError('parse before budget')) as parser:
        with pytest.raises(ValueError):
            hold_binding._json(raw, budget)
        parser.assert_not_called()
    assert budget.rows == budget.bytes == 0
