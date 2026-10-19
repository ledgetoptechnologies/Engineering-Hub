"""Real Linux UID-10001 SQLite generation copies; not baseline health evidence."""
from dataclasses import replace
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Host state provider requires actual Linux ownership")

from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.backend import BaselineIdentity
from hub_container_executor.state_provider import FrozenSQLiteStateProvisioner, StateProvisionError
from hub_container_executor import state_provider as provider_module
from tests.test_hub_container_pilot import pilot_manifest

pytestmark = pytest.mark.skipif(os.getuid() != 10001 or os.getgid() != 10001,
                                reason="Provider must run as actual pre-provisioned app UID/GID10001")


def digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


@pytest.fixture
def rig(tmp_path):
    root, reservations = tmp_path / "generations", tmp_path / "reservations"
    root.mkdir(mode=0o700)
    reservations.mkdir(mode=0o700)
    baseline_dir = root / "baseline-g1"
    baseline_dir.mkdir(mode=0o700)
    live = baseline_dir / "hub.sqlite3"
    with closing(sqlite3.connect(live)) as db:
        db.execute("CREATE TABLE fixture(v TEXT)")
        db.execute("INSERT INTO fixture VALUES ('enrolled synthetic baseline')")
        db.commit()
    live.chmod(0o600)
    frozen = root / "frozen-baseline.sqlite3"
    frozen.write_bytes(live.read_bytes())
    frozen.chmod(0o600)
    values = pilot_manifest().model_dump(mode="json", by_alias=True)
    values["state_policy"]["baseline_backup_digest"] = digest(frozen.read_bytes())
    values["retention"]["max_generations"] = 4
    manifest = PilotManifest.from_bytes(__import__("json").dumps(values).encode())
    baseline = BaselineIdentity(manifest.state_policy.baseline_id, manifest.state_policy.baseline_backup_digest,
                                "b" * 64, manifest.rollback.image_config_id, "baseline-g1", True, "enrolled-test-proof")

    class ExternalTestEnrollment:
        approved = True

        def enrolled_baseline(self, manifest):
            return baseline

        def verify_baseline(self, value, manifest):
            return self.approved and value == baseline

    enrollment = ExternalTestEnrollment()
    inventory = (("hub.sqlite3", live.stat().st_size, digest(live.read_bytes())),)

    def build(*, create=True, **changes):
        kwargs = dict(manifest=manifest, baseline=baseline, baseline_provider=enrollment,
                      frozen_backup=frozen, generation_root=root, reservation_root=reservations,
                      baseline_generation_inventory=inventory, create=create, allow_ephemeral_test_storage=True)
        kwargs.update(changes)
        return FrozenSQLiteStateProvisioner(**kwargs)

    return SimpleNamespace(root=root, reservations=reservations, baseline_dir=baseline_dir,
                           live=live, frozen=frozen, manifest=manifest, baseline=baseline,
                           enrollment=enrollment, inventory=inventory, build=build)


def prepare(store, rig, *, role="candidate", operation=None, run=None, **changes):
    kwargs = dict(manifest=rig.manifest, run_id=run or str(uuid4()), baseline=rig.baseline,
                  operation_id=operation or str(uuid4()), deadline_monotonic=time.monotonic() + 5)
    kwargs.update(changes)
    return getattr(store, "prepare_" + role)(**kwargs)


def test_real_candidate_and_rollback_have_durable_distinct_generations_without_baseline_mutation(rig):
    before_live, before_frozen = rig.live.read_bytes(), rig.frozen.read_bytes()
    operation, run = str(uuid4()), str(uuid4())
    with rig.build() as store:
        candidate = prepare(store, rig, operation=operation, run=run)
        assert candidate.generation_id == store._generation(run, operation, "candidate")
        rollback = prepare(store, rig, role="rollback", run=run)
        assert candidate.generation_id != rollback.generation_id
        for result in (candidate, rollback):
            from hub_container_executor.adapter import HostAdapter
            HostAdapter._validate_generation(SimpleNamespace(manifest=rig.manifest), result, rig.baseline)
            database = rig.root / result.generation_id / "hub.sqlite3"
            assert result.previous_generation_id == rig.baseline.state_generation_id
            assert result.source_backup_digest == rig.baseline.backup_digest
            assert result.generation_digest == digest(database.read_bytes())
            assert result.size_bytes == database.stat().st_size and result.baseline_unchanged is True
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                assert db.execute("SELECT v FROM fixture").fetchall() == [("enrolled synthetic baseline",)]
        with pytest.raises(StateProvisionError):
            prepare(store, rig, operation=operation, run=run)
    assert rig.live.read_bytes() == before_live and rig.frozen.read_bytes() == before_frozen
    with rig.build(create=False) as reopened:
        with pytest.raises(StateProvisionError):
            prepare(reopened, rig, operation=operation, run=run)


def test_private_reservation_root_single_owner_and_enrollment_reopen(rig):
    run = str(uuid4())
    with rig.build() as store:
        with pytest.raises(StateProvisionError):
            rig.build(create=False)
        assert prepare(store, rig, run=run)
    with rig.build(create=False) as reopened:
        with pytest.raises(StateProvisionError):
            prepare(reopened, rig)  # Another run cannot take the reserved capacity.
        with pytest.raises(StateProvisionError):
            prepare(reopened, rig, role="rollback")
        assert prepare(reopened, rig, role="rollback", run=run)


def test_zero_byte_enrolled_baseline_wal_is_hashed_without_mutation(rig):
    wal = rig.baseline_dir / "hub.sqlite3-wal"
    wal.touch(mode=0o600)
    inventory = rig.inventory + ((wal.name, 0, digest(b"")),)
    with rig.build(baseline_generation_inventory=inventory) as store:
        result = prepare(store, rig)
        assert result.baseline_unchanged is True
        assert wal.is_file() and wal.stat().st_size == 0


@pytest.mark.parametrize("case", ["no-verifier", "denied", "wrong-baseline", "wrong-inventory", "too-many-inventory"])
def test_independent_baseline_and_explicit_inventory_are_required(rig, case):
    kwargs = {}
    if case == "no-verifier":
        kwargs["baseline_provider"] = None
    elif case == "denied":
        rig.enrollment.approved = False
    elif case == "wrong-baseline":
        kwargs["baseline"] = replace(rig.baseline, backup_digest="sha256:" + "f" * 64)
    elif case == "wrong-inventory":
        kwargs["baseline_generation_inventory"] = (("../escape", 100, "sha256:" + "a" * 64),)
    else:
        kwargs["baseline_generation_inventory"] = tuple(("file" + str(i), 1, "sha256:" + "a" * 64) for i in range(17))
    with pytest.raises(StateProvisionError):
        rig.build(**kwargs)


@pytest.mark.parametrize("change", ["run", "operation", "baseline", "manifest", "expired", "nan", "boolean"])
def test_invalid_request_fails_without_any_generation_or_intent(rig, change):
    with rig.build() as store:
        kwargs = {}
        if change in {"run", "operation"}:
            kwargs[change + "_id"] = "not-canonical-uuid"
        elif change == "baseline":
            kwargs["baseline"] = replace(rig.baseline, proof_id="foreign-proof")
        elif change == "manifest":
            kwargs["manifest"] = object()
        else:
            kwargs["deadline_monotonic"] = {"expired": time.monotonic() - 1, "nan": float("nan"), "boolean": True}[change]
        before = set(rig.root.iterdir()), set(rig.reservations.iterdir())
        with pytest.raises(StateProvisionError):
            prepare(store, rig, **kwargs)
        assert before == (set(rig.root.iterdir()), set(rig.reservations.iterdir()))


@pytest.mark.parametrize("entry", ["foreign-root", "foreign-reservation", "backup-wal", "backup-shm", "backup-journal",
                                  "changed-live", "changed-backup", "symlink-root-entry", "hardlink-live"])
def test_foreign_entries_or_mutated_baseline_hold_without_deletion(rig, entry):
    with rig.build() as store:
        if entry == "foreign-root":
            (rig.root / "operator-data").write_bytes(b"keep")
        elif entry == "foreign-reservation":
            (rig.reservations / "unknown").write_bytes(b"keep")
        elif entry.startswith("backup-"):
            rig.frozen.with_name(rig.frozen.name + "-" + entry.removeprefix("backup-")).touch()
        elif entry == "changed-live":
            with closing(sqlite3.connect(rig.live)) as db:
                db.execute("INSERT INTO fixture VALUES ('changed')")
                db.commit()
        elif entry == "changed-backup":
            rig.frozen.write_bytes(b"changed frozen bytes")
        elif entry == "symlink-root-entry":
            (rig.root / "foreign-link").symlink_to(rig.frozen)
        else:
            os.link(rig.live, rig.root / "extra-hardlink")
        before = set(rig.root.iterdir())
        with pytest.raises(StateProvisionError):
            prepare(store, rig)
        assert set(rig.root.iterdir()) == before


def test_two_generation_quota_denies_candidate_before_intent_to_preserve_rollback(rig):
    values = __import__("json").loads(rig.manifest.canonical_bytes())
    values["retention"]["max_generations"] = 2
    rig.manifest = PilotManifest.from_bytes(__import__("json").dumps(values).encode())
    with rig.build(manifest=rig.manifest) as store:
        before = set(rig.root.iterdir()), set(rig.reservations.iterdir())
        with pytest.raises(StateProvisionError):
            prepare(store, rig)
        assert before == (set(rig.root.iterdir()), set(rig.reservations.iterdir()))


def test_copy_failure_keeps_durable_intent_no_replay_and_preserves_baseline(rig, monkeypatch):
    before = rig.live.read_bytes(), rig.frozen.read_bytes()
    operation = str(uuid4())
    calls = []

    def fail(source, destination, **kwargs):
        calls.append(kwargs)
        assert kwargs["deadline_monotonic"] > time.monotonic()
        raise RuntimeError("synthetic copy failure")

    with rig.build() as store:
        monkeypatch.setattr(provider_module, "copy_sqlite_generation", fail)
        with pytest.raises(StateProvisionError):
            prepare(store, rig, operation=operation)
        assert (rig.reservations / (operation + ".json")).is_file()
        assert not (rig.reservations / (operation + ".complete")).exists()
        with pytest.raises(StateProvisionError):
            prepare(store, rig, operation=operation)
        assert len(calls) == 1
        assert set(path.name for path in rig.root.iterdir()) == {rig.frozen.name, rig.baseline.state_generation_id}
    assert before == (rig.live.read_bytes(), rig.frozen.read_bytes())


def test_zero_metadata_write_fails_promptly_and_never_grants_success(rig, monkeypatch):
    with rig.build() as store:
        monkeypatch.setattr(provider_module.os, "write", lambda *args: 0)
        with pytest.raises(StateProvisionError):
            prepare(store, rig)
    assert not any(path.name.startswith("candidate-") for path in rig.root.iterdir())


def test_byte_quota_counts_frozen_backup_and_complete_baseline_inventory(rig):
    payload = rig.baseline_dir / "payload.bin"
    payload.write_bytes(b"x" * (16 * 1024 * 1024))
    payload.chmod(0o600)
    inventory = rig.inventory + (("payload.bin", payload.stat().st_size, digest(payload.read_bytes())),)
    values = __import__("json").loads(rig.manifest.canonical_bytes())
    values["retention"]["max_storage_bytes"] = 16 * 1024 * 1024
    rig.manifest = PilotManifest.from_bytes(__import__("json").dumps(values).encode())
    with rig.build(manifest=rig.manifest, baseline_generation_inventory=inventory) as store:
        before = set(rig.root.iterdir()), set(rig.reservations.iterdir())
        with pytest.raises(StateProvisionError):
            prepare(store, rig)
        assert before == (set(rig.root.iterdir()), set(rig.reservations.iterdir()))


def padded_baseline(rig, *, copies_of_headroom):
    maximum = 16 * 1024 * 1024
    backup_size = rig.frozen.stat().st_size
    payload = rig.baseline_dir / "payload.bin"
    payload.write_bytes(b"x" * (maximum - (2 + copies_of_headroom) * backup_size))
    payload.chmod(0o600)
    inventory = rig.inventory + ((payload.name, payload.stat().st_size, digest(payload.read_bytes())),)
    values = __import__("json").loads(rig.manifest.canonical_bytes())
    values["retention"]["max_generations"] = 3
    values["retention"]["max_storage_bytes"] = maximum
    rig.manifest = PilotManifest.from_bytes(__import__("json").dumps(values).encode())
    return inventory


def test_candidate_requires_byte_headroom_for_both_itself_and_mandatory_rollback(rig):
    inventory = padded_baseline(rig, copies_of_headroom=1)
    with rig.build(manifest=rig.manifest, baseline_generation_inventory=inventory) as store:
        before = set(rig.root.iterdir()), set(rig.reservations.iterdir())
        with pytest.raises(StateProvisionError):
            prepare(store, rig)
        assert before == (set(rig.root.iterdir()), set(rig.reservations.iterdir()))


def test_matching_run_rollback_uses_reserved_exact_count_and_bytes_after_reopen(rig):
    inventory = padded_baseline(rig, copies_of_headroom=2)
    run = str(uuid4())
    with rig.build(manifest=rig.manifest, baseline_generation_inventory=inventory) as store:
        first = prepare(store, rig, run=run)
        assert first.baseline_unchanged is True
        before = set(rig.root.iterdir()), set(rig.reservations.iterdir())
        with pytest.raises(StateProvisionError):
            prepare(store, rig)  # This would consume the rollback's last copy-sized allowance.
        with pytest.raises(StateProvisionError):
            prepare(store, rig, run=run)  # One candidate per run, even with a different operation UUID.
        assert before == (set(rig.root.iterdir()), set(rig.reservations.iterdir()))
    with rig.build(manifest=rig.manifest, baseline_generation_inventory=inventory, create=False) as reopened:
        before = set(rig.root.iterdir()), set(rig.reservations.iterdir())
        with pytest.raises(StateProvisionError):
            prepare(reopened, rig)
        with pytest.raises(StateProvisionError):
            prepare(reopened, rig, role="rollback")  # Wrong run cannot consume this reservation.
        assert before == (set(rig.root.iterdir()), set(rig.reservations.iterdir()))
        rollback = prepare(reopened, rig, role="rollback", run=run)
        assert rollback.baseline_unchanged is True
        assert sum(path.stat().st_size for path in rig.root.rglob("*") if path.is_file()) <= rig.manifest.retention.max_storage_bytes


@pytest.mark.parametrize("attack", ["root-permissions", "root-replacement", "baseline-symlink", "backup-hardlink"])
def test_unsafe_private_paths_hold_without_replacing_data(rig, attack):
    with rig.build() as store:
        if attack == "root-permissions":
            rig.root.chmod(0o755)
        elif attack == "root-replacement":
            rig.root.rename(rig.root.with_name("saved-generations"))
            rig.root.mkdir(mode=0o700)
        elif attack == "baseline-symlink":
            original = rig.baseline_dir.with_name("original-baseline")
            rig.baseline_dir.rename(original)
            rig.baseline_dir.symlink_to(original, target_is_directory=True)
        else:
            os.link(rig.frozen, rig.root / "backup-link")
        before = set(rig.root.iterdir())
        with pytest.raises(StateProvisionError):
            prepare(store, rig)
        assert set(rig.root.iterdir()) == before


def test_wal_header_is_rejected_without_opening_sqlite_or_creating_companions(rig, monkeypatch):
    with rig.build() as store:
        contents = bytearray(rig.frozen.read_bytes())
        contents[18:20] = b"\x02\x02"
        rig.frozen.write_bytes(contents)
        monkeypatch.setattr(provider_module, "copy_sqlite_generation", lambda *a, **kw: pytest.fail("WAL backup must not be opened"))
        before = rig.frozen.read_bytes(), set(rig.root.iterdir())
        with pytest.raises(StateProvisionError):
            prepare(store, rig)
        assert before == (rig.frozen.read_bytes(), set(rig.root.iterdir()))


def test_real_copy_deadline_failure_cleans_own_empty_directory_and_retains_intent(rig, monkeypatch):
    original = provider_module.copy_sqlite_generation
    operation = str(uuid4())
    calls = []

    def expired(source, destination, **kwargs):
        calls.append(kwargs["deadline_monotonic"])
        monkeypatch.setattr(time, "monotonic", lambda: kwargs["deadline_monotonic"] + 1)
        return original(source, destination, **kwargs)

    with rig.build() as store:
        before = rig.frozen.read_bytes(), rig.live.read_bytes()
        monkeypatch.setattr(provider_module, "copy_sqlite_generation", expired)
        with pytest.raises(StateProvisionError):
            prepare(store, rig, operation=operation)
        assert calls and (rig.reservations / (operation + ".json")).exists()
        assert set(path.name for path in rig.root.iterdir()) == {rig.frozen.name, rig.baseline.state_generation_id}
        assert before == (rig.frozen.read_bytes(), rig.live.read_bytes())


def test_wrong_app_uid_and_unsafe_root_configuration_fail_before_creation(rig, monkeypatch):
    monkeypatch.setattr(provider_module.os, "getuid", lambda: 0)
    with pytest.raises(StateProvisionError):
        rig.build()
    assert list(rig.reservations.iterdir()) == []


def test_actual_process_crash_after_durable_intent_never_replays_or_deletes_artifacts(rig, monkeypatch):
    import multiprocessing

    run, operation = str(uuid4()), str(uuid4())
    before_baselines = rig.frozen.read_bytes(), rig.live.read_bytes()
    # Finish and release the first owner before the child independently opens
    # the enrolled provider. No PID-bound provider capability is reused.
    with rig.build() as initial:
        generation = initial._generation(run, operation, "candidate")

    def child():
        def crash_after_intent(*args, **kwargs):
            os._exit(23)  # Durable intent and own generation directory already exist.

        provider_module.copy_sqlite_generation = crash_after_intent
        with rig.build(create=False) as freshly_opened:
            prepare(freshly_opened, rig, run=run, operation=operation)
        os._exit(24)

    process = multiprocessing.get_context("fork").Process(target=child)
    try:
        process.start()
        process.join(5)
        assert not process.is_alive(), "Crashing child did not exit within the bounded wait"
        assert process.exitcode == 23
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            process.close()

    intent = rig.reservations / (operation + ".json")
    incomplete_generation = rig.root / generation
    assert intent.is_file() and not (rig.reservations / (operation + ".complete")).exists()
    assert incomplete_generation.is_dir() and list(incomplete_generation.iterdir()) == []
    before_artifacts = set(rig.root.iterdir()), set(rig.reservations.iterdir())
    intent_bytes = intent.read_bytes()

    def forbidden_replay(*args, **kwargs):
        pytest.fail("An uncertain durable operation must never copy again")

    monkeypatch.setattr(provider_module, "copy_sqlite_generation", forbidden_replay)
    with rig.build(create=False) as reopened:
        for same_run, same_operation in ((run, operation), (str(uuid4()), str(uuid4()))):
            with pytest.raises(StateProvisionError):
                prepare(reopened, rig, run=same_run, operation=same_operation)
        assert before_artifacts == (set(rig.root.iterdir()), set(rig.reservations.iterdir()))
        assert intent.read_bytes() == intent_bytes and incomplete_generation.is_dir()
    assert before_baselines == (rig.frozen.read_bytes(), rig.live.read_bytes())
