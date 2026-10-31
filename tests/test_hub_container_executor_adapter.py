"""Explicit fake boundaries exercise adapter state; these are not Docker evidence."""
import hashlib
import json
import threading
import time
import uuid
from dataclasses import replace

import pytest

import hub_container_executor.adapter as adapter_module
from engineering_hub.container_pilot_client import ContainerPilotRequest
from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.adapter import HostAdapter, KnownTerminalFailure
from hub_container_executor.backend import (
    ActualImageSelection,
    BaselineIdentity,
    ContainerObservation,
    ImageDefaults,
    OperationObservation,
    ProbeObservation,
    GateHealthObservation,
    RunnerObservation,
    StateGeneration,
)
from hub_container_executor.build_record import verify_image_record
from hub_container_executor.ledger import LedgerClosed, OwnerBusy
from tests.test_hub_container_executor_build_record import FakeStore, FakeTrust, make_record
from tests.test_hub_container_pilot_manifest import make_manifest_data


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class FakeSecrets:
    def __init__(self):
        self.values = {"CODEX_TOKEN": "test-codex-value", "HERMES_TOKEN": "test-hermes-value",
                       "CLAUDE_TOKEN": "test-claude-value"}

    def resolve_test_secret(self, slot_id, version):
        prefix = slot_id.removeprefix("test-").upper().replace("-", "_")
        return self.values[prefix + "_TOKEN"]


class FakeDriverIdentityVerifier:
    def __init__(self, implementation_commit):
        self.commit = implementation_commit

    def implementation_commit(self):
        if isinstance(self.commit, BaseException):
            raise self.commit
        return self.commit


class FakeBaselineProvider:
    def __init__(self, baseline):
        self.baseline = baseline

    def enrolled_baseline(self, manifest):
        return self.baseline

    def verify_baseline(self, baseline, manifest):
        return baseline is self.baseline


class FakeStateProvisioner:
    def __init__(self, baseline):
        self.baseline = baseline
        self.calls = []
        self.deadlines = []
        self.delay_seconds = 0

    def _new(self, operation_id, generation_id, deadline_monotonic):
        self.calls.append(operation_id)
        self.deadlines.append((deadline_monotonic, time.monotonic()))
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return StateGeneration(generation_id=generation_id,
                               previous_generation_id=self.baseline.state_generation_id,
                               source_backup_digest=self.baseline.backup_digest,
                               generation_digest="sha256:" + "f" * 64,
                               size_bytes=4096, baseline_unchanged=True)

    def prepare_candidate(self, manifest, run_id, baseline, operation_id, *, deadline_monotonic=None):
        return self._new(operation_id, "candidate-g1", deadline_monotonic)

    def prepare_rollback(self, manifest, run_id, baseline, operation_id, *, deadline_monotonic=None):
        return self._new(operation_id, "rollback-g1", deadline_monotonic)


class FakeBackend:
    def __init__(self, manifest, secret_resolver, fail_create_after_late_effect=False,
                 *, bad_runner=False, alter_profile=False, terminal_start=False):
        self.manifest = manifest
        self.secret_resolver = secret_resolver
        self.fail_create_after_late_effect = fail_create_after_late_effect
        self.bad_runner = bad_runner
        self.alter_profile = alter_profile
        self.terminal_start = terminal_start
        self.events = []
        self.containers = {}
        self.probe_health = ["unhealthy", "healthy"]
        self.terminal_operations = {}

    def inspect_runner(self, timeout_ms):
        self.events.append("inspect_runner")
        binding = self.manifest.runner_binding
        identity = binding.daemon_identity + "-foreign" if self.bad_runner else binding.daemon_identity
        return RunnerObservation(binding.runner_id, identity, binding.platform)

    def inspect_image(self, image, timeout_ms):
        self.events.append("inspect_image")
        config = image.image_config.get("config") or {}
        return ImageDefaults(image.image_config_id, image.platform,
                             tuple(config.get("Entrypoint") or ()),
                             tuple(config.get("Cmd") or ()), tuple(config.get("Env") or ()))

    def inspect_container(self, container_id, timeout_ms):
        self.events.append("inspect_container")
        return self.containers.get(container_id)

    def create_container(self, plan, operation_id, timeout_ms):
        self.events.append("create")
        container_id = "c" * 64 if plan.image.role == "candidate" else "d" * 64
        observation = ContainerObservation(
            container_id=container_id,
            selected_image=ActualImageSelection(plan.image.runnable_reference,
                                                plan.image.platform_manifest_digest,
                                                plan.image.image_config_id,
                                                plan.image.platform),
            profile=(replace(plan.profile, network_mode="bridge")
                     if self.alter_profile else plan.profile),
            state_generation_id=plan.profile.state_generation_id,
            status="created",
        )
        self.containers[container_id] = observation
        if self.fail_create_after_late_effect:
            self.fail_create_after_late_effect = False
            raise TimeoutError("injected fake late operation")
        return container_id

    def start_container(self, container_id, operation_id, timeout_ms):
        self.events.append("start")
        if self.terminal_start:
            self.terminal_start = False
            raise KnownTerminalFailure("fake terminal result")
        before = self.containers[container_id]
        self.containers[container_id] = ContainerObservation(
            before.container_id, before.selected_image, before.profile,
            before.state_generation_id, "running",
        )

    def stop_container(self, container_id, operation_id, timeout_ms):
        self.events.append("stop")

    def run_compiled_probes(self, container_id, probes, operation_id, timeout_ms):
        self.events.append("probe")
        container = self.containers[container_id]
        raw = json.dumps([probe.model_dump(mode="json") for probe in probes],
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return ProbeObservation(container_id, container.selected_image.image_config_id,
                                digest(raw), self.probe_health.pop(0), True)

    def run_gate_health(self, container_id, probes, operation_id, timeout_ms, total_timeout_ms):
        now = int(time.time() * 1000)
        return GateHealthObservation(self.run_compiled_probes(container_id, probes, operation_id, timeout_ms),
                                     42, now, now, 0)

    def inspect_operation(self, operation_id, timeout_ms):
        self.events.append("inspect_operation")
        return self.terminal_operations.get(
            operation_id, OperationObservation(operation_id, "pending", False,
                                               "c" * 64 if self.fail_create_after_late_effect is False else None)
        )


def make_fixture(tmp_path, *, include_verifier=True, late_create=False, bad_runner=False,
                 alter_profile=False, terminal_start=False, driver_identity_commit="manifest",
                 include_driver_identity=True, command_budget_ms=None, state_delay_seconds=0,
                 startup_grace_ms=0):
    candidate_manifest, candidate_raw, candidate_blobs = make_record("candidate")
    rollback_manifest, rollback_raw, rollback_blobs = make_record("rollback")
    manifest_data = make_manifest_data()
    candidate_record = json.loads(candidate_raw)
    rollback_record = json.loads(rollback_raw)
    manifest_data["candidate"]["image_config_id"] = candidate_record["selection"]["image_config_id"]
    manifest_data["candidate"]["provenance"]["attestation_digest"] = digest(candidate_raw)
    manifest_data["rollback"]["image_config_id"] = rollback_record["selection"]["image_config_id"]
    manifest_data["rollback"]["provenance"]["attestation_digest"] = digest(rollback_raw)
    if command_budget_ms is not None:
        for phase in ("create_ms", "start_ms", "stop_ms", "inspect_ms"):
            manifest_data["timeouts"][phase] = min(100, command_budget_ms)
        manifest_data["timeouts"]["total_command_ms"] = command_budget_ms
        probe_budget = max(100, command_budget_ms // 3)
        for role_probes in manifest_data["probes"].values():
            for probe in role_probes:
                probe["timeout_ms"] = probe_budget
    if startup_grace_ms:
        for role_probes in manifest_data['probes'].values():
            role_probes[0]['startup_grace_ms'] = startup_grace_ms
    manifest = PilotManifest.from_bytes(canonical(manifest_data))

    records = {digest(candidate_raw): candidate_raw, digest(rollback_raw): rollback_raw}
    blobs = dict(candidate_blobs)
    blobs.update(rollback_blobs)
    record_store = FakeStore(records)
    metadata_store = FakeStore(blobs)
    baseline = BaselineIdentity(
        baseline_id=manifest.state_policy.baseline_id,
        backup_digest=manifest.state_policy.baseline_backup_digest,
        container_id="e" * 64,
        image_config_id=manifest.rollback.image_config_id,
        state_generation_id="baseline-g1",
        healthy_at_enrollment=True,
        proof_id="test-baseline-proof",
    )
    secrets = FakeSecrets()
    state = FakeStateProvisioner(baseline)
    state.delay_seconds = state_delay_seconds
    backend = FakeBackend(manifest, secrets, late_create, bad_runner=bad_runner,
                          alter_profile=alter_profile, terminal_start=terminal_start)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    identity_commit = (manifest.driver.implementation_commit if driver_identity_commit == "manifest"
                       else driver_identity_commit)
    driver_identity_verifier = (
        FakeDriverIdentityVerifier(identity_commit) if identity_commit is not None else None
    )
    identity_kwargs = ({"driver_identity_verifier": driver_identity_verifier}
                       if include_driver_identity else {})
    adapter = HostAdapter(
        root / "ledger.sqlite3", manifest,
        record_store=record_store, metadata_store=metadata_store,
        provenance_verifier=FakeTrust() if include_verifier else None,
        backend=backend, baseline_provider=FakeBaselineProvider(baseline),
        state_provisioner=state, secret_resolver=secrets,
        **identity_kwargs,
    )
    # Baseline image/profile is constructed from the authenticated rollback record.
    rollback_image = verify_image_record(manifest, "rollback", record_store, metadata_store, FakeTrust())
    defaults = adapter._defaults_from_record(rollback_image)
    secret_values = {name: secrets.values[name] for name in secrets.values}
    profile = adapter._profile(rollback_image, defaults, baseline.state_generation_id, secret_values)
    selection = ActualImageSelection(rollback_image.runnable_reference,
                                     rollback_image.platform_manifest_digest,
                                     rollback_image.image_config_id,
                                     rollback_image.platform)
    backend.containers[baseline.container_id] = ContainerObservation(
        baseline.container_id, selection, profile, baseline.state_generation_id, "running")
    return adapter, backend, state, manifest


def command(manifest, action, *, run_id=None, fence=1):
    return ContainerPilotRequest(
        schema_version="container_pilot_v1", command_id=str(uuid.uuid4()),
        run_id=run_id or str(uuid.uuid4()), target_id="engineering-hub:staging",
        action=action, manifest_digest=manifest.digest(),
        candidate_commit=manifest.candidate.source_commit,
        policy_revision=manifest.policy_revision, fencing_generation=fence,
    )


def test_default_missing_trusted_verifier_fails_before_any_effect(tmp_path):
    adapter, backend, state, manifest = make_fixture(tmp_path, include_verifier=False)
    response = adapter.submit(command(manifest, "deploy_container"))
    assert response.state == "held" and response.reason == "recovery_required"
    assert not any(event in {"create", "start", "probe"} for event in backend.events)
    assert state.calls == []
    adapter.close()


def test_gated_deploy_holds_before_effect_if_rollback_image_is_ungated(tmp_path, monkeypatch):
    from hub_container_executor.application_gate_enrollment import STATIC_ENTRYPOINT
    adapter, backend, state, manifest = make_fixture(tmp_path)
    adapter.gate_socket_directory = '/synthetic/gate'
    inspect_defaults = adapter._image_defaults
    def defaults(image, deadline):
        value = inspect_defaults(image, deadline)
        return replace(value, entrypoint=STATIC_ENTRYPOINT, command=()) if image.role == 'candidate' else value
    monkeypatch.setattr(adapter, '_image_defaults', defaults)
    try:
        response = adapter.submit(command(manifest, 'deploy_container'))
        assert response.state == 'held' and response.reason == 'image_unavailable'
        assert state.calls == []
        assert not any(event in {'create', 'start', 'stop', 'probe'} for event in backend.events)
    finally:
        adapter.close()


@pytest.mark.parametrize('health', ['healthy', 'unhealthy'])
def test_gated_start_cannot_finish_success_without_confirmed_gate(tmp_path, monkeypatch, health):
    from hub_container_executor.application_gate_enrollment import STATIC_ENTRYPOINT
    adapter, backend, state, manifest = make_fixture(tmp_path)
    backend.probe_health = [health]
    adapter.gate_socket_directory = '/synthetic/gate'
    inspect_defaults = adapter._image_defaults
    monkeypatch.setattr(adapter, '_image_defaults', lambda image, deadline:
                        replace(inspect_defaults(image, deadline), entrypoint=STATIC_ENTRYPOINT, command=()))
    monkeypatch.setattr(adapter, '_verify_baseline_container', lambda *_: None)
    try:
        request = command(manifest, 'deploy_container')
        response = adapter.submit(request)
        assert response.state == 'held' and response.reason == 'inspect_required'
        assert state.calls and 'start' in backend.events
        operations = adapter.ledger.operation_rows(request.command_id)
        health_row = next(row for row in operations if row['kind'] == 'gate_health')
        assert health_row['status'] == 'completed' and health_row['receipt_authentic'] is True
        assert health_row['receipt']['health'] == health
        with adapter.ledger._connection() as db:
            row = db.execute('SELECT state FROM gate_required WHERE command_id=?', (request.command_id,)).fetchone()
            assert row is not None and row['state'] == 'pending'
    finally:
        adapter.close()


def test_gated_health_timeout_keeps_original_intent_and_run_held(tmp_path, monkeypatch):
    from hub_container_executor.application_gate_enrollment import STATIC_ENTRYPOINT
    adapter, backend, _, manifest = make_fixture(tmp_path)
    adapter.gate_socket_directory = '/synthetic/gate'
    inspect_defaults = adapter._image_defaults
    monkeypatch.setattr(adapter, '_image_defaults', lambda image, deadline:
                        replace(inspect_defaults(image, deadline), entrypoint=STATIC_ENTRYPOINT, command=()))
    monkeypatch.setattr(adapter, '_verify_baseline_container', lambda *_: None)
    def timeout(*_): raise TimeoutError('synthetic uncertain probe')
    monkeypatch.setattr(backend, 'run_compiled_probes', timeout)
    try:
        request = command(manifest, 'deploy_container')
        result = adapter.submit(request)
        assert result.state == 'held' and result.reason == 'inspect_required'
        row = next(row for row in adapter.ledger.operation_rows(request.command_id) if row['kind'] == 'gate_health')
        assert row['status'] == 'intent'
        assert adapter.ledger.target_snapshot(request.target_id)['state'] == 'held'
    finally:
        adapter.close()


def test_fake_engine_candidate_failure_requires_separate_rollback_smoke(tmp_path):
    adapter, backend, state, manifest = make_fixture(tmp_path)
    run_id = str(uuid.uuid4())

    deployed = adapter.submit(command(manifest, "deploy_container", run_id=run_id))
    assert deployed.state == "succeeded" and deployed.evidence.health == "unknown"
    assert deployed.evidence.evidence_verified is True
    assert backend.events.count("create") == 1 and backend.events.count("start") == 1
    candidate = backend.containers["c" * 64]
    profile = candidate.profile
    assert profile.uid == 10001 and profile.gid == 10001
    assert profile.supplementary_groups == ()
    assert profile.cap_drop == ("ALL",) and profile.read_only_root and profile.no_new_privileges
    assert profile.cgroupns_mode == manifest.runtime.cgroupns_mode == "private"
    assert profile.network_mode == "none" and profile.published_ports == ()
    assert profile.tmpfs == (("/tmp", 64 * 1024**2, 0o1777, True, True),)
    assert profile.mounts == (("state_generation", "/var/lib/hub", "rw", "candidate-g1"),
                              ("tmpfs", "/tmp", "rw", "size=67108864",
                               "mode=1777", "noexec", "nosuid"))
    assert dict(profile.environment)["HUB_DB"] == "/var/lib/hub/hub.sqlite3"
    assert dict(profile.environment)["CODEX_TOKEN"] == "test-codex-value"
    target = adapter.ledger.target_snapshot()
    assert target["generation_id"] == "candidate-g1"
    assert "test-codex-value" not in json.dumps(adapter.ledger.operation_rows(deployed.command_id))

    failed_smoke = adapter.submit(command(manifest, "smoke_container", run_id=run_id))
    assert failed_smoke.state == "failed" and failed_smoke.reason == "health_failed"
    assert failed_smoke.evidence.health == "unhealthy"

    rolled_back = adapter.submit(command(manifest, "rollback_container", run_id=run_id))
    assert rolled_back.state == "succeeded"
    assert rolled_back.evidence.image_config_id == manifest.rollback.image_config_id
    assert adapter.ledger.target_snapshot()["generation_id"] == "rollback-g1"
    rollback_smoke = adapter.submit(command(manifest, "smoke_container_rollback", run_id=run_id))
    assert rollback_smoke.state == "succeeded" and rollback_smoke.evidence.health == "healthy"
    assert rollback_smoke.evidence.container_id == "d" * 64
    assert backend.events.count("probe") == 2
    assert len(state.calls) == 2
    adapter.close()


def test_lost_create_response_remains_held_and_reconcile_is_inspect_only(tmp_path):
    adapter, backend, state, manifest = make_fixture(tmp_path, late_create=True)
    request = command(manifest, "deploy_container")
    response = adapter.submit(request)
    assert response.state == "held" and response.reason == "inspect_required"
    effect_count = backend.events.count("create")
    assert effect_count == 1
    reconciled = adapter.reconcile(request.command_id)
    assert reconciled.state == "held"
    assert backend.events.count("create") == effect_count
    assert backend.events.count("start") == 0
    assert backend.events.count("probe") == 0
    assert backend.events.count("inspect_operation") == 1
    duplicate = adapter.submit(request)
    assert duplicate.state == "held" and duplicate.replayed is True
    fresh_phase_id = command(manifest, "deploy_container", run_id=request.run_id, fence=1)
    assert adapter.submit(fresh_phase_id).reason == "recovery_required"
    adapter.close()


def test_pause_stops_at_state_effect_boundary_and_profile_mismatch_never_starts(tmp_path):
    adapter, backend, state, manifest = make_fixture(tmp_path)
    adapter.set_paused(True)
    paused = adapter.submit(command(manifest, "deploy_container"))
    assert paused.state == "held" and paused.reason == "paused"
    assert backend.events.count("create") == 0 and state.calls == []
    adapter.close()

    second_root = tmp_path / "other"
    second_root.mkdir(mode=0o700)
    adapter, backend, state, manifest = make_fixture(second_root, alter_profile=True)
    mismatch = adapter.submit(command(manifest, "deploy_container"))
    assert mismatch.state == "held" and mismatch.reason == "evidence_mismatch"
    assert backend.events.count("create") == 1
    assert backend.events.count("start") == 0
    adapter.close()


def test_probe_budgets_share_remaining_command_deadline(tmp_path, monkeypatch):
    adapter, _backend, _state, manifest = make_fixture(tmp_path)
    monkeypatch.setattr(adapter_module.time, "monotonic", lambda: 100.0)
    timeouts = adapter._probe_timeouts(manifest.probes.candidate, deadline=102.5)
    assert timeouts == (1000, 1000, 500)
    assert sum(timeouts) <= 2500
    adapter.close()


def test_state_copy_receives_shared_monotonic_command_deadline(tmp_path):
    adapter, _backend, state, manifest = make_fixture(tmp_path, state_delay_seconds=0.02)
    response = adapter.submit(command(manifest, "deploy_container"))
    assert response.state == "succeeded"
    deadline, entered_at = state.deadlines[0]
    assert isinstance(deadline, float)
    assert 0 < deadline - entered_at < manifest.timeouts.total_command_ms / 1000
    adapter.close()


def test_state_copy_overrun_stays_held_and_does_not_start_next_effect(tmp_path):
    adapter, backend, state, manifest = make_fixture(
        tmp_path, command_budget_ms=300, state_delay_seconds=0.4
    )
    response = adapter.submit(command(manifest, "deploy_container"))
    assert response.state == "held" and response.reason == "inspect_required"
    assert len(state.calls) == 1
    assert backend.events.count("create") == 0
    operation = adapter.ledger.operation_rows(response.command_id)
    assert operation[0]["kind"] == "state_copy" and operation[0]["status"] == "intent"
    adapter.close()


def test_foreign_runner_is_denied_before_state_provisioning_or_daemon_effect(tmp_path):
    adapter, backend, state, manifest = make_fixture(tmp_path, bad_runner=True)
    response = adapter.submit(command(manifest, "deploy_container"))
    assert response.state == "held" and response.reason == "daemon_unavailable"
    assert state.calls == []
    assert backend.events.count("create") == 0
    adapter.close()


@pytest.mark.parametrize("identity_commit", [None, "d" * 40, RuntimeError("unavailable")],
                         ids=["missing", "wrong", "unavailable"])
def test_unverified_runtime_driver_identity_holds_before_any_effect(tmp_path, identity_commit):
    adapter, backend, state, manifest = make_fixture(
        tmp_path, driver_identity_commit=identity_commit
    )
    response = adapter.submit(command(manifest, "deploy_container"))
    assert response.state == "held" and response.reason == "daemon_unavailable"
    assert state.calls == []
    assert "inspect_runner" not in backend.events
    assert backend.events.count("create") == 0
    adapter.close()


def test_trusted_runtime_driver_identity_matching_manifest_allows_verified_flow(tmp_path):
    adapter, backend, _state, manifest = make_fixture(tmp_path)
    response = adapter.submit(command(manifest, "deploy_container"))
    assert response.state == "succeeded"
    assert backend.events.count("create") == 1
    adapter.close()


def test_runtime_driver_identity_provider_is_unavailable_by_default(tmp_path):
    adapter, backend, state, manifest = make_fixture(tmp_path, include_driver_identity=False)
    response = adapter.submit(command(manifest, "deploy_container"))
    assert response.state == "held" and response.reason == "daemon_unavailable"
    assert state.calls == []
    assert "inspect_runner" not in backend.events
    adapter.close()


def test_only_typed_terminal_effect_failure_can_be_failed_and_rollback_can_follow(tmp_path):
    adapter, backend, _state, manifest = make_fixture(tmp_path, terminal_start=True)
    run_id = str(uuid.uuid4())
    failed = adapter.submit(command(manifest, "deploy_container", run_id=run_id))
    assert failed.state == "failed" and failed.reason == "effect_failed"
    operation_rows = adapter.ledger.operation_rows(failed.command_id)
    assert any(row["kind"] == "start_candidate" and row["status"] == "terminal_failed"
               for row in operation_rows)
    rolled_back = adapter.submit(command(manifest, "rollback_container", run_id=run_id))
    assert rolled_back.state == "succeeded"
    adapter.close()


def test_close_waits_for_inflight_effect_and_keeps_owner_exclusive(tmp_path):
    adapter, backend, _state, manifest = make_fixture(tmp_path)
    request = command(manifest, "deploy_container")
    entered = threading.Event()
    release = threading.Event()
    original_create = backend.create_container

    def blocked_create(plan, operation_id, timeout_ms):
        result = original_create(plan, operation_id, timeout_ms)
        entered.set()
        assert release.wait(5)
        return result

    backend.create_container = blocked_create
    submit_result = []
    submit_errors = []

    def submit():
        try:
            submit_result.append(adapter.submit(request))
        except BaseException as exc:
            submit_errors.append(exc)

    worker = threading.Thread(target=submit)
    worker.start()
    assert entered.wait(5)
    close_done = threading.Event()
    closer = threading.Thread(target=lambda: (adapter.close(), close_done.set()))
    closer.start()
    writer_done = threading.Event()
    writer_errors = []

    def waiting_writer():
        try:
            adapter.set_paused(True)
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_done.set()

    try:
        assert not close_done.wait(0.05), "close released ownership during the callback"
        close_deadline = time.monotonic() + 2
        while not adapter._closing and time.monotonic() < close_deadline:
            time.sleep(0.001)
        assert adapter._closing
        writer = threading.Thread(target=waiting_writer)
        writer.start()
        assert writer_done.wait(2), "writer waited behind shutdown instead of being rejected"
        writer.join(2)
        assert len(writer_errors) == 1 and isinstance(writer_errors[0], LedgerClosed)
        assert adapter.ledger.is_paused() is False
        try:
            replacement = HostAdapter(adapter.ledger.path, manifest)
        except OwnerBusy:
            pass
        else:
            replacement.close()
            raise AssertionError("replacement owner opened during an in-flight effect")
    finally:
        release.set()
        worker.join(10)
        closer.join(10)
    assert not worker.is_alive() and not closer.is_alive()
    assert not submit_errors
    assert submit_result[0].state == "succeeded"
    assert close_done.is_set()


def test_reentrant_close_from_effect_callback_is_deferred_until_result_commit(tmp_path):
    adapter, backend, _state, manifest = make_fixture(tmp_path)
    request = command(manifest, "deploy_container")
    original_create = backend.create_container

    def close_inside_callback(plan, operation_id, timeout_ms):
        result = original_create(plan, operation_id, timeout_ms)
        adapter.close()
        return result

    backend.create_container = close_inside_callback
    response = adapter.submit(request)
    assert response.state == "succeeded"
    replacement = HostAdapter(adapter.ledger.path, manifest)
    assert replacement.get(request.command_id).state == "succeeded"
    replacement.close()


def test_create_context_tamper_keeps_reconcile_held_without_inspection(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path, late_create=True)
    request = command(manifest, "deploy_container")
    assert adapter.submit(request).state == "held"
    operation = next(row for row in adapter.ledger.operation_rows(request.command_id)
                     if row["kind"] == "create_candidate")
    context = adapter.ledger.create_context(operation["operation_id"])
    assert context["manifest_digest"] == manifest.digest()
    assert context["generation_id"] == "candidate-g1"
    assert "test-codex-value" not in json.dumps(context)
    with adapter.ledger._connection() as db:
        db.execute("UPDATE create_contexts SET context_mac=? WHERE operation_id=?",
                   ("0" * 64, operation["operation_id"]))
    assert adapter.reconcile(request.command_id).state == "held"
    assert "inspect_operation" not in backend.events
    assert backend.events.count("create") == 1
    adapter.close()


def reopen_adapter(adapter):
    path, manifest = adapter.ledger.path, adapter.manifest
    kwargs = dict(record_store=adapter.record_store, metadata_store=adapter.metadata_store,
                  provenance_verifier=adapter.provenance_verifier, backend=adapter.backend,
                  baseline_provider=adapter.baseline_provider,
                  state_provisioner=adapter.state_provisioner,
                  secret_resolver=adapter.secret_resolver,
                  driver_identity_verifier=adapter.driver_identity_verifier)
    adapter.close()
    return HostAdapter(path, manifest, **kwargs)


def test_lost_create_matching_context_after_restart_remains_inspect_only(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path, late_create=True)
    request = command(manifest, "deploy_container")
    assert adapter.submit(request).state == "held"
    adapter = reopen_adapter(adapter)
    backend.events.clear()
    assert adapter.reconcile(request.command_id).state == "held"
    assert backend.events == ["inspect_operation", "inspect_container"]
    assert adapter.submit(request).replayed is True
    assert backend.events == ["inspect_operation", "inspect_container"]
    adapter.close()


@pytest.mark.parametrize("drift", ["secret", "record", "context_missing"])
def test_reconcile_requires_original_context_and_inputs_before_backend_access(tmp_path, drift):
    adapter, backend, _, manifest = make_fixture(tmp_path, late_create=True)
    request = command(manifest, "deploy_container")
    assert adapter.submit(request).state == "held"
    operation = next(row for row in adapter.ledger.operation_rows(request.command_id)
                     if row["kind"] == "create_candidate")
    adapter = reopen_adapter(adapter)
    if drift == "secret":
        adapter.secret_resolver.values["CODEX_TOKEN"] = "test-changed-value"
    elif drift == "record":
        adapter.record_store.values.clear()
    else:
        with adapter.ledger._connection() as db:
            db.execute("DELETE FROM create_contexts WHERE operation_id=?", (operation["operation_id"],))
    backend.events.clear()
    assert adapter.reconcile(request.command_id).state == "held"
    assert backend.events == []
    adapter.close()


def test_legacy_terminal_failure_remains_held_without_backend_access(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path, late_create=True)
    request = command(manifest, "deploy_container")
    assert adapter.submit(request).state == "held"
    operation = next(row for row in adapter.ledger.operation_rows(request.command_id)
                     if row["kind"] == "create_candidate")
    adapter.ledger.record_terminal_failure(operation["operation_id"])
    with adapter.ledger._connection() as db:
        db.execute("DELETE FROM create_contexts WHERE operation_id=?", (operation["operation_id"],))
    adapter = reopen_adapter(adapter)
    backend.terminal_operations[operation["operation_id"]] = OperationObservation(
        operation["operation_id"], "terminal_failed", True, None)
    backend.events.clear()
    assert adapter.reconcile(request.command_id).state == "held"
    assert backend.events == []
    adapter.close()


def test_failure_after_create_intent_before_effect_stays_held_on_restart(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path)
    request = command(manifest, "deploy_container")
    def refuse_before_effect(plan, operation_id, timeout_ms):
        assert adapter.ledger.create_context(operation_id)["generation_id"] == "candidate-g1"
        raise TimeoutError("crash boundary before daemon effect")
    backend.create_container = refuse_before_effect
    assert adapter.submit(request).state == "held"
    operation = next(row for row in adapter.ledger.operation_rows(request.command_id)
                     if row["kind"] == "create_candidate")
    assert operation["status"] == "intent"
    adapter = reopen_adapter(adapter)
    backend.events.clear()
    assert adapter.reconcile(request.command_id).state == "held"
    assert "create" not in backend.events and "start" not in backend.events
    adapter.close()


def test_create_profile_commitment_is_keyed_and_stable_after_reopen(tmp_path):
    from hub_container_executor.runtime_profile import runtime_profile_digest
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first, backend, _, manifest = make_fixture(tmp_path / "first", late_create=True)
    request = command(manifest, "deploy_container")
    assert first.submit(request).state == "held"
    operation = next(row for row in first.ledger.operation_rows(request.command_id)
                     if row["kind"] == "create_candidate")
    profile = backend.containers["c" * 64].profile
    context = first.ledger.create_context(operation["operation_id"])
    commitment = context["profile_commitment"]
    assert context["schema"] == 2
    assert commitment != runtime_profile_digest(profile).removeprefix("sha256:")
    assert "profile_digest" not in context
    assert first.ledger.matches_profile_commitment(profile, commitment)
    second, _, _, _ = make_fixture(tmp_path / "second")
    assert second.ledger.profile_commitment(profile) != commitment
    second.close()
    first = reopen_adapter(first)
    assert first.ledger.matches_profile_commitment(profile, commitment)
    assert not first.ledger.matches_profile_commitment(
        replace(profile, environment=profile.environment + (("OTHER", "changed"),)), commitment)
    first.close()


def test_completed_create_reconcile_uses_authenticated_context_without_replay(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path)
    def fail_start(*args):
        raise TimeoutError("synthetic lost start")
    backend.start_container = fail_start
    request = command(manifest, "deploy_container")
    assert adapter.submit(request).state == "held"
    adapter = reopen_adapter(adapter)
    calls = []
    def restore(plan, operation, container, timeout):
        calls.append((plan, operation, container))
        return backend.containers[container]
    backend.restore_container_context = restore
    backend.events.clear()
    assert adapter.reconcile(request.command_id).state == "held"
    assert len(calls) == 1 and calls[0][2] == "c" * 64
    assert calls[0][0].profile == backend.containers["c" * 64].profile
    assert not any(event in backend.events for event in ("create", "start", "probe"))
    adapter.close()


def test_completed_create_bad_receipt_never_restores(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path)
    backend.start_container = lambda *args: (_ for _ in ()).throw(TimeoutError())
    request = command(manifest, "deploy_container")
    assert adapter.submit(request).state == "held"
    operation = next(row for row in adapter.ledger.operation_rows(request.command_id)
                     if row["kind"] == "create_candidate")
    with adapter.ledger._connection() as db:
        db.execute("UPDATE operations SET receipt_mac=? WHERE operation_id=?", ("0" * 64, operation["operation_id"]))
    adapter = reopen_adapter(adapter)
    calls = []
    backend.restore_container_context = lambda *args: calls.append(args)
    backend.events.clear()
    assert adapter.reconcile(request.command_id).state == "held"
    assert calls == [] and backend.events == []
    adapter.close()


def test_completed_create_restores_again_after_later_inspection_fails(tmp_path):
    adapter, backend, _, manifest = make_fixture(tmp_path)
    backend.start_container = lambda *args: (_ for _ in ()).throw(TimeoutError())
    request = command(manifest, "deploy_container")
    assert adapter.submit(request).state == "held"
    adapter = reopen_adapter(adapter)
    restores = []
    def restore(plan, operation, container, timeout):
        restores.append(operation)
        return backend.containers[container]
    backend.restore_container_context = restore
    calls = []
    def transient_inspect(operation, timeout):
        calls.append(operation)
        if len(calls) == 1:
            raise TimeoutError()
        return OperationObservation(operation, "pending", False, None)
    backend.inspect_operation = transient_inspect
    assert adapter.reconcile(request.command_id).state == "held"
    assert adapter.reconcile(request.command_id).state == "held"
    assert len(restores) == 2 and len(calls) == 2
    assert backend.events.count("create") == 1 and backend.events.count("probe") == 0
    adapter.close()


def test_enrolled_baseline_uses_separate_read_only_inspector(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hub_container_executor.daemon_backend import DockerEngineBackend
    from hub_container_executor.baseline_inspection import BaselineInspectionBackend
    from hub_container_executor.runtime_profile import derive_runtime_profile
    adapter, backend, _, _ = make_fixture(tmp_path)
    baseline = replace(adapter.baseline_provider.baseline, gate_socket_directory='/run/baseline-gate')
    image = verify_image_record(adapter.manifest, 'rollback', adapter.record_store, adapter.metadata_store, FakeTrust())
    defaults = adapter._defaults_from_record(image)
    secrets = dict(adapter.secret_resolver.values)
    profile = derive_runtime_profile(adapter.manifest, defaults, baseline.state_generation_id, secrets,
                                     gate_socket_directory=baseline.gate_socket_directory)
    selection = ActualImageSelection(image.runnable_reference, image.platform_manifest_digest, image.image_config_id, image.platform)
    real = object.__new__(DockerEngineBackend)
    real._gate_mount_verifier = SimpleNamespace(source_directory=baseline.gate_socket_directory)
    real.binding = object()
    monkeypatch.setattr(real, 'inspect_image', lambda *_: defaults)
    monkeypatch.setattr(real, 'inspect_container', lambda *_: ContainerObservation(
        baseline.container_id, selection, profile, baseline.state_generation_id, 'running'))
    adapter.baseline_inspection_backend = BaselineInspectionBackend(real)
    try:
        adapter._verify_baseline_container(baseline, image, defaults, secrets, time.monotonic() + 5)
        adapter.baseline_inspection_backend.source_directory = '/run/wrong-gate'
        with pytest.raises(adapter_module._Held):
            adapter._verify_baseline_container(baseline, image, defaults, secrets, time.monotonic() + 5)
        assert not any(event in {'create', 'start', 'stop'} for event in backend.events)
    finally:
        adapter.close()
