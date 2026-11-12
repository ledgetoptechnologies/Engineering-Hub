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
_ORIGINAL_HTTPX_SEND = httpx.Client.send
import pytest

pytest.importorskip("fcntl", reason="Owned local resources require Linux")

from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor import local_resources as module
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.build_enrollment import BuildEnrollment
from hub_container_executor.daemon_backend import DockerEngineBackend, DockerEngineBinding
from hub_container_executor.state_provider import StateProvisionError, FrozenSQLiteStateProvisioner
from tests.test_hub_container_executor_build_record import canonical, digest, make_record
from tests.test_hub_current_executor_observation import setup as signed_observer_setup, Signer

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
            # SQLite backup preserves database contents, not necessarily every
            # header byte; hash the actual newly copied generation independently.
            with closing(sqlite3.connect(actual.as_uri() + "?mode=ro", uri=True)) as db:
                assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                assert db.execute("SELECT v FROM fixture").fetchall() == [("synthetic approved baseline",)]
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


def test_opt_in_resources_enable_protocol_without_contacting_daemon(rig):
    params=dict(rig.params, single_pilot_protocol=True)
    with module.local_host_adapter_resources(**params) as adapter:
        assert type(adapter) is HostAdapter
        assert adapter.ledger.single_pilot_enabled()
        assert adapter.ledger.inspect_single_pilot() is None
        assert type(adapter.backend) is DockerEngineBackend
        assert adapter.backend.operation_journal is not None
        assert type(adapter.state_provisioner) is FrozenSQLiteStateProvisioner


def test_bound_resources_reopen_only_with_real_signed_observer_and_existing_key(rig, signed_observer_setup, monkeypatch):
    from engineering_hub.current_executor_observation import CurrentExecutorObserver
    from hub_container_executor.bound_installed_config import InstalledBoundProfile
    from hub_container_executor.effect_mac_key import create_for_new_journal
    from hub_container_executor.postlaunch_delivery import ProtectedApprovalLease
    from hub_container_executor.runtime_identity import LaunchReceiptIdentityVerifier
    from tests.test_hub_container_effect_binding_canonical import fixture
    clock, release, launch, signer, old_observer = signed_observer_setup
    class Driver:
        def implementation_commit(self): return rig.params['manifest'].driver.implementation_commit
    path = rig.params['journal_database']
    key_id = create_for_new_journal(path.parent, path.name)
    params = dict(rig.params, single_pilot_protocol=True, driver_identity_verifier=Driver())
    with module.local_host_adapter_resources(**params) as adapter:
        adapter.backend.operation_journal.enable_bindings()
    release = replace(release, implementation_commit=rig.params['manifest'].driver.implementation_commit,
                      platform=rig.params['binding'].platform)
    launch = replace(launch, engine_id=rig.params['binding'].daemon_identity)
    observer = CurrentExecutorObserver(release=release, launch=launch, transport=Signer(release, launch, signer.key, clock))
    profile = InstalledBoundProfile(key_id, '/fixture/approval.json', bytes(32), 30000, (fixture(),))
    # This resource-lifecycle fixture isolates sequencing; crypto/delivery
    # checks are covered by postlaunch approval tests, not this synthetic lease.
    lease = ProtectedApprovalLease('/fixture/approval.json', bytes(32), SimpleNamespace(launch=launch),
                                   object.__new__(LaunchReceiptIdentityVerifier), release, bytes(32), launch.engine_id)
    monkeypatch.setattr(ProtectedApprovalLease, 'check', lambda self: self.approval)
    params.update(journal_create=False, state_reservations_create=False,
                  bound_profile=profile, bound_observer=observer, bound_release=release, bound_approval_lease=lease)
    try:
        with module.local_host_adapter_resources(**params) as adapter:
            assert adapter.effect_context_guard.scope.__self__ is adapter.backend
            assert adapter.backend.effect_binding_provider.source_ledger is adapter.ledger
            adapter.backend.operation_journal.require_no_pending(allow_bound=True)
        with pytest.raises(module.LocalResourcesError):
            with module.local_host_adapter_resources(**(params | {'bound_observer': object()})):
                pass
    finally:
        observer.close()
        old_observer.close()


def _attach_synthetic_engine(adapter, params, *, lose_stop=False):
    from tests.test_hub_container_executor_daemon_backend import container_inspect, docker_config, oci_archive, json_bytes
    backend=adapter.backend; manifest=adapter.manifest
    images={role:adapter._verified_image(role) for role in ('candidate','rollback')}
    image_by_ref={image.runnable_reference:image for image in images.values()}
    blobs={('sha256:'+path.name):path.read_bytes() for path in params['metadata_root'].iterdir()}
    secrets=adapter._resolve_secrets(); baseline=adapter.baseline_provider.enrolled_baseline(manifest)
    defaults=adapter._defaults_from_record(images['rollback'])
    profile=adapter._profile(images['rollback'],defaults,baseline.state_generation_id,secrets)
    containers={baseline.container_id:container_inspect(images['rollback'],profile,
        str(params['binding'].generation_root + '/' + baseline.state_generation_id),baseline.container_id,'running')}
    if backend.app_cgroup_observer is not None:
        containers[baseline.container_id]['HostConfig']['CgroupParent'] = backend.app_cgroup_observer._pins.parent
    executions={}; posts=[]; counter=0
    def handler(request):
        nonlocal counter
        path=request.url.raw_path.decode('ascii').split('?',1)[0]
        if request.method == 'POST': posts.append(path)
        if path.endswith('/info'):
            return httpx.Response(200,json={'ID':manifest.runner_binding.daemon_identity,'OSType':'linux','Architecture':'amd64'})
        if '/images/' in path and path.endswith('/json'):
            image=next(v for k,v in image_by_ref.items() if k.replace(':','%3A') in path)
            return httpx.Response(200,json=docker_config(image))
        if '/images/' in path and path.endswith('/get'):
            image=next(v for k,v in image_by_ref.items() if k.replace(':','%3A') in path)
            return httpx.Response(200,content=oci_archive(image,blobs))
        if '/containers/' in path and path.endswith('/json'):
            cid=path.split('/containers/',1)[1].removesuffix('/json')
            return httpx.Response(200,json=containers[cid]) if cid in containers else httpx.Response(404,json={'message':'absent'})
        if path.endswith('/containers/create'):
            body=json.loads(request.content); image=image_by_ref[body['Image']]
            cid='d'*64 if image.role=='candidate' else 'e'*64
            source=body['HostConfig']['Mounts'][0]['Source']; generation=source.rsplit('/',1)[1]
            active=adapter._profile(image,adapter._defaults_from_record(image),generation,secrets)
            containers[cid]=container_inspect(image,active,source,cid,'created')
            if backend.app_cgroup_observer is not None:
                containers[cid]['HostConfig']['CgroupParent'] = backend.app_cgroup_observer._pins.parent
            return httpx.Response(201,json={'Id':cid,'Warnings':[]})
        if path.endswith('/stop') and '/containers/' in path:
            cid=path.split('/containers/',1)[1].removesuffix('/stop')
            if lose_stop: raise httpx.ReadTimeout('synthetic lost stop response', request=request)
            containers[cid]['State']['Status']='exited'
            return httpx.Response(204)
        if path.endswith('/start') and '/containers/' in path:
            cid=path.split('/containers/',1)[1].removesuffix('/start'); containers[cid]['State']['Status']='running'
            return httpx.Response(204)
        if path.endswith('/exec') and '/containers/' in path:
            cid=path.split('/containers/',1)[1].removesuffix('/exec'); counter+=1; eid=f'{counter:064x}'
            argv=json.loads(request.content)['Cmd']; failed=cid=='d'*64 and argv[4]=='health_json'
            executions[eid]={'report':{'expectation_id':argv[7],'ok':not failed,'status':503 if failed else int(argv[6])},'exit':1 if failed else 0}
            return httpx.Response(201,json={'Id':eid})
        if path.endswith('/start') and '/exec/' in path:
            eid=path.split('/exec/',1)[1].removesuffix('/start'); raw=json_bytes(executions[eid]['report'])
            return httpx.Response(200,content=bytes([1,0,0,0])+len(raw).to_bytes(4,'big')+raw)
        if path.endswith('/json') and '/exec/' in path:
            eid=path.split('/exec/',1)[1].removesuffix('/json')
            return httpx.Response(200,json={'ID':eid,'Running':False,'ExitCode':executions[eid]['exit']})
        raise AssertionError('unexpected synthetic route '+path)
    backend._client.close(); backend._client=httpx.Client(transport=httpx.MockTransport(handler),base_url='http://docker')
    backend._client.send = _ORIGINAL_HTTPX_SEND.__get__(backend._client, httpx.Client)
    backend._using_test_transport=True
    return posts


def test_single_pilot_concrete_resources_four_phases_and_ack_with_synthetic_engine(rig):
    from engineering_hub.container_pilot_client import single_pilot_outcome_digest
    from tests.test_hub_container_executor_adapter import command
    class Driver:
        def implementation_commit(self): return rig.params['manifest'].driver.implementation_commit
    params=dict(rig.params, single_pilot_protocol=True, driver_identity_verifier=Driver())
    with module.local_host_adapter_resources(**params) as adapter:
        manifest=adapter.manifest
        posts=_attach_synthetic_engine(adapter,params)
        run=str(uuid4())
        for action, state, health in [('deploy_container','succeeded','unknown'),('smoke_container','failed','unhealthy'),
                                      ('rollback_container','succeeded','unknown'),('smoke_container_rollback','succeeded','healthy')]:
            req=command(manifest,action,run_id=run)
            result=adapter.submit(req)
            assert (result.state,result.evidence.health)==(state,health), (action,result)
            assert adapter.ledger.inspect_single_pilot()['state']=='awaiting_ack'
            acknowledged=adapter.acknowledge_single_pilot(req,single_pilot_outcome_digest(result))
            assert acknowledged.state == state
        assert sum(path.endswith('/containers/create') for path in posts)==2


@pytest.mark.parametrize('deny_start', [False, True])
def test_bound_single_pilot_submit_derives_start_target_from_create(rig, deny_start):
    """Actual v2 submit, real ledgers/key/state, synthetic Engine and verifier."""
    from uuid import UUID, uuid5
    from hub_container_executor.adapter import TrustedEffectContextGuard
    from hub_container_executor.effect_mac_key import create_for_new_journal, EffectMacKey
    from hub_container_executor.enrolled_effect_provider import enrolled_effect_provider, DeferredTargetEffect
    from tests.test_hub_container_effect_binding_canonical import fixture
    from tests.test_hub_container_executor_adapter import command
    class Driver:
        def implementation_commit(self): return rig.params['manifest'].driver.implementation_commit
    journal_path = rig.params['journal_database']
    key_id = create_for_new_journal(journal_path.parent, journal_path.name)
    params = dict(rig.params, single_pilot_protocol=True, driver_identity_verifier=Driver())
    with module.local_host_adapter_resources(**params) as adapter:
        journal = adapter.backend.operation_journal
        journal.enable_bindings()
        key = EffectMacKey(journal_path.parent, journal_path.name, expected_key_id=key_id)
        try:
            posts = _attach_synthetic_engine(adapter, params)
            req = command(adapter.manifest, 'deploy_container')
            op_state = str(uuid5(UUID(req.command_id), 'state_copy'))
            op_create = str(uuid5(UUID(req.command_id), 'create_candidate'))
            op_start = str(uuid5(UUID(req.command_id), 'start_candidate'))
            generation = adapter.state_provisioner._generation(req.run_id, op_state, 'candidate')
            image = adapter._verified_image('candidate')
            profile = adapter._profile(image, adapter._defaults_from_record(image), generation,
                                       adapter._resolve_secrets())
            create = replace(fixture(), operation_id=op_create, run_id=req.run_id,
                kind='create_candidate', fence=req.fencing_generation, owner_epoch=journal._owner.epoch,
                resource_name=f'{adapter.manifest.runtime.resource_name_prefix}-{req.run_id[:12]}-candidate',
                container_id=None, image_index_digest=image.runnable_reference,
                platform_manifest_digest=image.platform_manifest_digest, image_config_id=image.image_config_id,
                state_generation_id=generation, environment_names=tuple(n for n, _ in profile.environment),
                memory_bytes=profile.memory_bytes, cpu_millicores=profile.cpu_millicores,
                pids_limit=profile.pids_limit, tmpfs_bytes=profile.tmpfs[0][1])
            start = replace(create, operation_id=op_start, kind='start', resource_name=None)
            adapter.backend.effect_binding_provider = enrolled_effect_provider(journal, key,
                (create, DeferredTargetEffect(start, req.command_id)), source_ledger=adapter.ledger,
                verify_generation=lambda actual, _: not (deny_start and actual.context.kind == 'start_candidate'))
            adapter.effect_context_guard = TrustedEffectContextGuard(lambda _: True, adapter.backend.effect_context_scope)
            result = adapter.submit(req)
            if deny_start:
                assert result.state == 'held'
                assert sum(path.endswith('/containers/create') for path in posts) == 1
                assert not any('/containers/' in path and path.endswith('/start') for path in posts)
                before = list(posts)
                assert adapter.submit(req).state == 'held'
                assert posts == before
            else:
                assert result.state == 'succeeded'
                assert result.evidence.container_id == 'd' * 64
                assert journal.inspect_binding(op_start).target_id == 'd' * 64
                assert sum(path.endswith('/containers/create') for path in posts) == 1
                assert sum('/containers/' in path and path.endswith('/start') for path in posts) == 1
        finally:
            key.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('lost', ['submit', 'ack'])
async def test_concrete_controller_asgi_recovers_lost_response_without_effect_replay(rig, lost):
    from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
    from engineering_hub.container_pilot_client import ContainerPilotClient
    from engineering_hub.pilot_ownership import PilotOwner
    from engineering_hub.pilot_recovery import PilotRecovery
    from engineering_hub.repository import HubRepository
    from hub_container_executor.asgi import create_app
    class Driver:
        def implementation_commit(self): return rig.params['manifest'].driver.implementation_commit
    class LoseOnce(httpx.AsyncBaseTransport):
        def __init__(self, app): self.inner=httpx.ASGITransport(app=app); self.lost=False
        async def handle_async_request(self, request):
            response=await self.inner.handle_async_request(request)
            path=request.url.path
            selected=(lost=='submit' and path=='/v2/container-pilot/commands') or (lost=='ack' and path.endswith('/acknowledge'))
            if request.method=='POST' and selected and not self.lost:
                self.lost=True
                await response.aclose()
                raise httpx.ReadError('synthetic response loss after server completed',request=request)
            return response
        async def aclose(self): await self.inner.aclose()
    params=dict(rig.params,single_pilot_protocol=True,driver_identity_verifier=Driver())
    controller_dir=rig.paths['adapter'].parent / 'controller'; controller_dir.mkdir(mode=0o700)
    db=controller_dir/'hub.sqlite'
    with module.local_host_adapter_resources(**params) as adapter:
        posts=_attach_synthetic_engine(adapter,params)
        app=create_app(adapter,bearer_token='synthetic-only')
        transport=LoseOnce(app)
        client=ContainerPilotClient('http://127.0.0.1','synthetic-only',transport=transport,single_pilot_protocol=True)
        owner=PilotOwner(db,allow_ephemeral_test_storage=True)
        try:
            repo=HubRepository(db,execution_principal='executor:container-pilot',pilot_owner=owner)
            target=ContainerPilotTarget.from_manifest(adapter.manifest)
            workflow=ContainerPilotWorkflow(repo,client,[target],pilot_owner=owner,single_pilot_protocol=True)
            task=repo.create_task('human:1234','engineering-hub','Synthetic pilot','synthetic')
            task=repo.claim_task('codex',task['id'],task['revision'])
            commit=adapter.manifest.candidate.source_commit
            task=repo.set_candidate('codex',task['id'],task['revision'],commit)
            task=repo.review_task('claude',task['id'],task['revision'],commit,'approve')
            run=workflow.create_run('codex',task['id'],task['revision'],adapter.manifest.candidate.image_config_id,target.target_id)
            held=await workflow.advance(run['id'])
            assert held['status']=='held' and transport.lost
            assert sum(path.endswith('/containers/create') for path in posts)==1
            before_recovery = tuple(posts)
        finally:
            owner.close(); await client.aclose()
        owner=PilotOwner(db,allow_ephemeral_test_storage=True)
        client=ContainerPilotClient('http://127.0.0.1','synthetic-only',transport=httpx.ASGITransport(app=app),single_pilot_protocol=True)
        try:
            repo=HubRepository(db,execution_principal='executor:container-pilot',pilot_owner=owner)
            resumed=ContainerPilotWorkflow(repo,client,[target],pilot_owner=owner,single_pilot_protocol=True)
            recovered=await PilotRecovery(resumed).recover(run['id'])
            assert recovered['recovery']=='recovered' and recovered['run']['status']=='running'
            assert tuple(posts) == before_recovery  # no create/start/exec effect during recovery
            final=await resumed.advance(run['id'])
            assert final['status']=='rolled_back'
            assert sum(path.endswith('/containers/create') for path in posts)==2
        finally:
            owner.close(); await client.aclose()

@pytest.mark.parametrize('lose_stop', [False, True])
def test_bound_v3_rollback_stops_candidate_before_replacement(rig, monkeypatch, lose_stop):
    """Real local journals, fake Engine and synthetic verifier; no host assurance."""
    import time
    from uuid import UUID, uuid5
    from engineering_hub.container_pilot_client import single_pilot_outcome_digest
    from hub_container_executor.adapter import TrustedEffectContextGuard
    from hub_container_executor.app_cgroup_observation import AppCgroupObserver, AppCgroupPins, AppCgroupObservation
    from hub_container_executor.effect_response_recorder import DurableEffectResponseRecorder
    from hub_container_executor.effect_mac_key import create_for_new_journal, EffectMacKey
    from hub_container_executor.enrolled_effect_provider import enrolled_effect_provider, DeferredTargetEffect
    from tests.test_hub_container_effect_binding_canonical import fixture
    from tests.test_hub_container_executor_adapter import command
    class Driver:
        def implementation_commit(self): return rig.params['manifest'].driver.implementation_commit
    journal_path=rig.params['journal_database']
    key_id=create_for_new_journal(journal_path.parent,journal_path.name)
    params=dict(rig.params,single_pilot_protocol=True,driver_identity_verifier=Driver())
    with module.local_host_adapter_resources(**params) as adapter:
        journal=adapter.backend.operation_journal; journal.enable_bindings(); journal.enable_response_records()
        key=EffectMacKey(journal_path.parent,journal_path.name,expected_key_id=key_id)
        try:
            observer=object.__new__(AppCgroupObserver)
            observer._pins=AppCgroupPins('test.slice',1,2,3,4)
            monkeypatch.setattr(AppCgroupObserver, 'verify', lambda self, cid, *, deadline:
                AppCgroupObservation(cid,int(time.time()*1000)+5000,min(deadline,time.monotonic()+5)))
            adapter.backend.app_cgroup_observer=observer
            adapter.backend.effect_response_recorder=DurableEffectResponseRecorder(journal,key)
            adapter._stop_failed_candidate_before_rollback=True
            posts=_attach_synthetic_engine(adapter,params,lose_stop=lose_stop)
            run=str(uuid4())
            deploy=command(adapter.manifest,'deploy_container',run_id=run)
            smoke=command(adapter.manifest,'smoke_container',run_id=run)
            rollback=command(adapter.manifest,'rollback_container',run_id=run)
            smoke_rollback=command(adapter.manifest,'smoke_container_rollback',run_id=run)
            effects=[]
            for req,role,probe_req in ((deploy,'candidate',smoke),(rollback,'rollback',smoke_rollback)):
                state_kind='state_copy' if role=='candidate' else 'rollback_state'
                generation=adapter.state_provisioner._generation(run,str(uuid5(UUID(req.command_id),state_kind)),role)
                image=adapter._verified_image(role)
                profile=adapter._profile(image,adapter._defaults_from_record(image),generation,adapter._resolve_secrets())
                create=replace(fixture(),operation_id=str(uuid5(UUID(req.command_id),'create_'+role)),run_id=run,
                    kind='create_'+role,fence=req.fencing_generation,owner_epoch=journal._owner.epoch,
                    resource_name=f'{adapter.manifest.runtime.resource_name_prefix}-{run[:12]}-{role}',
                    container_id=None,image_index_digest=image.runnable_reference,
                    platform_manifest_digest=image.platform_manifest_digest,image_config_id=image.image_config_id,
                    state_generation_id=generation,environment_names=tuple(n for n,_ in profile.environment),
                    memory_bytes=profile.memory_bytes,cpu_millicores=profile.cpu_millicores,
                    pids_limit=profile.pids_limit,tmpfs_bytes=profile.tmpfs[0][1])
                start=replace(create,operation_id=str(uuid5(UUID(req.command_id),'start_'+role)),kind='start',resource_name=None)
                probes=adapter.manifest.probes.candidate if role=='candidate' else adapter.manifest.probes.rollback
                probe=replace(create,operation_id=str(uuid5(UUID(probe_req.command_id),'probe')),kind='probe',
                    resource_name=None,probe_set_digest=adapter._probe_digest(probes))
                effects.extend((create,DeferredTargetEffect(start,req.command_id),DeferredTargetEffect(probe,req.command_id)))
                if role=='candidate':
                    stop=replace(create,operation_id=str(uuid5(UUID(rollback.command_id),'stop')),kind='stop',resource_name=None)
                    effects.append(DeferredTargetEffect(stop,req.command_id))
            adapter.backend.effect_binding_provider=enrolled_effect_provider(journal,key,tuple(effects),
                source_ledger=adapter.ledger,verify_generation=lambda actual, _: True)
            adapter.effect_context_guard=TrustedEffectContextGuard(lambda _: True,adapter.backend.effect_context_scope)
            for req,expected in ((deploy,'succeeded'),(smoke,'failed')):
                result=adapter.submit(req)
                assert result.state==expected, result
                adapter.acknowledge_single_pilot(req,single_pilot_outcome_digest(result))
            result=adapter.submit(rollback)
            if lose_stop:
                assert result.state=='held',result
                assert adapter.ledger.target_snapshot(rollback.target_id)['container_id']=='d'*64
                before=list(posts)
                assert adapter.submit(rollback).state=='held'
                assert posts==before
                assert sum(p.endswith('/containers/create') for p in posts)==1
            else:
                assert result.state=='succeeded',result
                stop_index=next(i for i,p in enumerate(posts) if p.endswith('/stop'))
                creates=[i for i,p in enumerate(posts) if p.endswith('/containers/create')]
                assert len(creates)==2 and creates[0]<stop_index<creates[1]
                assert '/'+('d'*64)+'/stop' in posts[stop_index]
                adapter.acknowledge_single_pilot(rollback,single_pilot_outcome_digest(result))
                assert adapter.submit(smoke_rollback).state=='succeeded'
        finally:
            key.close()
