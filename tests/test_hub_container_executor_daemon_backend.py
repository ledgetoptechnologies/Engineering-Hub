"""Fake-only Docker Engine transcripts for the concrete daemon backend."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from contextlib import closing

import httpx
import pytest

from hub_container_executor.backend import ImageDefaults, OperationObservation, RunnerObservation
from hub_container_executor.daemon_backend import (
    DaemonBackendError,
    DockerEngineBinding,
    DockerEngineBackend,
)
from hub_container_executor.build_record import verify_image_record
from tests.test_hub_container_executor_build_record import FakeStore, FakeTrust, make_record

INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"


def sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def verified_image(role="candidate", platform=None):
    manifest, record_raw, blobs = make_record(role, platform)
    selection = getattr(manifest, role)
    image = verify_image_record(
        manifest,
        role,
        FakeStore({selection.provenance.attestation_digest: record_raw}),
        FakeStore(blobs),
        FakeTrust(),
    )
    return manifest, image, blobs


def oci_archive(image, blobs):
    root_raw = blobs[image.runnable_reference]
    root_descriptor = {"mediaType": image.runnable_media_type,
                       "digest": image.runnable_reference, "size": len(root_raw)}
    entries = {
        "oci-layout": json_bytes({"imageLayoutVersion": "1.0.0"}),
        "index.json": json_bytes({"schemaVersion": 2, "manifests": [root_descriptor]}),
    }
    entries.update({"blobs/sha256/" + digest[7:]: raw for digest, raw in blobs.items()})
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, raw in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            info.mode = 0o600
            info.mtime = 0
            archive.addfile(info, io.BytesIO(raw))
    return stream.getvalue()


def docker_config(image):
    config = image.image_config
    image_defaults = config.get("config") or {}
    return {
        "Id": image.runnable_reference,
        "Os": image.platform.os,
        "Architecture": image.platform.architecture,
        "Variant": image.platform.variant,
        "Config": {
            "Entrypoint": image_defaults.get("Entrypoint"),
            "Cmd": image_defaults.get("Cmd"),
            "Env": image_defaults.get("Env"),
        },
    }


def binding(manifest, tmp_path, *, api_version="1.56"):
    return DockerEngineBinding(
        runner_id=manifest.runner_binding.runner_id,
        daemon_identity=manifest.runner_binding.daemon_identity,
        platform=manifest.runner_binding.platform,
        unix_socket_path="/operator/enrolled/docker.sock",
        api_version=api_version,
        generation_root=str(tmp_path / "generations"),
    )


def safe_generation(tmp_path, generation_id="fixture-g1"):
    root = tmp_path / "generations"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    source = root / generation_id
    source.mkdir(exist_ok=True, mode=0o700)
    source.chmod(0o700)
    return source


@pytest.mark.parametrize("api_version", ["1.41", "1.56", "1.100"])
def test_constructor_is_inert_and_runner_inspection_uses_only_explicit_api_binding(tmp_path, api_version):
    manifest, _image, _blobs = verified_image()
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "docker"
        assert request.url.path == f"/v{api_version}/info"
        return httpx.Response(200, json={
            "ID": manifest.runner_binding.daemon_identity,
            "OSType": "linux",
            "Architecture": "x86_64",
        })

    transport = httpx.MockTransport(handler)
    backend = DockerEngineBackend(binding(manifest, tmp_path, api_version=api_version), test_transport=transport)
    assert requests == []
    observed = backend.inspect_runner(1000)
    assert observed == RunnerObservation(
        manifest.runner_binding.runner_id,
        manifest.runner_binding.daemon_identity,
        manifest.runner_binding.platform,
    )
    assert backend.binding.api_version == api_version
    assert [(req.method, req.url.path) for req in requests] == [("GET", f"/v{api_version}/info")]
    backend.close()


@pytest.mark.parametrize("api_version", [
    "1.0", "1.9", "1.40", "1.041", "1.056", "01.56", "1.56.0", "v1.56",
    "1.56\n", "1.56 ", "1.-1", "2.56", "", None, 1.56,
])
def test_unsupported_or_noncanonical_api_binding_rejects_before_transport(tmp_path, api_version):
    manifest, _image, _blobs = verified_image()
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request))
    with pytest.raises(ValueError):
        DockerEngineBackend(binding(manifest, tmp_path, api_version=api_version), test_transport=transport)
    assert calls == []


def test_effective_profile_requires_explicit_cgroup_namespace_field():
    from dataclasses import MISSING, fields
    from hub_container_executor.backend import EffectiveProfile

    field = next(field for field in fields(EffectiveProfile) if field.name == "cgroupns_mode")
    assert field.default is MISSING and field.default_factory is MISSING


def test_runner_wrong_daemon_or_architecture_fails_closed(tmp_path):
    manifest, _image, _blobs = verified_image()

    for response_body in (
        {"ID": "foreign-daemon", "OSType": "linux", "Architecture": "x86_64"},
        {"ID": manifest.runner_binding.daemon_identity, "OSType": "windows", "Architecture": "x86_64"},
        {"ID": manifest.runner_binding.daemon_identity, "OSType": "linux", "Architecture": "aarch64"},
    ):
        backend = DockerEngineBackend(
            binding(manifest, tmp_path),
            test_transport=httpx.MockTransport(
                lambda request, body=response_body: httpx.Response(200, json=body)
            ),
        )
        with pytest.raises(DaemonBackendError):
            backend.inspect_runner(1000)
        backend.close()


def test_image_inspection_proves_original_oci_index_manifest_config_and_keeps_ids_distinct(tmp_path):
    manifest, image, blobs = verified_image()
    archive = oci_archive(image, blobs)
    requests = []

    def handler(request):
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        requests.append((request.method, path))
        if path.endswith("/images/" + image.runnable_reference.replace(":", "%3A") + "/json"):
            return httpx.Response(200, json=docker_config(image))
        if path.endswith("/images/" + image.runnable_reference.replace(":", "%3A") + "/get"):
            return httpx.Response(200, content=archive,
                                  headers={"Content-Type": "application/x-tar"})
        raise AssertionError("unexpected route")

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )
    defaults = backend.inspect_image(image, 3000)
    config = image.image_config["config"]
    assert defaults == ImageDefaults(
        image.image_config_id,
        image.platform,
        tuple(config["Entrypoint"]),
        tuple(config["Cmd"]),
        tuple(config["Env"]),
    )
    assert requests == [
        ("GET", "/v1.56/images/" + image.runnable_reference.replace(":", "%3A") + "/json"),
        ("GET", "/v1.56/images/" + image.runnable_reference.replace(":", "%3A") + "/get"),
    ]
    assert len({image.runnable_reference, image.platform_manifest_digest,
                image.image_config_id}) == 3
    assert not any(path.endswith(("/pull", "/build", "/exec")) for _, path in requests)
    backend.close()


def test_image_inspection_rejects_config_manifest_and_wrong_platform_in_archive(tmp_path):
    manifest, image, blobs = verified_image()
    archive = oci_archive(image, blobs)
    bodies = [
        b"not a tar archive",
        archive[:512],
        archive + b"x",
    ]

    for archive_body in bodies:
        def handler(request, body=archive_body):
            if request.url.path.endswith("/json"):
                return httpx.Response(200, json=docker_config(image))
            return httpx.Response(200, content=body)

        backend = DockerEngineBackend(
            binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
        )
        with pytest.raises(DaemonBackendError):
            backend.inspect_image(image, 3000)
        backend.close()


def test_classic_docker_save_archive_without_original_index_chain_is_unsupported(tmp_path):
    manifest, image, _blobs = verified_image()
    legacy_archive = io.BytesIO()
    with tarfile.open(fileobj=legacy_archive, mode="w") as archive:
        info = tarfile.TarInfo("manifest.json")
        payload = b"[]"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    def handler(request):
        if request.url.path.endswith("/json"):
            return httpx.Response(200, json=docker_config(image))
        return httpx.Response(200, content=legacy_archive.getvalue())

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )
    with pytest.raises(DaemonBackendError):
        backend.inspect_image(image, 3000)
    backend.close()


def test_wrong_manifest_config_and_platform_expectations_fail_archive_proof(tmp_path):
    from dataclasses import replace
    from engineering_hub.container_pilot_manifest import Platform

    manifest, image, blobs = verified_image()
    archive = oci_archive(image, blobs)
    wrong_images = [
        replace(image, platform_manifest_digest="sha256:" + "f" * 64),
        replace(image, image_config_id="sha256:" + "e" * 64),
        replace(image, platform=Platform(os="linux", architecture="arm64", variant="v8")),
    ]
    for wrong_image in wrong_images:
        def handler(request):
            if request.url.path.endswith("/json"):
                return httpx.Response(200, json=docker_config(image))
            return httpx.Response(200, content=archive)

        backend = DockerEngineBackend(
            binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
        )
        with pytest.raises(DaemonBackendError):
            backend.inspect_image(wrong_image, 3000)
        backend.close()


def test_daemon_inspect_id_outside_original_chain_fails_before_archive_read(tmp_path):
    manifest, image, _blobs = verified_image()
    archive_reads = []

    def handler(request):
        if request.url.path.endswith("/json"):
            return httpx.Response(200, json={**docker_config(image),
                                             "Id": "sha256:" + "f" * 64})
        archive_reads.append(request)
        return httpx.Response(200, content=b"unused")

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )
    with pytest.raises(DaemonBackendError):
        backend.inspect_image(image, 3000)
    assert archive_reads == []
    backend.close()


def make_profile(image, generation_id="fixture-g1", secrets=None):
    from hub_container_executor.backend import EffectiveProfile

    secrets = secrets or {"CODEX_TOKEN": "fake-codex", "HERMES_TOKEN": "fake-hermes",
                          "CLAUDE_TOKEN": "fake-claude"}
    defaults = image.image_config["config"]
    environment = dict(item.split("=", 1) for item in defaults.get("Env", []))
    environment.update({"HUB_BASE_URL": "http://127.0.0.1:8770",
                        "HUB_DB": "/var/lib/hub/hub.sqlite3"})
    environment.update(secrets)
    size = 64 * 1024**2
    return EffectiveProfile(
        uid=10001, gid=10001, supplementary_groups=(), read_only_root=True,
        cap_drop=("ALL",), no_new_privileges=True, privileged=False,
        pid_mode="private", ipc_mode="private", network_mode="none",
        cgroupns_mode="private",
        published_ports=(), memory_bytes=512 * 1024**2,
        cpu_millicores=1000, pids_limit=128,
        tmpfs=(("/tmp", size, 0o1777, True, True),),
        mounts=(("state_generation", "/var/lib/hub", "rw", generation_id),
                ("tmpfs", "/tmp", "rw", f"size={size}", "mode=1777", "noexec", "nosuid")),
        state_destination="/var/lib/hub", state_mode="rw",
        state_generation_id=generation_id,
        environment=tuple(sorted(environment.items())),
        entrypoint=tuple(defaults["Entrypoint"]), command=tuple(defaults["Cmd"]),
        restart_policy="no",
    )


def packaged_hub_defaults():
    """Synthetic metadata with the published Dockerfile's actual shape."""
    return {
        "User": "568:568",
        "Entrypoint": ["python", "-m", "engineering_hub", "--host", "0.0.0.0", "--port", "8770"],
        "Cmd": [],
        "Env": ["PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin", "PYTHON_VERSION=3.12.fixture",
                "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "HUB_UI_DIRECTORY=/app/dashboard",
                "HUB_DB=/var/lib/hub/hub.sqlite3"],
    }


def packaged_hub_profile(defaults):
    from dataclasses import replace
    _, image, _ = verified_image()
    # This unit exercises environment/profile admission, not image provenance;
    # the existing immutable archive transcript tests cover that separate gate.
    image = replace(image, image_config={**image.image_config, "config": defaults})
    return image, make_profile(image)


def test_packaged_hub_fixed_database_default_admitted_without_weakening_runtime_profile(tmp_path):
    from hub_container_executor.backend import ContainerPlan
    manifest, _, _ = verified_image()
    defaults = packaged_hub_defaults()
    image, profile = packaged_hub_profile(defaults)
    with closing(DockerEngineBackend(binding(manifest, tmp_path),
                 test_transport=httpx.MockTransport(lambda _: pytest.fail("Admission must not contact daemon")))) as backend:
        generation, environment = backend._validate_effective_profile(profile, {"Volumes": None}, defaults)
        assert generation == "fixture-g1" and environment == profile.environment
        assert dict(environment)["HUB_DB"] == "/var/lib/hub/hub.sqlite3"
        assert sum(key == "HUB_DB" for key, _ in environment) == 1
        body = backend._create_body(ContainerPlan("hub-pilot-fixture-01234567-89a-candidate", image,
                                     image.runnable_reference, False, profile), tmp_path / "fixture-g1", environment)
        assert defaults["User"] == "568:568" and body["User"] == "10001:10001"
        assert body["HostConfig"]["CgroupnsMode"] == "private"
        assert body["HostConfig"]["ReadonlyRootfs"] is True
        assert body["HostConfig"]["CapDrop"] == ["ALL"]
        assert body["Env"].count("HUB_DB=/var/lib/hub/hub.sqlite3") == 1


@pytest.mark.parametrize("entry", [
    "HUB_DB=", "HUB_DB=/tmp/hub.sqlite3", "HUB_DB=/var/lib/hub/other.sqlite3",
    "HUB_DB=/var/lib/hub/../hub/hub.sqlite3", "HUB_DB=/var/lib/hub/hub.sqlite3/",
    "HUB_DB=/var/lib/hub/hub.sqlite3\x00", "HUB_BASE_URL=http://127.0.0.1:8770",
    "CODEX_TOKEN=image-baked-token", "HERMES_TOKEN=image-baked-token", "CLAUDE_TOKEN=image-baked-token",
])
def test_packaged_image_other_reserved_or_wrong_database_defaults_denied_before_effect(entry):
    defaults = packaged_hub_defaults()
    defaults["Env"] = [value for value in defaults["Env"] if not value.startswith("HUB_DB=")]
    defaults["Env"].append(entry)
    _, profile = packaged_hub_profile(defaults)
    with pytest.raises(DaemonBackendError):
        DockerEngineBackend._profile_environment(profile, defaults)


@pytest.mark.parametrize("entries", [
    ["HUB_DB=/var/lib/hub/hub.sqlite3", "HUB_DB=/var/lib/hub/hub.sqlite3"],
    ["HUB_DB=/tmp/wrong", "HUB_DB=/var/lib/hub/hub.sqlite3"],
    ["HUB_DB"], [None], ["=missing-key"], ["PATH=/bin", "PATH=/bin"],
])
def test_packaged_image_duplicate_or_malformed_defaults_remain_denied(entries):
    defaults = packaged_hub_defaults()
    _, profile = packaged_hub_profile(defaults)
    defaults["Env"] = entries
    with pytest.raises(DaemonBackendError):
        DockerEngineBackend._profile_environment(profile, defaults)


@pytest.mark.parametrize("change", ["missing-db", "wrong-db", "empty-db", "extra", "duplicate-db", "missing-secret"])
def test_packaged_image_final_environment_stays_exactly_closed(change):
    from dataclasses import replace
    defaults = packaged_hub_defaults()
    _, profile = packaged_hub_profile(defaults)
    values = list(profile.environment)
    if change == "duplicate-db":
        values.append(("HUB_DB", "/var/lib/hub/hub.sqlite3"))
    elif change == "extra":
        values.append(("UNAPPROVED_EXTRA", "x"))
    else:
        key = "CLAUDE_TOKEN" if change == "missing-secret" else "HUB_DB"
        values = [(name, value) for name, value in values if name != key]
        if change in {"wrong-db", "empty-db"}:
            values.append((key, "/tmp/not-the-database" if change == "wrong-db" else ""))
    with pytest.raises(DaemonBackendError):
        DockerEngineBackend._profile_environment(replace(profile, environment=tuple(values)), defaults)


def container_inspect(image, profile, source, container_id="c" * 64, status="created"):
    size = profile.tmpfs[0][1]
    return {
        "Id": container_id,
        # Docker 29/containerd may report the selected platform manifest here.
        "Image": image.platform_manifest_digest,
        "Config": {
            "Image": image.runnable_reference,
            "User": "10001:10001",
            "Env": [f"{key}={value}" for key, value in profile.environment],
            "Entrypoint": list(profile.entrypoint),
            "Cmd": list(profile.command),
            "Volumes": None,
        },
        "HostConfig": {
            "ReadonlyRootfs": True, "CapDrop": ["ALL"], "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"], "Privileged": False,
            "PidMode": "", "IpcMode": "", "NetworkMode": "none",
            "CgroupnsMode": profile.cgroupns_mode,
            "PortBindings": {}, "Memory": profile.memory_bytes,
            "MemorySwap": profile.memory_bytes,
            "NanoCpus": profile.cpu_millicores * 1_000_000,
            "PidsLimit": profile.pids_limit,
            "Tmpfs": {"/tmp": f"rw,noexec,nosuid,size={size},mode=1777"},
            "Mounts": [{"Type": "bind", "Source": str(source),
                        "Target": "/var/lib/hub", "ReadOnly": False}],
            "RestartPolicy": {"Name": "no"}, "AutoRemove": False,
            "GroupAdd": [], "Binds": [], "Devices": [], "DeviceRequests": [],
            "DeviceCgroupRules": [], "ExtraHosts": [], "Links": [],
            "VolumesFrom": [],
        },
        "Mounts": [
            {"Type": "bind", "Source": str(source), "Destination": "/var/lib/hub",
             "Mode": "rw", "RW": True, "Propagation": "rprivate"},
            {"Type": "tmpfs", "Source": "", "Destination": "/tmp",
             "Mode": "", "RW": True,
             "Propagation": ""},
        ],
        "State": {"Status": status},
        "NetworkSettings": {"Ports": None},
    }


def _exercise_create_closed_profile(tmp_path):
    from hub_container_executor.backend import ContainerPlan

    manifest, image, blobs = verified_image()
    generation = safe_generation(tmp_path)
    profile = make_profile(image)
    plan = ContainerPlan("hub-pilot-fixture-01234567-89a-candidate", image,
                         image.runnable_reference, False, profile)
    archive = oci_archive(image, blobs)
    requests = []
    observed = container_inspect(image, profile, generation)

    def handler(request):
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        requests.append((request.method, path, request.content))
        if path.endswith("/info"):
            return httpx.Response(200, json={"ID": manifest.runner_binding.daemon_identity,
                                             "OSType": "linux", "Architecture": "amd64"})
        if "/images/" in path and path.endswith("/json"):
            return httpx.Response(200, json=docker_config(image))
        if "/images/" in path and path.endswith("/get"):
            return httpx.Response(200, content=archive)
        if path.endswith("/containers/hub-pilot-fixture-01234567-89a-candidate/json"):
            return httpx.Response(404, json={"message": "not found"})
        if path.endswith("/containers/create"):
            return httpx.Response(201, json={"Id": "c" * 64, "Warnings": []})
        if path.endswith("/containers/" + "c" * 64 + "/json"):
            return httpx.Response(200, json=observed)
        raise AssertionError(f"unexpected fake route: {request.method} {path}")

    with closing(DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )) as backend:
        result = backend.create_container(plan, "4c96d3f7-19c5-5ff4-a29e-302f6f661009", 5000)
        assert result == "c" * 64
        create = next(row for row in requests if row[0] == "POST" and row[1].endswith("/containers/create"))
        body = json.loads(create[2])
        assert body["Image"] == image.runnable_reference and body["Image"] != image.image_config_id
        assert "Entrypoint" not in body and "Cmd" not in body
        assert body["User"] == "10001:10001"
        assert body["Env"] == [f"{key}={value}" for key, value in profile.environment]
        host = body["HostConfig"]
        assert host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
        assert host["SecurityOpt"] == ["no-new-privileges:true"]
        assert host["CgroupnsMode"] == "private"
        assert host["NetworkMode"] == "none" and host["PortBindings"] == {}
        assert host["RestartPolicy"] == {"Name": "no"}
        assert host["Memory"] == profile.memory_bytes
        assert host["NanoCpus"] == profile.cpu_millicores * 1_000_000
        assert host["PidsLimit"] == profile.pids_limit
        assert host["Mounts"] == [{"Type": "bind", "Source": str(generation),
                                   "Target": "/var/lib/hub", "ReadOnly": False}]
        assert host["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=67108864,mode=1777"}
        assert not any("/pull" in path or "/build" in path or "/exec" in path
                       for _, path, _ in requests)
        assert not any(method == "POST" and "/images/" in path for method, path, _ in requests)


def test_create_uses_only_immutable_digest_and_exact_closed_profile(tmp_path):
    _exercise_create_closed_profile(tmp_path)


@pytest.mark.parametrize("mode", ["host", "", None, "missing"])
def test_effective_cgroup_namespace_readback_must_be_explicit_private(tmp_path, monkeypatch, mode):
    original = container_inspect

    def changed_inspection(*args, **kwargs):
        observed = original(*args, **kwargs)
        if mode == "missing":
            observed["HostConfig"].pop("CgroupnsMode")
        else:
            observed["HostConfig"]["CgroupnsMode"] = mode
        return observed

    monkeypatch.setattr(__import__(__name__, fromlist=["container_inspect"]),
                        "container_inspect", changed_inspection)
    with pytest.raises(DaemonBackendError, match="host security profile differs"):
        _exercise_create_closed_profile(tmp_path)


@pytest.mark.parametrize("mode", ["host", "", None])
def test_unsafe_planned_cgroup_namespace_rejected_before_any_daemon_call(tmp_path, mode):
    from dataclasses import replace
    from hub_container_executor.backend import ContainerPlan

    manifest, image, _blobs = verified_image()
    profile = replace(make_profile(image), cgroupns_mode=mode)
    plan = ContainerPlan("hub-pilot-fixture-01234567-89a-candidate", image,
                         image.runnable_reference, False, profile)
    calls = []
    backend = DockerEngineBackend(binding(manifest, tmp_path),
                                  test_transport=httpx.MockTransport(lambda request: calls.append(request)))
    try:
        with pytest.raises(DaemonBackendError, match="closed pilot policy"):
            backend.create_container(plan, "4c96d3f7-19c5-5ff4-a29e-302f6f661009", 5000)
        assert calls == []
    finally:
        backend.close()


def test_create_rejects_profile_mismatch_and_symlink_generation_before_post(tmp_path):
    from dataclasses import replace
    from hub_container_executor.backend import ContainerPlan

    manifest, image, blobs = verified_image()
    generations = tmp_path / "generations"
    generations.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (generations / "fixture-g1").symlink_to(outside, target_is_directory=True)
    archive = oci_archive(image, blobs)
    posts = []

    def handler(request):
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        if request.method == "POST":
            posts.append(path)
        if path.endswith("/info"):
            return httpx.Response(200, json={"ID": manifest.runner_binding.daemon_identity,
                                             "OSType": "linux", "Architecture": "amd64"})
        if "/images/" in path and path.endswith("/json"):
            return httpx.Response(200, json=docker_config(image))
        if "/images/" in path and path.endswith("/get"):
            return httpx.Response(200, content=archive)
        raise AssertionError("preflight must fail before a mutation request")

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )
    profile = make_profile(image)
    plan = ContainerPlan("hub-pilot-fixture-01234567-89a-candidate", image,
                         image.runnable_reference, False,
                         replace(profile, network_mode="bridge"))
    with pytest.raises(DaemonBackendError):
        backend.create_container(plan, "4c96d3f7-19c5-5ff4-a29e-302f6f661009", 5000)
    assert posts == []

    unsafe_plan = ContainerPlan("hub-pilot-fixture-01234567-89a-candidate", image,
                                image.runnable_reference, False, profile)
    with pytest.raises(DaemonBackendError):
        backend.create_container(unsafe_plan, "4c96d3f7-19c5-5ff4-a29e-302f6f66100a", 5000)
    assert posts == []

    bad_id = replace(profile, state_generation_id="../outside")
    bad_id_plan = ContainerPlan("hub-pilot-fixture-01234567-89a-candidate", image,
                                image.runnable_reference, False, bad_id)
    with pytest.raises(DaemonBackendError):
        backend.create_container(bad_id_plan, "4c96d3f7-19c5-5ff4-a29e-302f6f66100b", 5000)
    assert posts == []

    safe_generation(tmp_path, "fixture-g2")
    second_profile = make_profile(image, "fixture-g2")
    extra_mounts = replace(second_profile, mounts=second_profile.mounts + (
        ("state_generation", "/var/run/docker.sock", "rw", "fixture-g2"),
    ))
    extra_plan = ContainerPlan("hub-pilot-fixture-01234567-89a-candidate", image,
                               image.runnable_reference, False, extra_mounts)
    with pytest.raises(DaemonBackendError):
        backend.create_container(extra_plan, "4c96d3f7-19c5-5ff4-a29e-302f6f66100c", 5000)
    assert posts == []
    backend.close()


def probe_fixture(tmp_path, *, first_report=None, lose_start=False, role="candidate"):
    manifest, image, blobs = verified_image(role)
    generation = safe_generation(tmp_path)
    profile = make_profile(image)
    archive = oci_archive(image, blobs)
    container_id = "c" * 64
    inspection = container_inspect(image, profile, generation,
                                   container_id=container_id, status="running")
    requests = []
    exec_number = 0

    def handler(request):
        nonlocal exec_number
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        requests.append((request.method, path, request.content))
        if path.endswith("/info"):
            return httpx.Response(200, json={"ID": manifest.runner_binding.daemon_identity,
                                             "OSType": "linux", "Architecture": "amd64"})
        if "/images/" in path and path.endswith("/json"):
            return httpx.Response(200, json=docker_config(image))
        if "/images/" in path and path.endswith("/get"):
            return httpx.Response(200, content=archive)
        if path.endswith("/containers/" + container_id + "/json"):
            return httpx.Response(200, json=inspection)
        if path.endswith("/containers/" + container_id + "/exec"):
            exec_number += 1
            return httpx.Response(201, json={"Id": f"{exec_number:064x}"})
        if path.endswith("/exec/" + f"{exec_number:064x}" + "/start"):
            if lose_start:
                raise httpx.ReadTimeout("synthetic response loss")
            if first_report is not None and exec_number == 1:
                report = first_report
            else:
                probe = manifest.probes.candidate[exec_number - 1]
                report = {"expectation_id": probe.expectation_id,
                          "ok": True, "status": probe.expected_status}
            raw = json_bytes(report)
            frame = bytes([1, 0, 0, 0]) + len(raw).to_bytes(4, "big") + raw
            return httpx.Response(200, content=frame)
        if path.endswith("/exec/" + f"{exec_number:064x}" + "/json"):
            exit_code = 1 if first_report is not None and exec_number == 1 else 0
            return httpx.Response(200, json={"ID": f"{exec_number:064x}",
                                             "Running": False, "ExitCode": exit_code})
        raise AssertionError(f"unexpected fake route: {request.method} {path}")

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )
    backend.inspect_image(image, 5000)
    observed = backend.inspect_container(container_id, 3000)
    assert observed is not None and observed.status == "running"
    return backend, manifest, image, requests


def test_compiled_probes_use_fixed_exec_argv_and_return_health_evidence(tmp_path):
    backend, manifest, image, requests = probe_fixture(tmp_path)
    assert not any(path.endswith("/exec") or "/exec/" in path
                   for _, path, _ in requests)
    probes = manifest.probes.candidate
    outcome = backend.run_compiled_probes(
        "c" * 64, probes, "4c96d3f7-19c5-5ff4-a29e-302f6f661009",
        tuple(probe.timeout_ms for probe in probes),
    )
    assert outcome.container_id == "c" * 64
    assert outcome.image_config_id == image.image_config_id
    assert outcome.health == "healthy" and outcome.evidence_verified is True
    profile_raw = json_bytes([probe.model_dump(mode="json") for probe in probes])
    assert outcome.profile_digest == sha(profile_raw)
    create_calls = [json.loads(body) for method, path, body in requests
                    if method == "POST" and path.endswith("/exec")]
    assert len(create_calls) == 3
    first_script = None
    for probe, call in zip(probes, create_calls, strict=True):
        assert call == {
            "AttachStdin": False, "AttachStdout": True, "AttachStderr": False,
            "Tty": False, "User": "10001:10001",
            "Cmd": ["/usr/local/bin/python3", "-I", "-c", call["Cmd"][3],
                    probe.kind, probe.path, str(probe.expected_status), probe.expectation_id,
                    str(probe.max_response_bytes), str(probe.timeout_ms), image.source_commit],
        }
        assert isinstance(call["Cmd"][3], str)
        first_script = first_script or call["Cmd"][3]
        assert call["Cmd"][3] == first_script
        compile(first_script, "<compiled-probe-fixture>", "exec")
        assert "127.0.0.1:8770" in first_script and "ProxyHandler({})" in first_script
        assert "subprocess" not in first_script and "shell=True" not in first_script
    assert not any("/pull" in path or "/build" in path
                   for _, path, _ in requests)
    backend.close()


def test_rollback_probe_evidence_uses_rollback_config_identity(tmp_path):
    backend, manifest, image, requests = probe_fixture(tmp_path, role="rollback")
    assert backend._container_images == {}  # Existing baseline was never created by this backend.
    probes = manifest.probes.rollback
    outcome = backend.run_compiled_probes(
        "c" * 64, probes, "4c96d3f7-19c5-5ff4-a29e-302f6f661009",
        tuple(probe.timeout_ms for probe in probes),
    )
    assert outcome.image_config_id == manifest.rollback.image_config_id == image.image_config_id
    assert outcome.health == "healthy" and outcome.evidence_verified is True
    assert image.source_commit == manifest.rollback.source_commit != manifest.candidate.source_commit
    calls = [json.loads(body) for method, path, body in requests if method == "POST" and path.endswith("/exec")]
    assert len(calls) == 3 and all(call["Cmd"][-1] == manifest.rollback.source_commit for call in calls)
    backend.close()


@pytest.mark.parametrize("variant", ["missing", "runnable", "manifest", "config", "platform",
                                     "uppercase-commit", "missing-commit", "create-binding"])
def test_probe_missing_or_mismatched_verified_source_cache_denied_before_journal_or_exec(tmp_path, monkeypatch, variant):
    from dataclasses import replace
    from engineering_hub.container_pilot_manifest import Platform
    backend, manifest, image, requests = probe_fixture(tmp_path)
    try:
        observed = backend.inspect_container("c" * 64, 3000)
        selected = observed.selected_image
        if variant in {"runnable", "manifest", "config", "platform"}:
            changes = {"runnable_reference": "sha256:" + "d" * 64} if variant == "runnable" else (
                {"platform_manifest_digest": "sha256:" + "d" * 64} if variant == "manifest" else (
                {"image_config_id": "sha256:" + "d" * 64} if variant == "config" else
                {"platform": Platform(os="linux", architecture="arm64", variant="v8")}))
            selected = replace(selected, **changes)
        monkeypatch.setattr(backend, "_known_container", lambda *args: replace(observed, selected_image=selected))
        if variant == "missing":
            backend._images.pop(image.image_config_id)
        elif variant in {"uppercase-commit", "missing-commit"}:
            entry = backend._images[image.image_config_id]
            backend._images[image.image_config_id] = (
                replace(image, source_commit="A" * 40 if variant == "uppercase-commit" else None), *entry[1:])
        elif variant == "create-binding":
            backend._container_images["c" * 64] = replace(image, source_commit="d" * 40)
        effects = []
        monkeypatch.setattr(backend, "_journal_begin", lambda *args: effects.append(args))
        before = len(requests)
        with pytest.raises(DaemonBackendError, match="verified source-image binding"):
            backend.run_compiled_probes("c" * 64, manifest.probes.candidate,
                "4c96d3f7-19c5-5ff4-a29e-302f6f661009", tuple(p.timeout_ms for p in manifest.probes.candidate))
        assert not effects and len(requests) == before
    finally:
        backend.close()


def test_compiled_probe_failure_is_verified_unhealthy_and_timeout_stays_unknown(tmp_path):
    backend, manifest, _image, requests = probe_fixture(
        tmp_path, first_report={"expectation_id": "hub_health_v1", "ok": False, "status": 503}
    )
    probes = manifest.probes.candidate
    outcome = backend.run_compiled_probes(
        "c" * 64, probes, "4c96d3f7-19c5-5ff4-a29e-302f6f661009",
        tuple(probe.timeout_ms for probe in probes),
    )
    assert outcome.health == "unhealthy" and outcome.evidence_verified is True
    assert sum(path.endswith("/exec") for _, path, _ in requests) == 3
    backend.close()

    timeout_root = tmp_path / "timeout"
    timeout_root.mkdir()
    backend, manifest, _image, requests = probe_fixture(timeout_root, lose_start=True)
    with pytest.raises(DaemonBackendError):
        backend.run_compiled_probes(
            "c" * 64, manifest.probes.candidate,
            "4c96d3f7-19c5-5ff4-a29e-302f6f661009",
            tuple(probe.timeout_ms for probe in manifest.probes.candidate),
        )
    assert sum(path.endswith("/exec") for _, path, _ in requests) == 1
    assert sum(path.endswith("/start") for _, path, _ in requests) == 1
    backend.close()


def test_inspect_operation_without_trusted_private_journal_is_unknown_and_read_only(tmp_path):
    manifest, _image, _blobs = verified_image()
    requests = []
    backend = DockerEngineBackend(
        binding(manifest, tmp_path),
        test_transport=httpx.MockTransport(lambda request: requests.append(request)
                                          or httpx.Response(500)),
    )
    operation_id = "4c96d3f7-19c5-5ff4-a29e-302f6f661009"
    observed = backend.inspect_operation(operation_id, 1000)
    assert observed.state == "unknown" and observed.terminal is False
    assert observed.container_id is None and requests == []
    backend.close()


def create_transcript(tmp_path, *, occupied=False, lose_response=False, bad_profile=False,
                      operation_journal=None):
    from hub_container_executor.backend import ContainerPlan

    manifest, image, blobs = verified_image()
    generation = safe_generation(tmp_path)
    profile = make_profile(image)
    name = "hub-pilot-fixture-01234567-89a-candidate"
    plan = ContainerPlan(name, image, image.runnable_reference, False, profile)
    archive = oci_archive(image, blobs)
    requests = []
    actual = container_inspect(image, profile, generation)
    if bad_profile:
        actual["HostConfig"]["NetworkMode"] = "bridge"

    def handler(request):
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        requests.append((request.method, path))
        if path.endswith("/info"):
            return httpx.Response(200, json={"ID": manifest.runner_binding.daemon_identity,
                                             "OSType": "linux", "Architecture": "amd64"})
        if "/images/" in path and path.endswith("/json"):
            return httpx.Response(200, json=docker_config(image))
        if "/images/" in path and path.endswith("/get"):
            return httpx.Response(200, content=archive)
        if path.endswith("/containers/" + name + "/json"):
            return httpx.Response(200 if occupied else 404, json={"Id": "e" * 64})
        if path.endswith("/containers/create"):
            if lose_response:
                raise httpx.ReadTimeout("synthetic create response loss")
            return httpx.Response(201, json={"Id": "c" * 64, "Warnings": []})
        if path.endswith("/containers/" + "c" * 64 + "/json"):
            return httpx.Response(200, json=actual)
        raise AssertionError(f"unexpected fake route: {request.method} {path}")

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), operation_journal=operation_journal,
        test_transport=httpx.MockTransport(handler)
    )
    return backend, manifest, image, plan, requests


def test_ambiguous_existing_name_and_lost_create_response_are_never_retried(tmp_path):
    operation_id = "4c96d3f7-19c5-5ff4-a29e-302f6f661009"
    backend, _manifest, _image, plan, requests = create_transcript(tmp_path, occupied=True)
    with pytest.raises(DaemonBackendError):
        backend.create_container(plan, operation_id, 5000)
    assert not any(method == "POST" and path.endswith("/containers/create")
                   for method, path in requests)
    backend.close()

    root = tmp_path / "lost"
    root.mkdir()
    backend, _manifest, _image, plan, requests = create_transcript(root, lose_response=True)
    with pytest.raises(DaemonBackendError) as failure:
        backend.create_container(plan, operation_id, 5000)
    assert "fake" not in str(failure.value) and "response loss" not in str(failure.value)
    assert sum(method == "POST" and path.endswith("/containers/create")
               for method, path in requests) == 1
    assert backend.inspect_operation(operation_id, 1000).state == "unknown"
    backend.close()


def test_actual_profile_mismatch_after_create_is_unknown_and_never_started(tmp_path):
    backend, _manifest, _image, plan, requests = create_transcript(tmp_path, bad_profile=True)
    with pytest.raises(DaemonBackendError):
        backend.create_container(plan, "4c96d3f7-19c5-5ff4-a29e-302f6f661009", 5000)
    assert sum(method == "POST" and path.endswith("/containers/create")
               for method, path in requests) == 1
    assert not any(path.endswith("/start") or path.endswith("/exec")
                   for _, path in requests)
    backend.close()


def test_oversized_archive_rejected_before_metadata_is_used(tmp_path, monkeypatch):
    import hub_container_executor.daemon_backend as daemon_module

    manifest, image, blobs = verified_image()
    archive = oci_archive(image, blobs)
    monkeypatch.setattr(daemon_module, "_MAX_ARCHIVE", len(archive) - 1)

    def handler(request):
        if request.url.path.endswith("/json"):
            return httpx.Response(200, json=docker_config(image))
        return httpx.Response(200, content=archive)

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )
    with pytest.raises(DaemonBackendError):
        backend.inspect_image(image, 3000)
    backend.close()


class FakePrivateJournal:
    """Explicit unit transcript only; not a deployed durable trust provider."""

    def __init__(self):
        self.rows = {}
        self.events = []

    def begin(self, operation_id, kind):
        self.events.append(("begin", operation_id, kind))
        self.rows[operation_id] = OperationObservation(operation_id, "pending", False)

    def completed(self, operation_id, kind, container_id=None):
        self.events.append(("completed", operation_id, kind, container_id))
        self.rows[operation_id] = OperationObservation(
            operation_id, "completed", True, container_id
        )

    def inspect(self, operation_id):
        return self.rows.get(operation_id)


def test_explicit_private_journal_records_backend_ack_and_inspection_never_uses_labels(tmp_path):
    journal = FakePrivateJournal()
    backend, _manifest, _image, plan, requests = create_transcript(
        tmp_path, operation_journal=journal
    )
    operation_id = "4c96d3f7-19c5-5ff4-a29e-302f6f661009"
    container_id = backend.create_container(plan, operation_id, 5000)
    assert container_id == "c" * 64
    assert journal.events == [
        ("begin", operation_id, "create_candidate"),
        ("completed", operation_id, "create_candidate", container_id),
    ]
    request_count = len(requests)
    observed = backend.inspect_operation(operation_id, 1000)
    assert observed == OperationObservation(operation_id, "completed", True, container_id)
    assert len(requests) == request_count
    assert "fake-codex" not in repr(journal.events)
    backend.close()


def test_start_and_stop_are_fixed_lifecycle_posts_with_actual_state_checks(tmp_path):
    manifest, image, blobs = verified_image()
    generation = safe_generation(tmp_path)
    profile = make_profile(image)
    container_id = "c" * 64
    current = {"status": "created"}
    requests = []
    archive = oci_archive(image, blobs)

    def handler(request):
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        requests.append((request.method, path, request.url.query.decode("ascii")))
        if path.endswith("/info"):
            return httpx.Response(200, json={"ID": manifest.runner_binding.daemon_identity,
                                             "OSType": "linux", "Architecture": "amd64"})
        if "/images/" in path and path.endswith("/json"):
            return httpx.Response(200, json=docker_config(image))
        if "/images/" in path and path.endswith("/get"):
            return httpx.Response(200, content=archive)
        if path.endswith("/containers/" + container_id + "/json"):
            return httpx.Response(200, json=container_inspect(
                image, profile, generation, container_id, current["status"]))
        if path.endswith("/containers/" + container_id + "/start"):
            current["status"] = "running"
            return httpx.Response(204)
        if path.endswith("/containers/" + container_id + "/stop"):
            current["status"] = "exited"
            return httpx.Response(204)
        raise AssertionError(f"unexpected fake route {request.method} {path}")

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )
    backend.inspect_image(image, 5000)
    initial = backend.inspect_container(container_id, 3000)
    assert initial is not None and initial.status == "created"
    backend.start_container(container_id, "4c96d3f7-19c5-5ff4-a29e-302f6f661009", 5000)
    backend.stop_container(container_id, "edfa4681-3a0a-5e5f-a670-2d2a91d7b70d", 5000)
    assert current["status"] == "exited"
    posts = [(method, path, query) for method, path, query in requests if method == "POST"]
    assert posts == [
        ("POST", "/v1.56/containers/" + container_id + "/start", ""),
        ("POST", "/v1.56/containers/" + container_id + "/stop", "t=5"),
    ]
    assert sum(path.endswith("/info") for _, path, _ in requests) == 4
    assert not any("/exec" in path or "/pull" in path or "/build" in path
                   for _, path, _ in requests)
    backend.close()


def test_start_refuses_effect_when_daemon_identity_changes_after_inspect(tmp_path):
    manifest, image, blobs = verified_image()
    generation = safe_generation(tmp_path)
    profile = make_profile(image)
    container_id = "c" * 64
    requests = []
    archive = oci_archive(image, blobs)

    def handler(request):
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        requests.append((request.method, path))
        if path.endswith("/info"):
            return httpx.Response(200, json={"ID": "foreign-daemon",
                                             "OSType": "linux", "Architecture": "amd64"})
        if "/images/" in path and path.endswith("/json"):
            return httpx.Response(200, json=docker_config(image))
        if "/images/" in path and path.endswith("/get"):
            return httpx.Response(200, content=archive)
        if path.endswith("/containers/" + container_id + "/json"):
            return httpx.Response(200, json=container_inspect(
                image, profile, generation, container_id, "created"))
        raise AssertionError(f"unexpected fake route {request.method} {path}")

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )
    backend.inspect_image(image, 5000)
    with pytest.raises(DaemonBackendError):
        backend.start_container(container_id, "4c96d3f7-19c5-5ff4-a29e-302f6f661009", 5000)
    assert not any(method == "POST" and path.endswith("/start")
                   for method, path in requests)
    backend.close()


def test_arm64_index_only_container_id_is_unsupported_without_platform_manifest_proof(tmp_path):
    manifest, image, blobs = verified_image(
        platform={"os": "linux", "architecture": "arm64", "variant": "v8"}
    )
    generation = safe_generation(tmp_path)
    profile = make_profile(image)
    inspection = container_inspect(image, profile, generation)
    inspection["Image"] = image.runnable_reference
    archive = oci_archive(image, blobs)

    def handler(request):
        if request.url.path.endswith("/json") and "/images/" in request.url.path:
            return httpx.Response(200, json=docker_config(image))
        if request.url.path.endswith("/get"):
            return httpx.Response(200, content=archive)
        return httpx.Response(200, json=inspection)

    backend = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler)
    )
    backend.inspect_image(image, 5000)
    with pytest.raises(DaemonBackendError):
        backend.inspect_container("c" * 64, 3000)
    backend.close()


def test_typed_adapter_flow_uses_fake_daemon_for_failed_smoke_and_rollback(tmp_path):
    from tests.test_hub_container_executor_adapter import command, make_fixture

    adapter, _placeholder_backend, _state, manifest = make_fixture(tmp_path)
    images = {role: adapter._verified_image(role) for role in ("candidate", "rollback")}
    image_by_ref = {image.runnable_reference: image for image in images.values()}
    blobs_by_id = dict(adapter.metadata_store.values)
    secrets = dict(adapter.secret_resolver.values)
    root = tmp_path / "generations"
    baseline_path = safe_generation(tmp_path, "baseline-g1")
    safe_generation(tmp_path, "candidate-g1")
    safe_generation(tmp_path, "rollback-g1")
    rollback = images["rollback"]
    rollback_defaults = adapter._defaults_from_record(rollback)
    baseline_profile = adapter._profile(rollback, rollback_defaults, "baseline-g1", secrets)
    containers = {
        "e" * 64: container_inspect(rollback, baseline_profile, baseline_path,
                                    "e" * 64, "running"),
    }
    executions = {}
    requests = []
    exec_counter = 0

    def engine_handler(request):
        nonlocal exec_counter
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        requests.append((request.method, path, request.content))
        if path.endswith("/info"):
            return httpx.Response(200, json={
                "ID": manifest.runner_binding.daemon_identity,
                "OSType": "linux", "Architecture": "amd64",
            })
        if "/images/" in path and path.endswith("/json"):
            selected = next(image for reference, image in image_by_ref.items()
                            if reference.replace(":", "%3A") in path)
            return httpx.Response(200, json=docker_config(selected))
        if "/images/" in path and path.endswith("/get"):
            selected = next(image for reference, image in image_by_ref.items()
                            if reference.replace(":", "%3A") in path)
            return httpx.Response(200, content=oci_archive(selected, blobs_by_id))
        if "/containers/" in path and path.endswith("/json"):
            container_id = path.split("/containers/", 1)[1].removesuffix("/json")
            name = container_id
            if name in containers:
                return httpx.Response(200, json=containers[name])
            return httpx.Response(404, json={"message": "absent"})
        if path.endswith("/containers/create"):
            body = json.loads(request.content)
            selected = image_by_ref[body["Image"]]
            container_id = "c" * 64 if selected.role == "candidate" else "d" * 64
            source = body["HostConfig"]["Mounts"][0]["Source"]
            generation_id = source.rsplit("/", 1)[1]
            defaults = adapter._defaults_from_record(selected)
            profile = adapter._profile(selected, defaults, generation_id, secrets)
            containers[container_id] = container_inspect(
                selected, profile, source, container_id, "created"
            )
            return httpx.Response(201, json={"Id": container_id, "Warnings": []})
        if path.endswith("/start") and "/containers/" in path:
            container_id = path.split("/containers/", 1)[1].removesuffix("/start")
            containers[container_id]["State"]["Status"] = "running"
            return httpx.Response(204)
        if path.endswith("/exec") and "/containers/" in path:
            container_id = path.split("/containers/", 1)[1].removesuffix("/exec")
            exec_counter += 1
            exec_id = f"{exec_counter:064x}"
            argv = json.loads(request.content)["Cmd"]
            kind, expected, expectation = argv[4], int(argv[6]), argv[7]
            is_candidate_health_failure = container_id == "c" * 64 and kind == "health_json"
            executions[exec_id] = {
                "id": exec_id,
                "report": {"expectation_id": expectation,
                           "ok": not is_candidate_health_failure,
                           "status": 503 if is_candidate_health_failure else expected},
                "exit": 1 if is_candidate_health_failure else 0,
            }
            return httpx.Response(201, json={"Id": exec_id})
        if path.endswith("/start") and "/exec/" in path:
            exec_id = path.split("/exec/", 1)[1].removesuffix("/start")
            raw = json_bytes(executions[exec_id]["report"])
            return httpx.Response(200, content=bytes([1, 0, 0, 0]) + len(raw).to_bytes(4, "big") + raw)
        if path.endswith("/json") and "/exec/" in path:
            exec_id = path.split("/exec/", 1)[1].removesuffix("/json")
            item = executions[exec_id]
            return httpx.Response(200, json={"ID": exec_id, "Running": False,
                                             "ExitCode": item["exit"]})
        raise AssertionError(f"unexpected fake engine route {request.method} {path}")

    driver = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(engine_handler)
    )
    adapter.backend = driver
    run_id = "53dd7fca-70f4-4e6a-abec-74e5ea0af719"
    try:
        deployed = adapter.submit(command(manifest, "deploy_container", run_id=run_id))
        assert deployed.state == "succeeded" and deployed.evidence.health == "unknown"
        failed_smoke = adapter.submit(command(manifest, "smoke_container", run_id=run_id))
        assert failed_smoke.state == "failed" and failed_smoke.reason == "health_failed"
        rolled_back = adapter.submit(command(manifest, "rollback_container", run_id=run_id))
        assert rolled_back.state == "succeeded"
        assert rolled_back.evidence.image_config_id == manifest.rollback.image_config_id
        verified_rollback = adapter.submit(
            command(manifest, "smoke_container_rollback", run_id=run_id)
        )
        assert verified_rollback.state == "succeeded"
        assert verified_rollback.evidence.health == "healthy"
        assert verified_rollback.evidence.container_id == "d" * 64
        assert sum(method == "POST" and path.endswith("/containers/create")
                   for method, path, _ in requests) == 2
        assert sum(method == "POST" and path.endswith("/exec")
                   for method, path, _ in requests) == 6
        assert not any("/pull" in path or "/build" in path
                       for _, path, _ in requests)
    finally:
        adapter.close()
        driver.close()


def test_adapter_holds_lost_create_response_and_reconcile_does_not_repeat_effect(tmp_path):
    from tests.test_hub_container_executor_adapter import command, make_fixture

    adapter, _placeholder_backend, _state, manifest = make_fixture(tmp_path)
    images = {role: adapter._verified_image(role) for role in ("candidate", "rollback")}
    image_by_ref = {image.runnable_reference: image for image in images.values()}
    blobs_by_id = dict(adapter.metadata_store.values)
    secrets = dict(adapter.secret_resolver.values)
    baseline = adapter.baseline_provider.baseline
    baseline_path = safe_generation(tmp_path, baseline.state_generation_id)
    safe_generation(tmp_path, "candidate-g1")
    rollback = images["rollback"]
    defaults = adapter._defaults_from_record(rollback)
    baseline_profile = adapter._profile(rollback, defaults,
                                        baseline.state_generation_id, secrets)
    baseline_observation = container_inspect(
        rollback, baseline_profile, baseline_path, baseline.container_id, "running"
    )
    requests = []
    create_posts = 0

    def engine_handler(request):
        nonlocal create_posts
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        requests.append((request.method, path))
        if path.endswith("/info"):
            return httpx.Response(200, json={"ID": manifest.runner_binding.daemon_identity,
                                             "OSType": "linux", "Architecture": "amd64"})
        if "/images/" in path and path.endswith("/json"):
            image = next(value for reference, value in image_by_ref.items()
                         if reference.replace(":", "%3A") in path)
            return httpx.Response(200, json=docker_config(image))
        if "/images/" in path and path.endswith("/get"):
            image = next(value for reference, value in image_by_ref.items()
                         if reference.replace(":", "%3A") in path)
            return httpx.Response(200, content=oci_archive(image, blobs_by_id))
        if path.endswith("/containers/" + baseline.container_id + "/json"):
            return httpx.Response(200, json=baseline_observation)
        if "/containers/" in path and path.endswith("/json"):
            return httpx.Response(404, json={"message": "absent"})
        if path.endswith("/containers/create"):
            create_posts += 1
            raise httpx.ReadTimeout("synthetic create response loss")
        raise AssertionError(f"unexpected fake route {request.method} {path}")

    driver = DockerEngineBackend(
        binding(manifest, tmp_path), test_transport=httpx.MockTransport(engine_handler)
    )
    adapter.backend = driver
    request = command(manifest, "deploy_container")
    try:
        response = adapter.submit(request)
        assert response.state == "held" and response.reason == "inspect_required"
        assert create_posts == 1
        reconciled = adapter.reconcile(request.command_id)
        assert reconciled.state == "held"
        assert create_posts == 1
        assert not any(path.endswith("/start") or path.endswith("/exec")
                       for _, path in requests)
    finally:
        adapter.close()
        driver.close()
