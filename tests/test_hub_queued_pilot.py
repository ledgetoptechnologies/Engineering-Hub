"""Linux real-ledger queue/controller integration with simulated host evidence.

No Docker daemon or host acceptance is exercised by these tests.
"""
import asyncio
import json
import sys
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from tests.manual_session_fixtures import synthetic_workflow_reconciliation

from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.container_pilot_manifest import PilotManifest
from engineering_hub.dispatcher import StagingDispatcher
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget, WorkWorker
from tests.test_hub_container_pilot import (
    ACTIONS, COMMIT, IMAGE, PRINCIPAL, FakeClient, pilot_manifest, reviewed_task,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Real Linux PilotOwner fencing")
ACTION = "engineering-hub.container-pilot"
MACHINE = "isolated-staging"


class DistinctContainerClient(FakeClient):
    def response(self, command):
        response = super().response(command)
        if command.action in ACTIONS[2:]:
            return response.model_copy(update={"evidence": response.evidence.model_copy(
                update={"container_id": "f" * 64})})
        return response


@contextmanager
def queued_pilot_rig(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "hub.sqlite3"
    owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
    client = DistinctContainerClient()
    workflow = ContainerPilotWorkflow(repo, client,
        [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
    clock = [1000.0]
    queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project="engineering-hub")}, clock=lambda: clock[0])
    bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE, reconciliation=synthetic_workflow_reconciliation(workflow))
    result = SimpleNamespace(repo=repo, owner=owner, client=client, workflow=workflow,
                             queue=queue, bridge=bridge, clock=clock)
    try:
        yield result
    finally:
        result.owner.close()


@pytest.fixture
def rig(tmp_path):
    with queued_pilot_rig(tmp_path) as value:
        yield value


def submit(rig, key="one"):
    task = reviewed_task(rig.repo, key)
    job = rig.queue.submit(actor="codex", request_id=key, action_key=ACTION,
        environment="staging", target=WorkTarget(task["id"], COMMIT, IMAGE))
    return task, job


def machine_job(rig):
    with rig.repo._connection() as db:
        row = db.execute("SELECT job_id FROM hub_work_machines WHERE machine_id=?", (MACHINE,)).fetchone()
        return row["job_id"] if row else None


@pytest.mark.asyncio
async def test_success_has_one_run_verified_task_and_releases_machine(rig):
    task, job = submit(rig)
    result = await rig.bridge.run_once()
    assert result["id"] == job["id"] and result["state"] == "succeeded"
    runs = rig.repo.list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "succeeded"
    assert runs[0]["task_id"] == task["id"] and runs[0]["artifact_digest"] == IMAGE
    assert rig.repo.get_task(task["id"])["status"] == "verified"
    assert [c.action for c in rig.client.submissions] == ACTIONS[:2]
    assert machine_job(rig) is None
    assert await rig.bridge.run_once() is None
    assert len(rig.client.submissions) == 2


@pytest.mark.asyncio
async def test_healthy_rollback_reports_failed_work_without_claiming_promotion(rig):
    rig.client.outcomes["smoke_container"] = "failed"
    task, job = submit(rig)
    result = await rig.bridge.run_once()
    assert result["id"] == job["id"] and result["state"] == "failed"
    assert [c.action for c in rig.client.submissions] == ACTIONS
    assert rig.repo.list_runs()[0]["status"] == "rolled_back"
    current = rig.repo.get_task(task["id"])
    assert current["status"] == "verified" and current["repair_count"] == 1
    assert machine_job(rig) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["held", "running", TimeoutError()])
async def test_unknown_holds_machine_and_never_resubmits(rig, outcome):
    rig.client.outcomes["deploy_container"] = outcome
    _, job = submit(rig)
    result = await rig.bridge.run_once()
    assert result["id"] == job["id"] and result["state"] == "held"
    assert machine_job(rig) == job["id"]
    assert len(rig.client.submissions) == 1
    assert await rig.bridge.run_once() is None
    recovered = await rig.bridge.recover(job["id"])
    assert recovered["state"] == "held"
    assert len(rig.client.submissions) == 1
    assert len(rig.repo.list_runs()) == 1


@pytest.mark.asyncio
async def test_private_worker_only_and_fence_cannot_be_forged(rig):
    _, job = submit(rig)
    for agent in ("hermes", "codex", "claude"):
        assert rig.queue.claim(worker=WorkWorker(agent, str(uuid.uuid4()))) is None
    claimed = rig.queue.claim(worker=rig.bridge.worker, lease_seconds=600)
    assert claimed["id"] == job["id"]
    with pytest.raises(HubError):
        rig.queue.complete(job_id=job["id"], worker=rig.bridge.worker,
                           fence=claimed["fence"] + 1, outcome="succeeded", evidence_id=job["id"])
    with pytest.raises(HubError):
        rig.queue.complete(job_id=job["id"], worker=rig.bridge.worker,
                           fence=claimed["fence"], outcome="succeeded", evidence_id=job["id"])
    assert machine_job(rig) == job["id"] and rig.client.submissions == []


@pytest.mark.asyncio
async def test_changed_review_prevents_claim_and_any_host_submission(rig):
    task, _ = submit(rig)
    current = rig.repo.get_task(task["id"])
    rig.repo.set_candidate("codex", task["id"], current["revision"], "f" * 40)
    assert await rig.bridge.run_once() is None
    assert rig.client.submissions == [] and rig.repo.list_runs() == []


@pytest.mark.asyncio
async def test_direct_workflow_create_and_advance_cannot_bypass_queue(rig):
    task, _ = submit(rig)
    with pytest.raises(HubError):
        rig.workflow.create_run("codex", task["id"], task["revision"], IMAGE, "engineering-hub:staging")
    assert rig.repo.list_runs() == [] and rig.client.submissions == []
    await rig.bridge.run_once()
    run = rig.repo.list_runs()[0]
    with pytest.raises(HubError):
        await rig.workflow.advance(run["id"])
    with pytest.raises(HubError):
        await rig.workflow.inspect(run["id"], "deploy_container")
    with pytest.raises(HubError):
        await rig.bridge.recovery.recover(run["id"])
    assert len(rig.client.submissions) == 2
    assert rig.client.inspections == []


@pytest.mark.asyncio
async def test_closed_owner_cannot_start_work(rig):
    submit(rig)
    rig.owner.close()
    with pytest.raises((PilotOwnershipError, HubError)):
        await rig.bridge.run_once()
    assert rig.client.submissions == []


def mapping(rig, job_id):
    with rig.repo._connection() as db:
        return dict(db.execute("SELECT * FROM hub_queued_pilot_jobs WHERE job_id=?", (job_id,)).fetchone())


def reopen(rig):
    path = rig.repo.path
    rig.owner.close()
    rig.owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    rig.repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=rig.owner)
    rig.client = DistinctContainerClient()
    rig.workflow = ContainerPilotWorkflow(rig.repo, rig.client,
        [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=rig.owner)
    rig.queue = WorkQueue(rig.repo, {ACTION: WorkAction(MACHINE, project="engineering-hub")},
                          clock=lambda: rig.clock[0])
    rig.bridge = QueuedContainerPilot(rig.queue, rig.workflow, action_key=ACTION, machine_id=MACHINE, reconciliation=synthetic_workflow_reconciliation(rig.workflow))


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["before-create", "after-create", "after-map"])
async def test_interrupted_run_mapping_reopens_without_replacement_or_post(rig, monkeypatch, cut):
    _, job = submit(rig)
    create, save = rig.workflow.create_run, rig.bridge._map
    def crash_create(*args, **kwargs):
        if cut == "after-create":
            create(*args, **kwargs)
        raise RuntimeError("synthetic process-boundary failure")
    def crash_map(*args, **kwargs):
        save(*args, **kwargs)
        raise RuntimeError("synthetic mapping boundary failure")
    if cut == "after-map":
        monkeypatch.setattr(rig.bridge, "_map", crash_map)
    else:
        monkeypatch.setattr(rig.workflow, "create_run", crash_create)
    result = await rig.bridge.run_once()
    assert result["state"] == "held"
    original = mapping(rig, job["id"])
    assert original["request_id"] == str(uuid.uuid5(uuid.UUID(job["id"]),
        "queued-container-pilot:" + str(original["fence"])))
    runs_before = rig.repo.list_runs()
    assert len(runs_before) == (0 if cut == "before-create" else 1)
    assert rig.client.submissions == []
    reopen(rig)
    assert (await rig.bridge.recover(job["id"]))["state"] == "held"
    assert await rig.bridge.run_once() is None
    assert rig.client.submissions == [] and rig.client.inspections == []
    assert rig.repo.list_runs() == runs_before
    after = mapping(rig, job["id"])
    assert after["request_id"] == original["request_id"] and after["fence"] == original["fence"]
    assert after["run_id"] == (runs_before[0]["id"] if runs_before else None)
    assert machine_job(rig) == job["id"]


@pytest.mark.asyncio
async def test_cancel_after_terminal_run_recovers_original_proof_without_resend(rig, monkeypatch):
    _, job = submit(rig)
    complete = rig.queue.complete
    def interrupted_completion(**kwargs):
        if kwargs["outcome"] == "succeeded":
            raise asyncio.CancelledError()
        return complete(**kwargs)
    monkeypatch.setattr(rig.queue, "complete", interrupted_completion)
    with pytest.raises(asyncio.CancelledError):
        await rig.bridge.run_once()
    run_id = mapping(rig, job["id"])["run_id"]
    assert rig.repo.get_run(run_id)["status"] == "succeeded"
    assert len(rig.client.submissions) == 2 and machine_job(rig) == job["id"]
    rig.clock[0] += 601
    reopen(rig)
    recovered = await rig.bridge.recover(job["id"])
    assert recovered["state"] == "succeeded"
    assert mapping(rig, job["id"])["run_id"] == run_id
    assert rig.client.submissions == [] and machine_job(rig) is None
    assert len(rig.repo.list_runs()) == 1


@pytest.mark.asyncio
async def test_same_machine_excludes_second_job_through_unknown_and_restart(rig):
    rig.client.outcomes["deploy_container"] = "held"
    _, first = submit(rig, "first")
    _, second = submit(rig, "second")
    assert (await rig.bridge.run_once())["id"] == first["id"]
    assert await rig.bridge.run_once() is None
    assert rig.queue.get_job(second["id"])["state"] == "queued"
    reopen(rig)
    assert await rig.bridge.run_once() is None
    assert machine_job(rig) == first["id"] and rig.client.submissions == []


@pytest.mark.asyncio
async def test_direct_repository_idempotency_replay_is_denied_before_mutation(rig):
    task, job = submit(rig)
    await rig.bridge.run_once()
    saved = mapping(rig, job["id"])
    before = rig.repo.list_runs(), rig.repo.get_task(task["id"])
    with rig.repo._connection() as db:
        fence = list(db.execute("SELECT * FROM hub_target_fences"))
    with pytest.raises(HubError):
        rig.repo.create_run("codex", task["id"], task["revision"], IMAGE, "engineering-hub:staging",
                            execution_manifest_digest=saved["manifest_digest"], client_request_id=saved["request_id"])
    assert (rig.repo.list_runs(), rig.repo.get_task(task["id"])) == before
    with rig.repo._connection() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM hub_target_fences")] == [tuple(row) for row in fence]
    assert len(rig.client.submissions) == 2


@pytest.mark.asyncio
async def test_objects_created_before_enrollment_cannot_bypass_late_marker(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "hub.sqlite3"
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
        client = FakeClient()
        workflow = ContainerPilotWorkflow(repo, client,
            [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
        queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project="engineering-hub")})
        QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE, reconciliation=synthetic_workflow_reconciliation(workflow))
        task = reviewed_task(repo)
        with pytest.raises(HubError):
            repo.create_run("codex", task["id"], task["revision"], IMAGE, "engineering-hub:staging")
        with pytest.raises(HubError):
            StagingDispatcher(repo, workflow)
        assert repo.list_runs() == []
        assert repo.get_task(task["id"]) == task and client.submissions == []


def test_existing_legacy_dispatcher_refuses_bridge_enrollment(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "hub.sqlite3"
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
        client = FakeClient()
        workflow = ContainerPilotWorkflow(repo, client,
            [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
        dispatcher = StagingDispatcher(repo, workflow)
        queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project="engineering-hub")})
        before = dispatcher.cursor
        with pytest.raises(HubError):
            QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE, reconciliation=synthetic_workflow_reconciliation(workflow))
        assert dispatcher.cursor == before and client.submissions == []
        assert getattr(repo, "_queued_pilot", None) is None


@pytest.mark.asyncio
async def test_original_target_authorization_revocation_between_phases_stops_post(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "hub.sqlite3"
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
        allowed = [True]
        class RevokingClient(FakeClient):
            async def submit_command(self, command):
                response = await super().submit_command(command)
                allowed[0] = False
                return response
        client = RevokingClient()
        workflow = ContainerPilotWorkflow(repo, client,
            [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
        queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project="engineering-hub")},
                          target_verifier=lambda *args: allowed[0])
        bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE, reconciliation=synthetic_workflow_reconciliation(workflow))
        task = reviewed_task(repo)
        job = queue.submit(actor="codex", request_id="revoked", action_key=ACTION,
            environment="staging", target=WorkTarget(task["id"], COMMIT, IMAGE))
        assert (await bridge.run_once())["state"] == "held"
        assert [c.action for c in client.submissions] == ["deploy_container"]
        assert queue.get_job(job["id"])["state"] == "held"
        assert await bridge.run_once() is None
        assert len(client.submissions) == 1


@pytest.mark.asyncio
async def test_action_registry_change_between_phases_holds_before_second_post(rig):
    original = rig.client.submit_command
    async def change_registry(command):
        response = await original(command)
        rig.queue.actions[ACTION] = WorkAction("different-machine", project="engineering-hub")
        return response
    rig.client.submit_command = change_registry
    _, job = submit(rig)
    assert (await rig.bridge.run_once())["state"] == "held"
    assert [c.action for c in rig.client.submissions] == ["deploy_container"]
    assert machine_job(rig) == job["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["config", "smoke-container"])
async def test_mismatched_typed_host_evidence_never_releases_capacity(rig, mismatch):
    original = rig.client.response
    def altered(command):
        response = original(command)
        if mismatch == "config":
            changes = {"image_config_id": "sha256:" + "9" * 64}
        elif command.action == "smoke_container":
            changes = {"container_id": "9" * 64}
        else:
            return response
        return response.model_copy(update={"evidence": response.evidence.model_copy(update=changes)})
    rig.client.response = altered
    _, job = submit(rig)
    result = await rig.bridge.run_once()
    assert result["state"] == "held"
    assert machine_job(rig) == job["id"]
    assert rig.repo.get_task(job["target"]["task_id"])["status"] == "verified"


@pytest.mark.parametrize("changed", ["machine", "manifest"])
def test_restart_cannot_rebind_enrolled_machine_or_manifest(rig, changed):
    path = rig.repo.path
    rig.owner.close()
    rig.owner = PilotOwner(path, allow_ephemeral_test_storage=True)
    repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=rig.owner)
    data = pilot_manifest().model_dump(mode="json", by_alias=True)
    if changed == "manifest":
        data["candidate"]["source_commit"] = "9" * 40
    manifest = PilotManifest.from_bytes(json.dumps(data).encode())
    machine = "new-machine-alias" if changed == "machine" else MACHINE
    client = DistinctContainerClient()
    with pytest.raises(HubError):
        workflow = ContainerPilotWorkflow(repo, client, [ContainerPilotTarget.from_manifest(manifest)],
                                           pilot_owner=rig.owner)
        queue = WorkQueue(repo, {ACTION: WorkAction(machine, project="engineering-hub")})
        QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=machine, reconciliation=synthetic_workflow_reconciliation(workflow))
    assert client.submissions == []


def test_preexisting_second_repository_cannot_borrow_current_owner_to_bypass_marker(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "hub.sqlite3"
    with PilotOwner(path, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
        other = HubRepository(path, execution_principal=PRINCIPAL, pilot_owner=owner)
        client = FakeClient()
        workflow = ContainerPilotWorkflow(repo, client,
            [ContainerPilotTarget.from_manifest(pilot_manifest())], pilot_owner=owner)
        queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project="engineering-hub")})
        QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE, reconciliation=synthetic_workflow_reconciliation(workflow))
        task = reviewed_task(repo)
        with pytest.raises(HubError):
            other.create_run("codex", task["id"], task["revision"], IMAGE, "engineering-hub:staging")
        assert repo.list_runs() == [] and repo.get_task(task["id"]) == task
        assert client.submissions == []


def test_missing_inprocess_bridge_never_treats_durable_marker_as_authority(rig):
    task, _ = submit(rig)
    del rig.repo._queued_pilot
    try:
        with pytest.raises(HubError):
            rig.repo.create_run("codex", task["id"], task["revision"], IMAGE, "engineering-hub:staging")
        assert rig.repo.list_runs() == [] and rig.client.submissions == []
    finally:
        rig.repo._queued_pilot = rig.bridge
