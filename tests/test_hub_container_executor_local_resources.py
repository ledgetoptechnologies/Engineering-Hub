"""Real local-provider assembly over synthetic protected artifacts, not enrollment.

Only the existing providers' ephemeral-storage admission flag is injected in
tests. The composition itself has no bypass; no Docker request is permitted.
"""
import asyncio
from contextlib import closing, contextmanager
from dataclasses import replace
import inspect
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("fcntl", reason="Owned local resources require Linux")

from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor import local_resources as module
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.build_enrollment import BuildEnrollment
from hub_container_executor.daemon_backend import DockerEngineBackend, DockerEngineBinding
from hub_container_executor.state_provider import StateProvisionError
from tests.test_hub_container_executor_build_record import canonical, digest, make_record

pytestmark = pytest.mark.skipif(os.getuid() != 10001 or os.getgid() != 10001,
                                reason="Actual app UID/GID10001 required")
PROFILE = "sha256:" + "e" * 64


def put(root, raw):
    identity = digest(raw)
    path = root / identity[7:]
    path.write_bytes(raw)
    path.chmod(0o600)
    return identity


class ExplicitSyntheticDriverTrust:
    """Caller-owned fake only; constructor must not sample it as proof."""
    def __init__(self):
        self.calls = 0
        self.closed = False

    def implementation_commit(self):
        self.calls += 1
        raise AssertionError("Assembly must not claim observed runtime identity")

    def close(self):
        self.closed = True


@pytest.fixture
def rig(tmp_path, monkeypatch):
    paths = {}
    for name in ("records", "metadata", "receipts", "secrets", "generations", "reservations", "adapter", "journal"):
        paths[name] = tmp_path / name
        paths[name].mkdir(mode=0o700)
    live_root = paths["generations"] / "baseline-g1"
    live_root.mkdir(mode=0o700)
    live = live_root / "hub.sqlite3"
    with closing(sqlite3.connect(live)) as db:
        db.execute("CREATE TABLE fixture(v TEXT)")
        db.execute("INSERT INTO fixture VALUES ('synthetic approved baseline')")
        db.commit()
    live.chmod(0o600)
    frozen = paths["generations"] / "frozen-baseline.sqlite3"
    frozen.write_bytes(live.read_bytes())
    frozen.chmod(0o600)

    candidate, candidate_raw, candidate_blobs = make_record("candidate")
    rollback, rollback_raw, rollback_blobs = make_record("rollback")
    values = candidate.model_dump(mode="json", by_alias=True)
    values["rollback"] = rollback.model_dump(mode="json", by_alias=True)["rollback"]
    values["state_policy"]["baseline_backup_digest"] = digest(frozen.read_bytes())
    manifest = PilotManifest.from_bytes(canonical(values))
    records = {"candidate": candidate_raw, "rollback": rollback_raw}
    for raw in records.values():
        put(paths["records"], raw)
    for identity, raw in {**candidate_blobs, **rollback_blobs}.items():
        assert put(paths["metadata"], raw) == identity
    enrollments = tuple(BuildEnrollment(manifest.digest(), role, digest(raw),
                                       json.loads(raw)["builder_id"], json.loads(raw)["invocation_id"])
                        for role, raw in records.items())
    row = dict(schema="hub_baseline_receipt_v1", campaign_id=manifest.campaign_id, proof_id=str(uuid4()),
               manifest_digest=manifest.digest(), baseline_id=manifest.state_policy.baseline_id,
               runner=manifest.runner_binding.model_dump(mode="json"),
               rollback=dict(json.loads(rollback_raw)["selection"], source_commit=manifest.rollback.source_commit,
                             record_digest=digest(rollback_raw)),
               container_id="c" * 64, state_generation_id="baseline-g1",
               backup=dict(digest=digest(frozen.read_bytes()), size_bytes=frozen.stat().st_size),
               runtime_profile_digest=PROFILE,
               probe_set_digest=digest(canonical([p.model_dump(mode="json") for p in manifest.probes.rollback])),
               observation=dict(health="healthy", evidence_verified=True, observed_at=1_700_000_000),
               baseline_inventory=[dict(name="hub.sqlite3", size_bytes=live.stat().st_size,
                                        digest=digest(live.read_bytes()))])
    receipt_pin = put(paths["receipts"], canonical(row))
    secret_bindings = tuple((value.slot_id, value.version,
                             put(paths["secrets"], ("synthetic-value-" + name.upper() + "_TOKEN").encode()))
                            for name in ("codex", "hermes", "claude")
                            for value in (getattr(manifest.runtime.secret_bindings, name),))
    binding = DockerEngineBinding(manifest.runner_binding.runner_id, manifest.runner_binding.daemon_identity,
                                  manifest.runner_binding.platform, str(tmp_path / "no-daemon.sock"),
                                  "1.54", str(paths["generations"]))
    trust = ExplicitSyntheticDriverTrust()
    params = dict(manifest=manifest, binding=binding, adapter_database=paths["adapter"] / "ledger.sqlite3",
                  journal_database=paths["journal"] / "journal.sqlite3", journal_create=True,
                  record_root=paths["records"], metadata_root=paths["metadata"],
                  baseline_receipt_root=paths["receipts"], secret_root=paths["secrets"],
                  secret_bindings=secret_bindings, build_enrollments=enrollments,
                  baseline_receipt_digest=receipt_pin, baseline_runtime_profile_digest=PROFILE,
                  frozen_backup=frozen, reservation_root=paths["reservations"],
                  state_reservations_create=True, driver_identity_verifier=trust)
    constructed, closed = [], []
    # Exact real providers, allowing only this test's ephemeral filesystem.
    for alias in ("PrivateContentStore", "ImmutableTestSecretResolver", "FrozenSQLiteStateProvisioner", "SQLiteOperationJournal"):
        original = getattr(module, alias)

        def constructor(*args, _original=original, _alias=alias, **kwargs):
            assert "allow_ephemeral_test_storage" not in kwargs
            obj = _original(*args, **kwargs, allow_ephemeral_test_storage=True)
            constructed.append((_alias, obj))
            original_close = obj.close

            def close():
                closed.append((_alias, obj))
                original_close()
            obj.close = close
            return obj
        monkeypatch.setattr(module, alias, constructor)
    requests = []
    def deny_request(*args, **kwargs):
        requests.append((args, kwargs))
        raise AssertionError("No daemon request during assembly/cleanup")
    monkeypatch.setattr(httpx.Client, "send", deny_request)
    yield SimpleNamespace(params=params, paths=paths, live=live, frozen=frozen, row=row, records=records,
                          trust=trust, constructed=constructed, closed=closed)
    assert not requests, "A negative case must not pass by swallowing a daemon request denial"


def build(rig, **changes):
    return module.local_host_adapter_resources(**(rig.params | changes))


def test_complete_real_assembly_is_inert_and_cleans_owned_providers(rig):
    before = rig.live.read_bytes(), rig.frozen.read_bytes()
    with build(rig) as adapter:
        assert type(adapter) is HostAdapter
        assert type(adapter.backend) is DockerEngineBackend
        assert adapter.backend._using_test_transport is False
        assert adapter.manifest is not rig.params["manifest"]
        assert adapter.manifest.digest() == rig.params["manifest"].digest()
        assert adapter.driver_identity_verifier is rig.trust and rig.trust.calls == 0
        assert adapter.baseline_provider.enrolled_baseline(adapter.manifest).state_generation_id == "baseline-g1"
        for name in ("codex", "hermes", "claude"):
            binding = getattr(adapter.manifest.runtime.secret_bindings, name)
            assert adapter.secret_resolver.resolve_test_secret(binding.slot_id, binding.version) == "synthetic-value-" + name.upper() + "_TOKEN"
        assert not adapter._closed
    assert adapter._closed and adapter.backend._client.is_closed
    assert all(any(closed is obj for _, closed in rig.closed) for _, obj in rig.constructed)
    assert not rig.trust.closed and rig.trust.calls == 0
    assert before == (rig.live.read_bytes(), rig.frozen.read_bytes())
    assert set(rig.paths["generations"].iterdir()) == {rig.live.parent, rig.frozen}


def test_explicit_reopen_preserves_existing_exact_state(rig):
    with build(rig):
        pass
    persisted = {str(p): p.read_bytes() for name in ("reservations",) for p in rig.paths[name].iterdir()}
    with build(rig, journal_create=False, state_reservations_create=False) as adapter:
        assert adapter.manifest.digest() == rig.params["manifest"].digest()
    assert persisted == {str(p): p.read_bytes() for p in rig.paths["reservations"].iterdir()}
    with pytest.raises(Exception):
        with build(rig):
            pass


@pytest.mark.parametrize("field", ["journal_create", "state_reservations_create"])
def test_reopen_missing_is_not_automatic_creation(rig, field):
    with pytest.raises(Exception):
        with build(rig, **{field: False}):
            pass
    assert not rig.params["adapter_database"].exists()


@pytest.mark.parametrize("field,value", [("runner_id", "foreign-runner"), ("daemon_identity", "foreign-daemon")])
def test_binding_mismatch_denied_before_resource_acquisition(rig, field, value):
    binding = replace(rig.params["binding"], **{field: value})
    with pytest.raises(module.LocalResourcesError):
        with build(rig, binding=binding):
            pass
    assert not rig.constructed
    assert not list(rig.paths["reservations"].iterdir())


def test_platform_binding_mismatch_denied_before_resources(rig):
    platform = rig.params["binding"].platform.model_copy(update={"architecture": "arm64", "variant": "v8"})
    binding = replace(rig.params["binding"], platform=platform)
    with pytest.raises(module.LocalResourcesError):
        with build(rig, binding=binding):
            pass
    assert not rig.constructed


@pytest.mark.parametrize("field", ["adapter_database", "journal_database", "record_root", "secret_root", "reservation_root"])
def test_private_path_overlap_denied_before_resources(rig, field):
    collision = rig.paths["generations"] / "forbidden.sqlite3" if field.endswith("database") else rig.paths["generations"]
    with pytest.raises(module.LocalResourcesError):
        with build(rig, **{field: collision}):
            pass
    assert not rig.constructed


@pytest.mark.parametrize("value", [None, object(), SimpleNamespace(implementation_commit="not-callable")])
def test_identity_verifier_required_without_manifest_echo_default(rig, value):
    with pytest.raises(module.LocalResourcesError):
        with build(rig, driver_identity_verifier=value):
            pass
    assert not rig.constructed and rig.trust.calls == 0


def test_no_configuration_bypass_or_implicit_create_defaults():
    signature = inspect.signature(module.local_host_adapter_resources)
    assert "allow_ephemeral_test_storage" not in signature.parameters
    for name in ("journal_create", "state_reservations_create", "driver_identity_verifier"):
        assert signature.parameters[name].default is inspect.Parameter.empty


@pytest.mark.parametrize("role", ["candidate", "rollback"])
def test_independent_enrollment_required_for_each_role(rig, role):
    enrollments = tuple(replace(item, invocation_id=str(uuid4())) if item.role == role else item
                        for item in rig.params["build_enrollments"])
    with pytest.raises(Exception):
        with build(rig, build_enrollments=enrollments):
            pass
    assert not rig.params["journal_database"].exists()
    assert not rig.params["adapter_database"].exists()
    assert all(any(closed is obj for _, closed in rig.closed) for _, obj in rig.constructed)


@pytest.mark.parametrize("field", ["baseline_receipt_digest", "baseline_runtime_profile_digest"])
def test_independent_baseline_pins_must_match(rig, field):
    with pytest.raises(Exception):
        with build(rig, **{field: "sha256:" + "9" * 64}):
            pass
    assert not rig.params["journal_database"].exists()
    assert not rig.params["adapter_database"].exists()


def test_pinned_receipt_inventory_must_match_actual_baseline(rig):
    row = dict(rig.row)
    row["baseline_inventory"] = [dict(name="hub.sqlite3", size_bytes=rig.live.stat().st_size,
                                       digest="sha256:" + "9" * 64)]
    pin = put(rig.paths["receipts"], canonical(row))
    with build(rig, baseline_receipt_digest=pin) as adapter:
        with pytest.raises(StateProvisionError):
            adapter.state_provisioner.prepare_candidate(adapter.manifest, str(uuid4()),
                adapter.baseline_provider.enrolled_baseline(adapter.manifest), str(uuid4()),
                deadline_monotonic=time.monotonic() + 5)
        assert set(rig.paths["generations"].iterdir()) == {rig.live.parent, rig.frozen}


def test_wrong_frozen_backup_does_not_become_new_baseline(rig):
    rig.frozen.write_bytes(b"not enrolled SQLite bytes")
    with build(rig) as adapter:
        with pytest.raises(StateProvisionError):
            adapter.state_provisioner.prepare_candidate(adapter.manifest, str(uuid4()),
                adapter.baseline_provider.enrolled_baseline(adapter.manifest), str(uuid4()),
                deadline_monotonic=time.monotonic() + 5)
        assert set(rig.paths["generations"].iterdir()) == {rig.live.parent, rig.frozen}
    assert rig.frozen.read_bytes() == b"not enrolled SQLite bytes"


def test_assembled_state_provider_prepares_candidate_and_reserved_rollback(rig):
    before = rig.live.read_bytes(), rig.frozen.read_bytes()
    run = str(uuid4())
    with build(rig) as adapter:
        baseline = adapter.baseline_provider.enrolled_baseline(adapter.manifest)
        candidate = adapter.state_provisioner.prepare_candidate(
            adapter.manifest, run, baseline, str(uuid4()),
            deadline_monotonic=time.monotonic() + 5)
        rollback = adapter.state_provisioner.prepare_rollback(
            adapter.manifest, run, baseline, str(uuid4()),
            deadline_monotonic=time.monotonic() + 5)
        assert candidate.generation_id != rollback.generation_id
        for result in (candidate, rollback):
            actual = rig.paths["generations"] / result.generation_id / "hub.sqlite3"
            assert actual.read_bytes() == before[1]
            assert result.source_backup_digest == digest(before[1])
            assert result.generation_digest == digest(actual.read_bytes())
            assert result.baseline_unchanged is True
        assert rig.trust.calls == 0  # State-only preparation is not runtime identity proof.
    assert before == (rig.live.read_bytes(), rig.frozen.read_bytes())


def test_body_failure_closes_every_owned_handle_and_preserves_durable_state(rig):
    with pytest.raises(module.LocalResourcesError) as error:
        with build(rig) as adapter:
            raise LookupError("synthetic caller failure")
    assert "synthetic caller failure" not in str(error.value)
    assert adapter._closed and adapter.backend._client.is_closed
    assert all(any(closed is obj for _, closed in rig.closed) for _, obj in rig.constructed)
    assert rig.params["adapter_database"].exists()
    assert rig.params["journal_database"].exists()
    with build(rig, journal_create=False, state_reservations_create=False):
        pass


@pytest.mark.parametrize("stage", ["ProtectedBaselineProvider", "FrozenSQLiteStateProvisioner", "SQLiteOperationJournal", "DockerEngineBackend", "HostAdapter"])
def test_partial_construction_closes_earlier_resources_without_removing_state(rig, monkeypatch, stage):
    def fail(*a, **k):
        raise RuntimeError("synthetic-private-sentinel")
    monkeypatch.setattr(module, stage, fail)
    with pytest.raises(module.LocalResourcesError) as error:
        with build(rig):
            pass
    assert "synthetic-private-sentinel" not in str(error.value)
    assert all(any(closed is obj for _, closed in rig.closed) for _, obj in rig.constructed)
    assert rig.live.exists() and rig.frozen.exists()


def test_cleanup_failure_does_not_skip_later_closers(rig, monkeypatch):
    original = module.HostAdapter
    def make(*args, **kwargs):
        adapter = original(*args, **kwargs)
        original_close = adapter.close
        def close():
            original_close()
            raise RuntimeError("synthetic-private-cleanup")
        adapter.close = close
        return adapter
    monkeypatch.setattr(module, "HostAdapter", make)
    with pytest.raises(module.LocalResourcesError) as error:
        with build(rig) as adapter:
            pass
    assert "synthetic-private-cleanup" not in str(error.value)
    assert adapter._closed and adapter.backend._client.is_closed
    assert all(any(closed is obj for _, closed in rig.closed) for _, obj in rig.constructed)


@pytest.mark.parametrize("case", ["missing-slot", "wrong-version", "malformed-bytes"])
def test_exact_manifest_secret_versions_checked_before_mutable_state(rig, case):
    bindings = rig.params["secret_bindings"]
    if case == "missing-slot":
        bindings = bindings[1:]
    elif case == "wrong-version":
        bindings = ((bindings[0][0], 2, bindings[0][2]),) + bindings[1:]
    else:
        bindings = ((bindings[0][0], bindings[0][1], put(rig.paths["secrets"], b"private\ninvalid")),) + bindings[1:]
    with pytest.raises(module.LocalResourcesError) as error:
        with build(rig, secret_bindings=bindings):
            pass
    assert "private" not in str(error.value).lower()
    assert not rig.params["journal_database"].exists()
    assert not list(rig.paths["reservations"].iterdir())


def test_caller_manifest_and_binding_platform_are_independent_snapshots(rig):
    with build(rig) as adapter:
        original_manifest = adapter.manifest.digest()
        # Deliberately bypass frozen-model assignment, as an in-process caller
        # could; ordinary attribute assignment is correctly forbidden.
        object.__setattr__(rig.params["binding"].platform, "architecture", "arm64")
        object.__setattr__(rig.params["manifest"].runner_binding.platform, "architecture", "arm64")
        assert adapter.manifest.digest() == original_manifest
        assert adapter.backend.binding.platform.architecture == "amd64"


def test_distinct_spelling_same_root_inode_denied_before_resources(rig, monkeypatch):
    original = Path.stat
    def stat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == rig.paths["secrets"]:
            return original(rig.paths["records"], *args, **kwargs)
        return info
    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(module.LocalResourcesError):
        with build(rig):
            pass
    assert not rig.constructed


@pytest.mark.asyncio
async def test_concrete_factory_runs_in_owned_listener_and_exits_off_loop(rig):
    from hub_container_executor.service import serve_owned_host_executor
    main_thread = threading.get_ident()
    observed = {}
    stop = asyncio.Event()
    ready = asyncio.get_running_loop().create_future()

    @contextmanager
    def factory():
        with build(rig) as adapter:
            observed["adapter"] = adapter
            try:
                yield adapter
            finally:
                observed["exit_thread"] = threading.get_ident()

    serving = asyncio.create_task(serve_owned_host_executor(factory,
        bearer_token="synthetic-local-factory-token", supervisor_stop_grace_seconds=2000,
        host="127.0.0.1", port=0, stop_event=stop, install_signals=False,
        on_ready=lambda address: ready.set_result(address)))
    try:
        address = await asyncio.wait_for(asyncio.shield(ready), 8)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{address[1]}", trust_env=False) as client:
            result = await client.get("/v1/container-pilot/commands/" + str(uuid4()))
            assert result.status_code == 401
            result = await client.get("/v1/container-pilot/commands/" + str(uuid4()),
                headers={"Authorization": "Bearer synthetic-local-factory-token"})
            assert result.status_code == 404
        assert not observed["adapter"]._closed and rig.trust.calls == 0
    finally:
        stop.set()
        await serving
    assert observed["exit_thread"] != main_thread
    assert observed["adapter"]._closed and observed["adapter"].backend._client.is_closed
    assert all(any(closed is obj for _, closed in rig.closed) for _, obj in rig.constructed)
