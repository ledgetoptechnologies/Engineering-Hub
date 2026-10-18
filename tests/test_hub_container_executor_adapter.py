"""Explicit fake boundaries exercise adapter state; these are not Docker evidence."""
import hashlib
import json
import uuid
from dataclasses import replace

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
    RunnerObservation,
    StateGeneration,
)
from hub_container_executor.build_record import verify_image_record
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

    def _new(self, operation_id, generation_id):
        self.calls.append(operation_id)
        return StateGeneration(generation_id=generation_id,
                               previous_generation_id=self.baseline.state_generation_id,
                               source_backup_digest=self.baseline.backup_digest,
                               generation_digest="sha256:" + "f" * 64,
                               size_bytes=4096, baseline_unchanged=True)

    def prepare_candidate(self, manifest, run_id, baseline, operation_id):
        return self._new(operation_id, "candidate-g1")

    def prepare_rollback(self, manifest, run_id, baseline, operation_id):
        return self._new(operation_id, "rollback-g1")


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

    def inspect_operation(self, operation_id, timeout_ms):
        self.events.append("inspect_operation")
        return self.terminal_operations.get(
            operation_id, OperationObservation(operation_id, "pending", False,
                                               "c" * 64 if self.fail_create_after_late_effect is False else None)
        )


def make_fixture(tmp_path, *, include_verifier=True, late_create=False, bad_runner=False,
                 alter_profile=False, terminal_start=False):
    candidate_manifest, candidate_raw, candidate_blobs = make_record("candidate")
    rollback_manifest, rollback_raw, rollback_blobs = make_record("rollback")
    manifest_data = make_manifest_data()
    candidate_record = json.loads(candidate_raw)
    rollback_record = json.loads(rollback_raw)
    manifest_data["candidate"]["image_config_id"] = candidate_record["selection"]["image_config_id"]
    manifest_data["candidate"]["provenance"]["attestation_digest"] = digest(candidate_raw)
    manifest_data["rollback"]["image_config_id"] = rollback_record["selection"]["image_config_id"]
    manifest_data["rollback"]["provenance"]["attestation_digest"] = digest(rollback_raw)
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
    backend = FakeBackend(manifest, secrets, late_create, bad_runner=bad_runner,
                          alter_profile=alter_profile, terminal_start=terminal_start)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    adapter = HostAdapter(
        root / "ledger.sqlite3", manifest,
        record_store=record_store, metadata_store=metadata_store,
        provenance_verifier=FakeTrust() if include_verifier else None,
        backend=backend, baseline_provider=FakeBaselineProvider(baseline),
        state_provisioner=state, secret_resolver=secrets,
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


def test_foreign_runner_is_denied_before_state_provisioning_or_daemon_effect(tmp_path):
    adapter, backend, state, manifest = make_fixture(tmp_path, bad_runner=True)
    response = adapter.submit(command(manifest, "deploy_container"))
    assert response.state == "held" and response.reason == "daemon_unavailable"
    assert state.calls == []
    assert backend.events.count("create") == 0
    adapter.close()


def test_only_typed_terminal_effect_failure_is_failed_and_rollback_can_follow(tmp_path):
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
