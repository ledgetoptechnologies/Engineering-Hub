"""Offline helper unit evidence; these tests do not start Docker or publish."""
import ast
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def load(monkeypatch):
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("split_smoke_under_test", scripts / "hub_split_container_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("image,commit", [
    ("busybox:latest", "a" * 40), ("ghcr.io/ledgetoptechnologies/engineering-hub-control:latest", "a" * 40),
    ("ghcr.io/ledgetoptechnologies/internal-mcp:build-test", "a" * 40),
    ("ghcr.io/ledgetoptechnologies/engineering-hub-control:build-test", "A" * 40),
    ("ghcr.io/ledgetoptechnologies/engineering-hub-control:build-test", "not-sha"),
    (None, "a" * 40),
])
def test_artifact_validation_precedes_any_docker(monkeypatch, image, commit):
    module = load(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("No Docker permitted"))
    with pytest.raises(ValueError):
        module.smoke(image, commit)


def simulation(monkeypatch, module, *, fail=None):
    owner = "c" * 32
    commit = "a" * 40
    calls, containers, volumes = [], set(), set()
    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex=owner))

    def fake(args, **kwargs):
        args = list(args)
        calls.append(args)
        stdout = ""
        if args[1:3] == ["volume", "create"]:
            volumes.add(args[-1])
        elif args[1] == "run":
            containers.add(args[args.index("--name") + 1])
            if fail == "partial_run":
                raise subprocess.CalledProcessError(1, args, stderr="synthetic interrupted result")
        elif args[1:3] in (["container", "inspect"], ["volume", "inspect"]):
            objects = containers if args[1] == "container" else volumes
            if args[-1] not in objects:
                return SimpleNamespace(returncode=1, stdout="", stderr="No such " + args[1])
            if args[1] == "container" and "Config.User" in args[-2]:
                stdout = json.dumps({"user": "568:568", "host": {
                    "NetworkMode": "none", "CgroupnsMode": "private", "ReadonlyRootfs": True,
                    "Privileged": False, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                    "Memory": 0 if fail == "profile" else 512 * 1024 * 1024,
                    "MemorySwap": 512 * 1024 * 1024, "NanoCpus": 1_000_000_000,
                    "PidsLimit": 128, "PortBindings": {}, "Devices": [], "Binds": [],
                    "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"}},
                    "mounts": [{"Type": "volume", "Name": next(iter(volumes)), "Destination": "/var/lib/hub"}]})
            elif args[1] == "container" and fail in {"unknown", "wrong_owner"}:
                return SimpleNamespace(returncode=1 if fail == "unknown" else 0,
                                       stdout="someone-else", stderr="Docker unavailable")
            else:
                stdout = owner
        elif args[1:3] == ["container", "rm"]:
            containers.discard(args[-1])
        elif args[1:3] == ["volume", "rm"]:
            volumes.discard(args[-1])
        elif args[1] == "exec" and args[-2] == module.PROBE:
            stdout = json.dumps({"split_image_smoke": "passed", "build_commit": "b" * 40 if fail == "probe" else commit,
                                 "authenticated_agents": 3, "mcp_tools": 10, "agent_bind": "0.0.0.0:8770",
                                 "browser_bind": "127.0.0.1:8771", "human_sign_in": "not_tested", "network": "none"})
        elif args[1] == "exec" and args[-2] == module.DURABILITY_PROBE:
            stdout = json.dumps({"durable_records_verified": fail != "durable",
                                 "authenticated_readers": 3,
                                 "durable_task_decision_recreation": args[-1] == "1"})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    return calls, containers, volumes, commit


@pytest.mark.parametrize("image", ["ghcr.io/ledgetoptechnologies/engineering-hub-control:build-split-test",
                                  "ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:" + "d" * 64])
def test_plan_hardening_split_entrypoint_recreation_and_owned_cleanup(monkeypatch, image):
    module = load(monkeypatch)
    calls, containers, volumes, commit = simulation(monkeypatch, module)
    report = module.smoke(image, commit)
    runs = [call for call in calls if call[1] == "run"]
    assert len(runs) == 2
    for call in runs:
        assert call[-4:] == [image, "--split-ingress", "--browser-port", "8771"]
        for key, value in (("--network", "none"), ("--user", "568:568"), ("--pull", "never"),
                           ("--cap-drop", "ALL"), ("--cgroupns", "private"), ("--memory", "512m"),
                           ("--memory-swap", "512m"), ("--cpus", "1"), ("--pids-limit", "128")):
            assert call[call.index(key) + 1] == value
        assert "--read-only" in call and "no-new-privileges:true" in call
        assert not {"-p", "--publish", "--privileged", "--device", "--entrypoint", "--env-file"} & set(call)
        assert "type=volume" in call[call.index("--mount") + 1]
        env = [call[index + 1] for index, arg in enumerate(call) if arg == "-e"]
        assert "HUB_HUMAN_AUTH_MODE=cloudflare_access" in env
        assert "AGENT_BUS_ALLOWED_IPS=127.0.0.1" in env
        assert all("synthetic-split-" in value for value in env if "_TOKEN=" in value)
    probes = [call for call in calls if call[1] == "exec" and call[-2] == module.PROBE]
    assert len(probes) == 2 and all(call[-1] == commit for call in probes)
    seeds = [call for call in calls if call[1] == "exec" and call[-1] == module.SEED_RECORDS]
    assert len(seeds) == 1
    durability = [call for call in calls if call[1] == "exec" and call[-2] == module.DURABILITY_PROBE]
    assert len(durability) == 2 and [call[-1] for call in durability] == ["0", "1"]
    markers = [call[-1] for call in calls if call[1] == "exec" and call[-2] == "-c"
               and ".split-smoke" in call[-1]]
    assert "write_text" in markers[0] and "read_text" in markers[1]
    assert report["named_volume_recreation"] == "passed" and report["registry_publication"] == "not_tested"
    assert report["truenas"] == "not_deployed" and report["autonomous_pilot"] == "not_tested"
    assert report["durable_task_decision_recreation"] is True
    assert not containers and not volumes
    assert calls[-1][1:3] == ["volume", "inspect"]


@pytest.mark.parametrize("fail", ["partial_run", "profile", "probe", "durable"])
def test_failed_run_profile_or_probe_reconciles_owned_objects(monkeypatch, fail):
    module = load(monkeypatch)
    calls, containers, volumes, commit = simulation(monkeypatch, module, fail=fail)
    with pytest.raises((RuntimeError, AssertionError)):
        module.smoke("ghcr.io/ledgetoptechnologies/engineering-hub-control:build-test", commit)
    assert not containers and not volumes
    assert any(call[1:3] == ["container", "rm"] for call in calls)
    assert any(call[1:3] == ["volume", "rm"] for call in calls)


@pytest.mark.parametrize("fail", ["unknown", "wrong_owner"])
def test_unknown_or_mismatched_ownership_never_removes_objects_or_live_volume(monkeypatch, fail):
    module = load(monkeypatch)
    calls, containers, volumes, commit = simulation(monkeypatch, module, fail=fail)
    with pytest.raises(RuntimeError):
        module.smoke("ghcr.io/ledgetoptechnologies/engineering-hub-control:build-test", commit)
    assert containers and volumes
    assert not any(call[1:3] in (["container", "rm"], ["volume", "rm"]) for call in calls)


def test_probe_compiles_and_contains_no_fake_login_or_network_dependency(monkeypatch):
    module = load(monkeypatch)
    compile(module.PROBE, "<split-probe>", "exec")
    tree = ast.parse(module.PROBE)
    assert any(isinstance(node, ast.Assert) for node in tree.body)
    for required in ("/proc/net/tcp", "/proc/net/tcp6", "00000000", "0100007F", "expected_commit",
                     "expected_tools", "len(tools) == 10", "os.getuid() == 568", "ProxyHandler({})",
                     "X-Forwarded-For", "CF-Access-Authenticated-User-Email", "forged"):
        assert required in module.PROBE
    assert "https://" not in module.PROBE
    assert "cookie" not in module.PROBE.lower()


@pytest.mark.parametrize("port", [8770, 8771])
def test_probe_request_transport_sets_host_and_bounds_body_and_timeout(monkeypatch, port):
    import time
    import urllib.error
    import urllib.request
    module = load(monkeypatch)
    definitions = [node for node in ast.parse(module.PROBE).body
                   if isinstance(node, ast.FunctionDef) and node.name == "request"]
    requests = []

    class Response:
        status = 401

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 1024 * 1024 + 1
            return b'{"detail":"denied"}'

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:" + str(port) + "/api/hub/v1/overview"
            assert request.get_header("Host") == "hub.ledgetoptechnologies.com"
            assert request.get_header("Authorization") == "Bearer synthetic-only"
            assert 0 < timeout <= 2
            requests.append(request)
            return Response()

    namespace = {"opener": Opener(), "expected_host": "hub.ledgetoptechnologies.com",
                 "time": time, "deadline": time.monotonic() + 30, "urllib": __import__("urllib"), "json": json}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), "<request-unit>", "exec"), namespace)
    status, body = namespace["request"](port, "/api/hub/v1/overview",
                                        headers={"Authorization": "Bearer synthetic-only"})
    assert status == 401 and json.loads(body) == {"detail": "denied"} and len(requests) == 1
    namespace["deadline"] = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        namespace["request"](port, "/api/hub/v1/overview")
    assert len(requests) == 1


def test_durable_fixture_probes_actual_api_records_and_rejects_mismatch(monkeypatch, tmp_path, capsys):
    import sys
    import urllib.request
    from fastapi.testclient import TestClient
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig

    module = load(monkeypatch)
    database = tmp_path / "hub.db"
    monkeypatch.setenv("HUB_DB", str(database))
    for name in ("hermes", "codex", "claude"):
        monkeypatch.setenv(name.upper() + "_TOKEN", "synthetic-" + name)
    exec(compile(module.SEED_RECORDS, "<seed-unit>", "exec"), {})
    metadata = tmp_path / ".split-records.json"
    saved = json.loads(metadata.read_text())
    assert saved["task"]["revision"] == 1 and saved["decision"] is None
    # The packaged test runs on Linux. Windows cannot report Unix permission
    # bits, so this transport unit supplies the Linux metadata for this one file.
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        if path == metadata:
            return SimpleNamespace(st_mode=0o100600)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    active_http = [None]
    requests = []

    class Response:
        def __init__(self, response):
            self.response, self.status = response, response.status_code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return self.response.content[:limit]

    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith("http://127.0.0.1:8770/api/hub/v1/")
            assert request.get_header("Host") == "hub.ledgetoptechnologies.com"
            assert request.get_header("Authorization") in {"Bearer synthetic-" + name for name in ("hermes", "codex", "claude")}
            requests.append(request.get_method())
            response = active_http[0].request(request.get_method(), request.full_url,
                                             headers=dict(request.headers), content=request.data)
            return Response(response)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    settings = Settings(database, AuthSettings("synthetic-hermes", "synthetic-codex",
                        claude_token="synthetic-claude"), AuthConfig("https://hub.ledgetoptechnologies.com"),
                        ingress_mode="agents")
    for phase in (0, 1):
        # A fresh app simulates process recreation for probe unit coverage;
        # only the separate actual Docker run proves packaged recreation.
        with TestClient(create_app(settings)) as http:
            active_http[0] = http
            before = len(requests)
            monkeypatch.setattr(sys, "argv", ["probe", str(phase)])
            exec(compile(module.DURABILITY_PROBE, "<durable-unit>", "exec"), {})
            report = json.loads(capsys.readouterr().out)
            assert report["durable_records_verified"] is True
            assert report["durable_task_decision_recreation"] is (phase == 1)
            if phase == 1:
                assert set(requests[before:]) == {"GET"}  # No replay during verification.
    assert requests.count("POST") == 2  # One claim and one agent decision, only in phase zero.
    state = json.loads(metadata.read_text())
    from engineering_hub.repository import HubRepository
    HubRepository(database).set_candidate("codex", state["task"]["id"], 2, "a" * 40)
    with TestClient(create_app(settings)) as http:
        active_http[0] = http
        monkeypatch.setattr(sys, "argv", ["probe", "1"])
        with pytest.raises(AssertionError, match="Durable task mismatch"):
            exec(compile(module.DURABILITY_PROBE, "<durable-unit>", "exec"), {})
