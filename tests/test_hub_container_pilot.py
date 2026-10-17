"""Controller unit tests with typed simulated evidence, not a real Docker pilot."""
import asyncio
from dataclasses import replace
import hashlib
import json
import uuid

import pytest

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.container_pilot_client import (
    ContainerPilotEvidence, ContainerPilotRequest, ContainerPilotResponse,
)
from engineering_hub.repository import HubError, HubRepository
from tests.test_hub_container_pilot_manifest import make_manifest_data


COMMIT = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64
ROLLBACK_IMAGE = 'sha256:' + 'd' * 64
PRINCIPAL = 'executor:container-pilot'
ACTIONS = ['deploy_container', 'smoke_container', 'rollback_container', 'smoke_container_rollback']


def test_noncanonical_direct_binding_cannot_poison_canonical_restart(repo, target):
    pretty = json.dumps(json.loads(target.canonical_manifest), indent=2)
    altered = replace(target, canonical_manifest=pretty)
    with pytest.raises(HubError, match='container_pilot_manifest_unverified'):
        ContainerPilotWorkflow(repo, FakeClient(), [altered])
    ContainerPilotWorkflow(repo, FakeClient(), [target])
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    ContainerPilotWorkflow(reopened, FakeClient(), [target])


def pilot_manifest():
    data = make_manifest_data()
    data['candidate']['image_config_id'] = IMAGE
    data['candidate']['source_commit'] = COMMIT
    data['rollback']['image_config_id'] = ROLLBACK_IMAGE
    data['rollback']['source_commit'] = 'f' * 40
    return PilotManifest.from_bytes(json.dumps(data).encode('utf-8'))


MANIFEST = pilot_manifest().digest()


class FakeClient:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.submissions = []
        self.inspections = []

    def response(self, command):
        outcome = self.outcomes.get(command.action, 'succeeded')
        if isinstance(outcome, BaseException):
            raise outcome
        rollback = command.action in ACTIONS[2:]
        response = ContainerPilotResponse(
            **command.model_dump(), state=outcome, reason=None, replayed=False,
            evidence=ContainerPilotEvidence(container_id='e' * 64,
                image_config_id=ROLLBACK_IMAGE if rollback else IMAGE,
                health='healthy', evidence_verified=True))
        return response

    async def submit_command(self, command):
        assert type(command) is ContainerPilotRequest
        self.submissions.append(command)
        return self.response(command)

    async def get_command(self, command):
        self.inspections.append(command)
        return self.response(command)


@pytest.fixture
def repo(tmp_path):
    return HubRepository(tmp_path / 'hub.sqlite3', execution_principal=PRINCIPAL)


@pytest.fixture
def target():
    return ContainerPilotTarget.from_manifest(pilot_manifest())


def reviewed_task(repo, key='pilot'):
    task = repo.create_task('human:1234', 'engineering-hub', 'Container pilot', key)
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    return repo.review_task('claude', task['id'], task['revision'], COMMIT, 'approve')


def ready_run(repo, target, client=None):
    workflow = ContainerPilotWorkflow(repo, client or FakeClient(), [target])
    task = reviewed_task(repo)
    return workflow, workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)


@pytest.mark.asyncio
async def test_success_two_phases_stable_ids_and_reopen_no_posts(repo, target):
    client = FakeClient()
    workflow, run = ready_run(repo, target, client)
    result = await workflow.advance(run['id'])
    assert result['status'] == 'succeeded'
    assert [c.action for c in client.submissions] == ACTIONS[:2]
    for command in client.submissions:
        assert command.command_id == str(uuid.uuid5(uuid.UUID(run['id']), 'container_pilot_v1:' + command.action))
        assert command.manifest_digest == MANIFEST
        assert command.candidate_commit == COMMIT
        assert command.fencing_generation == run['fencing_generation']
    assert await workflow.advance(run['id']) == result
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    next_client = FakeClient()
    assert await ContainerPilotWorkflow(reopened, next_client, [target]).advance(run['id']) == result
    assert next_client.submissions == []
    assert len(client.submissions) == 2


@pytest.mark.asyncio
async def test_smoke_failure_requires_four_distinct_commands_and_rollback_health(repo, target):
    client = FakeClient({'smoke_container': 'failed'})
    workflow, run = ready_run(repo, target, client)
    result = await workflow.advance(run['id'])
    assert result['status'] == 'rolled_back'
    assert [c.action for c in client.submissions] == ACTIONS
    assert len({c.command_id for c in client.submissions}) == 4
    assert all(c.manifest_digest == MANIFEST and c.candidate_commit == COMMIT for c in client.submissions)
    assert repo.get_task(run['task_id'])['repair_count'] == 1
    assert (await workflow.advance(run['id']))['status'] == 'rolled_back'
    assert len(client.submissions) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['failed', 'held', 'running', TimeoutError()])
async def test_rollback_smoke_failure_or_unknown_retains_target_and_no_repost(repo, target, outcome):
    client = FakeClient({'smoke_container': 'failed', 'smoke_container_rollback': outcome})
    workflow, run = ready_run(repo, target, client)
    assert (await workflow.advance(run['id']))['status'] == 'failed'
    assert [c.action for c in client.submissions] == ACTIONS
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    next_client = FakeClient()
    assert (await ContainerPilotWorkflow(reopened, next_client, [target]).advance(run['id']))['status'] == 'failed'
    assert next_client.submissions == []
    task = reviewed_task(repo, 'other-pilot')
    with pytest.raises(HubError, match='active_run'):
        workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ACTIONS[2:])
@pytest.mark.parametrize('wrong', ['candidate_image', 'unverified', 'unhealthy'])
async def test_rollback_success_requires_exact_rollback_image_and_healthy_smoke(repo, target, action, wrong):
    class BadRollbackClient(FakeClient):
        def response(self, command):
            response = super().response(command)
            if command.action != action:
                return response
            changes = {'candidate_image': {'image_config_id': IMAGE},
                       'unverified': {'evidence_verified': False},
                       'unhealthy': {'health': 'unhealthy'}}[wrong]
            return response.model_copy(update={'evidence': response.evidence.model_copy(update=changes)})
    client = BadRollbackClient({'smoke_container': 'failed'})
    workflow, run = ready_run(repo, target, client)
    result = await workflow.advance(run['id'])
    # Deploy/rollback effects do not themselves establish health; smoke must.
    if action == 'rollback_container' and wrong == 'unhealthy':
        assert result['status'] == 'rolled_back'
        assert client.submissions[-1].action == 'smoke_container_rollback'
    else:
        assert result['status'] == 'failed'
        assert client.submissions[-1].action == action


@pytest.mark.asyncio
async def test_pause_after_failure_prevents_rollback_effect(repo, target):
    class PausingFailureClient(FakeClient):
        async def submit_command(self, command):
            response = await super().submit_command(command)
            if command.action == 'smoke_container':
                repo.set_pause('human:1234', True)
            return response
    client = PausingFailureClient({'smoke_container': 'failed'})
    workflow, run = ready_run(repo, target, client)
    assert (await workflow.advance(run['id']))['status'] == 'failed'
    assert [c.action for c in client.submissions] == ACTIONS[:2]


@pytest.mark.asyncio
async def test_three_failed_candidates_exhaust_repair_budget(repo, target):
    client = FakeClient({'smoke_container': 'failed'})
    workflow, run = ready_run(repo, target, client)
    for attempt in range(3):
        assert (await workflow.advance(run['id']))['status'] == 'rolled_back'
        task = repo.get_task(run['task_id'])
        assert task['repair_count'] == attempt + 1
        if attempt < 2:
            run = workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    with pytest.raises(HubError, match='repair_limit'):
        workflow.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    assert len(client.submissions) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [TimeoutError(), 'held', 'running', 'pending'])
async def test_unknown_candidate_is_held_no_rollback_or_repost_inspect_only(repo, target, outcome):
    client = FakeClient({'deploy_container': outcome})
    workflow, run = ready_run(repo, target, client)
    assert (await workflow.advance(run['id']))['status'] == 'held'
    assert [c.action for c in client.submissions] == ACTIONS[:1]
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    next_client = FakeClient()
    recovered = ContainerPilotWorkflow(reopened, next_client, [target])
    assert (await recovered.advance(run['id']))['status'] == 'held'
    assert next_client.submissions == []
    assert (await recovered.inspect(run['id'], ACTIONS[0])).state == 'succeeded'
    assert next_client.inspections == client.submissions
    assert repo.get_run(run['id'])['status'] == 'held'


@pytest.mark.asyncio
@pytest.mark.parametrize('alteration', ['echo', 'image', 'unverified', 'missing_container', 'health'])
async def test_untrusted_success_evidence_holds(repo, target, alteration):
    class CorruptClient(FakeClient):
        def response(self, command):
            response = super().response(command)
            if alteration == 'echo':
                return response.model_copy(update={'run_id': str(uuid.uuid4())})
            if alteration == 'health' and command.action != 'smoke_container':
                return response
            changes = {
                'image': {'image_config_id': 'sha256:' + '0' * 64},
                'unverified': {'evidence_verified': False},
                'missing_container': {'container_id': None},
                'health': {'health': 'unhealthy'},
            }[alteration]
            return response.model_copy(update={'evidence': response.evidence.model_copy(update=changes)})
    client = CorruptClient()
    workflow, run = ready_run(repo, target, client)
    assert (await workflow.advance(run['id']))['status'] == 'held'
    assert [c.action for c in client.submissions] == (ACTIONS[:2] if alteration == 'health' else ACTIONS[:1])


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [dict(candidate_commit='0' * 40), dict(artifact_digest='sha256:' + '0' * 64),
    dict(manifest_digest='sha256:' + '0' * 64)])
async def test_manifest_mutation_before_io_denied(repo, target, changes):
    _, run = ready_run(repo, target)
    client = FakeClient()
    with pytest.raises(HubError, match='container_pilot_manifest_'):
        await ContainerPilotWorkflow(repo, client, [replace(target, **changes)]).advance(run['id'])
    assert client.submissions == []
    assert repo.get_run(run['id'])['status'] == 'pending'


@pytest.mark.asyncio
async def test_complete_binding_immutable_before_first_io(repo, target):
    _, run = ready_run(repo, target)
    client = FakeClient()
    changed = replace(target, rollback_image_config_id='sha256:' + '0' * 64)
    with pytest.raises(HubError, match='container_pilot_manifest_changed'):
        await ContainerPilotWorkflow(repo, client, [changed]).advance(run['id'])
    assert client.submissions == []


@pytest.mark.asyncio
async def test_complete_binding_mutation_after_restart_denied(repo, target):
    workflow, run = ready_run(repo, target, FakeClient({'deploy_container': TimeoutError()}))
    await workflow.advance(run['id'])
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    client = FakeClient()
    changed = replace(target, rollback_commit='0' * 40)
    with pytest.raises(HubError, match='container_pilot_manifest_changed'):
        await ContainerPilotWorkflow(reopened, client, [changed]).advance(run['id'])
    assert client.submissions == []


@pytest.mark.asyncio
async def test_legacy_run_without_explicit_container_manifest_denied(repo, target):
    task = reviewed_task(repo)
    run = repo.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id)
    client = FakeClient()
    with pytest.raises(HubError, match='container_pilot_manifest_mismatch'):
        await ContainerPilotWorkflow(repo, client, [target]).advance(run['id'])
    assert client.submissions == []


def test_unreviewed_and_self_review_denied(repo, target):
    task = repo.create_task('human:1234', 'engineering-hub', 'Unreviewed', 'unreviewed')
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], COMMIT)
    with pytest.raises(HubError, match='independent_review_required'):
        repo.review_task('codex', task['id'], task['revision'], COMMIT, 'approve')
    with pytest.raises(HubError, match='review_required'):
        ContainerPilotWorkflow(repo, FakeClient(), [target]).create_run(
            'codex', task['id'], task['revision'], IMAGE, target.target_id)


@pytest.mark.asyncio
async def test_pause_before_effect_and_between_candidate_phases(repo, target):
    class PausingClient(FakeClient):
        async def submit_command(self, command):
            response = await super().submit_command(command)
            repo.set_pause('human:1234', True)
            return response
    client = PausingClient()
    workflow, run = ready_run(repo, target, client)
    repo.set_pause('human:1234', True)
    with pytest.raises(HubError, match='paused'):
        await workflow.advance(run['id'])
    assert client.submissions == []
    assert repo.get_run(run['id'])['status'] == 'pending'
    repo.set_pause('human:1234', False)
    assert (await workflow.advance(run['id']))['status'] == 'held'
    assert [c.action for c in client.submissions] == ACTIONS[:1]


@pytest.mark.asyncio
async def test_baseexception_durably_reserves_unknown_and_reopen_never_posts(repo, target):
    class Crash(BaseException):
        pass
    crashing = FakeClient({'deploy_container': Crash()})
    workflow, run = ready_run(repo, target, crashing)
    with pytest.raises(Crash):
        await workflow.advance(run['id'])
    with repo._connection() as db:
        row = db.execute('SELECT payload,state FROM hub_container_pilot_commands WHERE run_id=?', (run['id'],)).fetchone()
        assert row['state'] == 'unknown'
        assert json.loads(row['payload']) == crashing.submissions[0].model_dump()
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    client = FakeClient()
    assert (await ContainerPilotWorkflow(reopened, client, [target]).advance(run['id']))['status'] == 'held'
    assert client.submissions == []


@pytest.mark.asyncio
async def test_concurrent_advance_has_no_duplicate_posts(repo, target):
    entered, release = asyncio.Event(), asyncio.Event()
    class BlockingClient(FakeClient):
        async def submit_command(self, command):
            if command.action == 'deploy_container':
                entered.set()
                await release.wait()
            return await super().submit_command(command)
    client = BlockingClient()
    workflow, run = ready_run(repo, target, client)
    first = asyncio.create_task(workflow.advance(run['id']))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        with pytest.raises(HubError, match='advance_requires_reconciliation'):
            await workflow.advance(run['id'])
    finally:
        release.set()
        await first
    assert [c.action for c in client.submissions] == ACTIONS[:2]


@pytest.mark.asyncio
async def test_process_termination_lock_is_not_automatically_adopted(repo, target):
    workflow, run = ready_run(repo, target, FakeClient({'deploy_container': TimeoutError()}))
    await workflow.advance(run['id'])
    with repo._transaction() as db:
        db.execute('INSERT INTO hub_container_pilot_advances VALUES(?,?)', (run['id'], 'terminated-owner'))
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    client = FakeClient()
    recovered = ContainerPilotWorkflow(reopened, client, [target])
    with pytest.raises(HubError, match='advance_requires_reconciliation'):
        await recovered.advance(run['id'])
    await recovered.inspect(run['id'], ACTIONS[0])
    assert client.submissions == []
    assert len(client.inspections) == 1
    with repo._connection() as db:
        assert db.execute('SELECT owner FROM hub_container_pilot_advances WHERE run_id=?', (run['id'],)).fetchone()[0] == 'terminated-owner'


@pytest.mark.asyncio
async def test_typed_receipts_survive_reopen_with_exact_identity_and_evidence(repo, target):
    client = FakeClient()
    workflow, run = ready_run(repo, target, client)
    assert (await workflow.advance(run['id']))['status'] == 'succeeded'
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    next_client = FakeClient()
    recovered = ContainerPilotWorkflow(reopened, next_client, [target])
    with reopened._connection() as db:
        rows = db.execute('SELECT payload,state,response_json FROM hub_container_pilot_commands WHERE run_id=? ORDER BY action',
                          (run['id'],)).fetchall()
    assert len(rows) == 2
    for row in rows:
        command = ContainerPilotRequest.model_validate_json(row['payload'])
        receipt = ContainerPilotResponse.model_validate_json(row['response_json'])
        assert receipt == client.response(command)
        assert receipt.model_dump(include=set(ContainerPilotRequest.model_fields)) == command.model_dump()
        assert receipt.evidence.image_config_id == IMAGE
        assert receipt.evidence.container_id == 'e' * 64
        assert receipt.evidence.health == 'healthy'
        assert receipt.evidence.evidence_verified is True
        assert row['state'] == 'succeeded'
    await recovered.advance(run['id'])
    assert next_client.submissions == []


@pytest.mark.asyncio
async def test_unknown_outcome_has_no_fabricated_receipt(repo, target):
    workflow, run = ready_run(repo, target, FakeClient({'deploy_container': TimeoutError()}))
    assert (await workflow.advance(run['id']))['status'] == 'held'
    with repo._connection() as db:
        row = db.execute('SELECT state,response_json FROM hub_container_pilot_commands WHERE run_id=?',
                         (run['id'],)).fetchone()
    assert row['state'] == 'held'
    assert row['response_json'] is None


@pytest.mark.asyncio
async def test_held_typed_bad_image_retained_as_assertion_not_accepted_effect(repo, target):
    class WrongImageClient(FakeClient):
        def response(self, command):
            response = super().response(command)
            return response.model_copy(update={'evidence': response.evidence.model_copy(
                update={'image_config_id': ROLLBACK_IMAGE})})
    client = WrongImageClient()
    workflow, run = ready_run(repo, target, client)
    assert (await workflow.advance(run['id']))['status'] == 'held'
    with repo._connection() as db:
        row = db.execute('SELECT state,response_json FROM hub_container_pilot_commands WHERE run_id=?',
                         (run['id'],)).fetchone()
    assert row['state'] == 'held'
    assert ContainerPilotResponse.model_validate_json(row['response_json']) == client.response(client.submissions[0])


@pytest.mark.asyncio
async def test_legacy_four_column_unknown_row_migrates_without_resend_or_evidence(repo, target):
    task = reviewed_task(repo)
    run = repo.create_run('codex', task['id'], task['revision'], IMAGE, target.target_id,
                          execution_manifest_digest=MANIFEST)
    command = ContainerPilotRequest(schema_version='container_pilot_v1',
        command_id=str(uuid.uuid5(uuid.UUID(run['id']), 'container_pilot_v1:deploy_container')),
        run_id=run['id'], target_id=target.target_id, action='deploy_container',
        manifest_digest=MANIFEST, candidate_commit=COMMIT, policy_revision=1,
        fencing_generation=run['fencing_generation'])
    payload = json.dumps(command.model_dump(), sort_keys=True, separators=(',', ':'))
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_container_pilot_commands (run_id TEXT NOT NULL, action TEXT NOT NULL, '
                   'payload TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(run_id,action))')
        db.execute('INSERT INTO hub_container_pilot_commands VALUES(?,?,?,?)',
                   (run['id'], command.action, payload, 'unknown'))
    reopened = HubRepository(repo.path, execution_principal=PRINCIPAL)
    client = FakeClient()
    recovered = ContainerPilotWorkflow(reopened, client, [target])
    assert (await recovered.advance(run['id']))['status'] == 'held'
    assert client.submissions == []
    with reopened._connection() as db:
        row = db.execute('SELECT payload,state,response_json FROM hub_container_pilot_commands WHERE run_id=?',
                         (run['id'],)).fetchone()
    assert row['payload'] == payload
    assert row['state'] == 'unknown'
    assert row['response_json'] is None


def pilot_tables(repo):
    with repo._connection() as db:
        return db.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'hub_container_pilot_%' ORDER BY name").fetchall()


def test_digest_only_legacy_binding_rejected_before_tables_or_client_io(repo):
    client = FakeClient()
    legacy = ContainerPilotTarget(IMAGE, COMMIT, MANIFEST, ROLLBACK_IMAGE, 'f' * 40)
    before = pilot_tables(repo)
    with pytest.raises(HubError, match='container_pilot_manifest_unverified'):
        ContainerPilotWorkflow(repo, client, [legacy])
    assert pilot_tables(repo) == before == []
    assert client.submissions == [] and client.inspections == []
    assert repo.list_runs() == []


@pytest.mark.parametrize('tamper', ['unknown_field', 'unknown_field_rehashed', 'wrong_schema',
                                  'invalid_json', 'digest_mismatch', 'runtime_changed_without_digest'])
def test_tampered_full_manifest_rejected_before_tables_or_client_io(repo, target, tamper):
    data = json.loads(target.canonical_manifest)
    if tamper in {'unknown_field', 'unknown_field_rehashed'}:
        data['caller_authority'] = 'deploy-anything'
    elif tamper == 'wrong_schema':
        data['schema_version'] = 'unrecognized'
    elif tamper == 'runtime_changed_without_digest':
        prior = data['runtime']['resources']['pids_limit']
        data['runtime']['resources']['pids_limit'] = 48 if prior != 48 else 64
    canonical = '{' if tamper == 'invalid_json' else json.dumps(data)
    changes = {'canonical_manifest': canonical}
    if tamper == 'unknown_field_rehashed':
        changes['manifest_digest'] = 'sha256:' + hashlib.sha256(canonical.encode()).hexdigest()
    if tamper == 'digest_mismatch':
        changes['manifest_digest'] = 'sha256:' + '0' * 64
    changed = replace(target, **changes)
    client = FakeClient()
    before = pilot_tables(repo)
    with pytest.raises(HubError, match='container_pilot_manifest_'):
        ContainerPilotWorkflow(repo, client, [changed])
    assert pilot_tables(repo) == before == []
    assert client.submissions == [] and client.inspections == []


def test_constructor_pins_complete_canonical_manifest_before_any_run(repo, target):
    client = FakeClient()
    ContainerPilotWorkflow(repo, client, [target])
    with repo._connection() as db:
        rows = db.execute('SELECT manifest_digest,binding FROM hub_container_pilot_manifest_views').fetchall()
    assert len(rows) == 1 and rows[0]['manifest_digest'] == MANIFEST
    pinned = json.loads(rows[0]['binding'])
    assert pinned['canonical_manifest'] == target.canonical_manifest
    assert PilotManifest.from_bytes(pinned['canonical_manifest'].encode()).digest() == MANIFEST
    assert json.loads(pinned['canonical_manifest']) == json.loads(pilot_manifest().canonical_bytes())
    assert repo.list_runs() == [] and client.submissions == []


@pytest.mark.asyncio
async def test_changed_runtime_policy_has_new_digest_and_cannot_advance_old_run(repo, target):
    _, run = ready_run(repo, target)
    changed_data = json.loads(target.canonical_manifest)
    original_cpu = changed_data['runtime']['resources']['cpu_millicores']
    changed_data['runtime']['resources']['cpu_millicores'] = 500 if original_cpu != 500 else 1000
    changed_manifest = PilotManifest.from_bytes(json.dumps(changed_data).encode())
    changed_target = ContainerPilotTarget.from_manifest(changed_manifest)
    assert changed_target.manifest_digest != target.manifest_digest
    assert changed_target.artifact_digest == target.artifact_digest
    assert changed_target.candidate_commit == target.candidate_commit
    client = FakeClient()
    workflow = ContainerPilotWorkflow(repo, client, [changed_target])
    with pytest.raises(HubError, match='container_pilot_manifest_mismatch'):
        await workflow.advance(run['id'])
    assert client.submissions == [] and client.inspections == []
    assert repo.get_run(run['id'])['status'] == 'pending'
    assert repo.get_run(run['id'])['execution_manifest_digest'] == MANIFEST
    with repo._connection() as db:
        rows = db.execute('SELECT manifest_digest,binding FROM hub_container_pilot_manifest_views').fetchall()
    assert {row['manifest_digest'] for row in rows} == {MANIFEST, changed_target.manifest_digest}
    assert json.loads(next(row['binding'] for row in rows if row['manifest_digest'] == MANIFEST))['canonical_manifest'] == target.canonical_manifest
