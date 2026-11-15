"""Offline unit tests: no Docker daemon or service credentials are used."""
import importlib.util
import json
from pathlib import Path
import subprocess
import stat
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "hub_dashboard_smoke", Path(__file__).parents[1] / "scripts/hub_dashboard_container_smoke.py"
)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

DOCKER_HOST = "unix:///run/eh-test/docker.sock"

def run_smoke(*args, **kwargs):
    kwargs.setdefault("docker_host", DOCKER_HOST)
    return smoke.run_smoke(*args, **kwargs)


IMAGE = "sha256:" + "a" * 64
OWNER = "b" * 32
NAME = "hub-dashboard-smoke-" + OWNER
CONTAINER = "c" * 64
REPORT = {"image_smoke": "passed", "assets": 2, "durability": "not_tested"}


@pytest.mark.parametrize('commit', ['unknown', 'a' * 39, 'A' * 40, 'a' * 40 + '\n', 1])
def test_invalid_expected_commit_never_calls_docker(fake_docker, commit):
    calls, _ = fake_docker
    with pytest.raises(ValueError, match='Expected commit'):
        run_smoke(IMAGE, commit)
    assert calls == []


def test_expected_identity_is_passed_to_probe_and_verified(fake_docker):
    calls, controls = fake_docker
    commit = 'd' * 40
    controls['exec'] = subprocess.CompletedProcess([], 0, stdout=json.dumps(REPORT | {'build_commit': commit}), stderr='')
    assert run_smoke(IMAGE, commit)['build_commit'] == commit
    assert calls[1][0][-1] == commit


def test_mismatched_build_report_fails_and_cleans_up(fake_docker):
    calls, _ = fake_docker
    with pytest.raises(ValueError, match='build identity'):
        run_smoke(IMAGE, 'd' * 40)
    assert calls[-1][0] == ['docker', 'rm', '--force', CONTAINER]


@pytest.fixture
def fake_docker(monkeypatch):
    calls = []
    controls = {"startup": None, "exec": None, "inspect": None, "remove": None}
    monkeypatch.setattr(smoke.uuid, "uuid4", lambda: SimpleNamespace(hex=OWNER))
    monkeypatch.setattr(smoke.shutil, "which", lambda name, path: "/usr/bin/docker")
    monkeypatch.setattr(smoke, "endpoint_realpath", lambda path: path)
    def socket_stat(path):
        if path == DOCKER_HOST[7:]:
            return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_dev=1, st_ino=2)
        raise FileNotFoundError(path)
    monkeypatch.setattr(smoke, "endpoint_stat", socket_stat)

    def run(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/docker", "--host", DOCKER_HOST]
        assert set(kwargs["env"]) == {"PATH", "DOCKER_CONFIG"}
        assert kwargs["env"]["PATH"] == "/usr/bin:/bin"
        assert Path(kwargs["env"]["DOCKER_CONFIG"]).is_dir()
        argv = ["docker", *argv[3:]]
        calls.append((argv, kwargs))
        assert argv[0] == "docker"
        assert kwargs.get("shell", False) is False
        assert kwargs["capture_output"] is True and kwargs["text"] is True
        assert 0 < kwargs["timeout"] <= 60
        if argv[1] == "run":
            phase, output = "startup", CONTAINER + "\n"
        elif argv[1] == "exec":
            phase, output = "exec", json.dumps(REPORT)
        elif argv[1:3] == ["container", "inspect"]:
            phase, output = "inspect", CONTAINER + "|" + OWNER + "\n"
        elif argv[1] == "rm":
            phase, output = "remove", CONTAINER + "\n"
        else:
            pytest.fail(f"Unexpected Docker action: {argv[:3]}")
        outcome = controls[phase]
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return outcome
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    return calls, controls


@pytest.mark.parametrize("image", [None, "", "alpine:latest", "--help", "sha256:" + "A" * 64,
                                       "sha256:" + "a" * 63, IMAGE + "\n", 1])
def test_invalid_image_never_calls_docker(fake_docker, image):
    calls, _ = fake_docker
    with pytest.raises(ValueError, match="immutable local image"):
        run_smoke(image)
    assert calls == []


def test_success_is_bounded_isolated_pinned_and_cleanup_uses_verified_id(fake_docker):
    calls, _ = fake_docker
    assert run_smoke(IMAGE) == REPORT
    run = calls[0][0]
    assert run[:3] == ["docker", "run", "--detach"]
    for option, value in [("--pull", "never"), ("--name", NAME), ("--network", "none"),
                          ("--cap-drop", "ALL"), ("--pids-limit", "64"),
                          ("--memory", "512m"), ("--cpus", "1"),
                          ("--security-opt", "no-new-privileges")]:
        assert run[run.index(option) + 1] == value
    assert "--read-only" in run
    assert run[run.index("--label") + 1] == "engineering-hub.smoke-owner=" + OWNER
    assert run[-1] == IMAGE
    assert not set(run) & {"--mount", "--volume", "-v", "--publish", "-p", "-P", "--privileged",
                          "--env-file", "--pid", "--ipc", "--device"}
    mounts = [run[i + 1] for i, value in enumerate(run) if value == "--tmpfs"]
    assert len(mounts) == 2
    assert all("noexec" in value and "nosuid" in value and "size=" in value for value in mounts)
    assert all(value.startswith(("/var/lib/hub:", "/tmp:")) for value in mounts)
    assert calls[1][0] == ["docker", "exec", NAME, "python", "-c", smoke.PROBE]
    assert calls[2][0][-1] == NAME
    assert calls[3][0] == ["docker", "rm", "--force", CONTAINER]
    assert len(calls) == 4


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("docker run", 60),
                                      subprocess.CalledProcessError(125, "docker run")])
def test_ambiguous_startup_still_cleans_owned_container(fake_docker, failure):
    calls, controls = fake_docker
    controls["startup"] = failure
    with pytest.raises(type(failure)) as captured:
        run_smoke(IMAGE)
    assert captured.value is failure
    assert [argv[1] for argv, _ in calls] == ["run", "container", "rm"]
    assert calls[-1][0] == ["docker", "rm", "--force", CONTAINER]


@pytest.mark.parametrize("stderr", ["Error: No such container: " + NAME,
                                   "Error: No such object: " + NAME])
def test_confirmed_absence_after_startup_failure_never_removes(fake_docker, stderr):
    calls, controls = fake_docker
    controls["startup"] = subprocess.TimeoutExpired("docker run", 60)
    controls["inspect"] = subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)
    with pytest.raises(subprocess.TimeoutExpired):
        run_smoke(IMAGE)
    assert len(calls) == 2 and calls[-1][0][1:3] == ["container", "inspect"]


@pytest.mark.parametrize("identity", [CONTAINER + "|foreign", "bad-id|" + OWNER,
                                     CONTAINER, CONTAINER + "|" + OWNER + "|extra"])
def test_unverified_ownership_never_removes(fake_docker, identity):
    calls, controls = fake_docker
    controls["inspect"] = subprocess.CompletedProcess([], 0, stdout=identity, stderr="")
    with pytest.raises(RuntimeError, match="ownership could not be verified"):
        run_smoke(IMAGE)
    assert all(argv[1] != "rm" for argv, _ in calls)


def test_ambiguous_inspect_failure_is_explicit_and_never_removes(fake_docker):
    calls, controls = fake_docker
    controls["inspect"] = subprocess.CompletedProcess([], 1, stdout="", stderr="daemon unavailable")
    with pytest.raises(RuntimeError, match="cleanup could not be verified"):
        run_smoke(IMAGE)
    assert all(argv[1] != "rm" for argv, _ in calls)


def test_inspect_timeout_never_removes(fake_docker):
    calls, controls = fake_docker
    controls["inspect"] = subprocess.TimeoutExpired("docker inspect", 15)
    with pytest.raises(subprocess.TimeoutExpired):
        run_smoke(IMAGE)
    assert all(argv[1] != "rm" for argv, _ in calls)


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("docker exec", 30),
                                      subprocess.CalledProcessError(1, "docker exec")])
def test_probe_failure_still_cleans_owned_container(fake_docker, failure):
    calls, controls = fake_docker
    controls["exec"] = failure
    with pytest.raises(type(failure)):
        run_smoke(IMAGE)
    assert calls[-1][0] == ["docker", "rm", "--force", CONTAINER]


def test_bad_probe_json_still_cleans_owned_container(fake_docker):
    calls, controls = fake_docker
    controls["exec"] = subprocess.CompletedProcess([], 0, stdout="not-json", stderr="")
    with pytest.raises(json.JSONDecodeError):
        run_smoke(IMAGE)
    assert calls[-1][0] == ["docker", "rm", "--force", CONTAINER]


def test_failed_cleanup_is_not_reported_as_success(fake_docker):
    _, controls = fake_docker
    controls["remove"] = subprocess.CalledProcessError(1, "docker rm")
    with pytest.raises(subprocess.CalledProcessError):
        run_smoke(IMAGE)


def test_cleanup_error_retains_primary_error_in_exception_chain(fake_docker):
    _, controls = fake_docker
    primary = subprocess.TimeoutExpired("docker run", 60)
    controls["startup"] = primary
    controls["inspect"] = subprocess.CompletedProcess([], 1, stdout="", stderr="daemon unavailable")
    with pytest.raises(RuntimeError) as captured:
        run_smoke(IMAGE)
    assert captured.value.__context__ is primary


@pytest.mark.parametrize("endpoint", [None, "", "tcp://host:2375", "unix:///var/run/docker.sock", "unix:///run/docker.sock", "unix:///run/../docker.sock", "unix:///run//docker.sock", "unix:///run/./docker.sock"])
def test_ambient_or_shared_endpoint_is_rejected_before_docker(fake_docker, endpoint):
    calls, _ = fake_docker
    with pytest.raises(ValueError, match="dedicated local Unix"):
        run_smoke(IMAGE, docker_host=endpoint)
    assert not calls


def test_socket_symlink_alias_is_rejected(fake_docker, monkeypatch):
    calls, _ = fake_docker
    monkeypatch.setattr(smoke, "endpoint_realpath", lambda path: "/run/docker.sock")
    with pytest.raises(ValueError, match="aliases"):
        run_smoke(IMAGE)
    assert not calls


def test_shared_socket_inode_is_rejected(fake_docker, monkeypatch):
    calls, _ = fake_docker
    monkeypatch.setattr(smoke, "endpoint_stat", lambda path: SimpleNamespace(st_mode=stat.S_IFSOCK, st_dev=1, st_ino=2))
    with pytest.raises(ValueError, match="Shared system"):
        run_smoke(IMAGE)
    assert not calls


def test_docker_config_is_removed_after_run(fake_docker):
    calls, _ = fake_docker
    run_smoke(IMAGE)
    paths = {kwargs["env"]["DOCKER_CONFIG"] for _, kwargs in calls}
    assert len(paths) == 1
    assert not Path(next(iter(paths))).exists()
