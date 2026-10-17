import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path.home() / ".hermes" / "cache" / "scratch"
RESULTS = ROOT / "docker" / "hub-test-results.txt"
TOKEN_LABEL = "hub-executor-v1-container-smoke"
TARGET = "3d-viewer:staging"
CANDIDATE_DIGEST = "sha256:4c031a5fbabf2e39b2aa313c390470863285ffea20396317c6c996a8e1fb8725"
ROLLBACK_DIGEST = "sha256:3ab6e82eb9e517ba1b2a107b4d8b838e7e872a4f7a62c068a07b07d4227103ec"
CANDIDATE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
ROLLBACK_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _run(args, *, env, timeout=120, allow_failure=False):
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode and not allow_failure:
        raise RuntimeError(f"docker step failed ({Path(args[0]).name}, exit {completed.returncode})")
    return completed


def _request(base_url, path, *, token=None, method="GET", body=None):
    headers = {}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    call = urllib.request.Request(
        f"{base_url}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(call, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _payload(action, digest, commit, generation, action_key):
    command_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TOKEN_LABEL}/{action_key}"))
    return {
        "command_id": command_id,
        "target_id": TARGET,
        "action": action,
        "artifact_digest": digest,
        "candidate_commit": commit,
        "policy_revision": 1,
        "fencing_generation": generation,
    }


def _assert_command_shape(value, request):
    required = set(request) | {"state", "reason", "observed"}
    assert set(value) in (required, required | {"replayed"})
    assert value["state"] in {"pending", "running", "succeeded", "failed", "held"}
    assert value["reason"] is None or isinstance(value["reason"], str)
    assert isinstance(value["observed"], dict)
    if "replayed" in value:
        assert type(value["replayed"]) is bool


def _wait_health(base_url, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, body = _request(base_url, "/health")
            if status == 200 and body == {"ok": True}:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise RuntimeError("container health did not recover")


def _inspect_isolation(env, project, data_root, token_path):
    container_id = _run(
        ["docker", "compose", "-f", "compose.hub-test.yaml", "-p", project, "ps", "-q", "api"],
        env=env,
    ).stdout.strip()
    if not container_id:
        raise RuntimeError("smoke container is absent")
    inspected = json.loads(_run(["docker", "inspect", container_id], env=env).stdout)[0]
    host = inspected["HostConfig"]
    assert inspected["Config"]["User"] == "568:568"
    assert host["ReadonlyRootfs"] is True
    assert "ALL" in host["CapDrop"]
    assert any("no-new-privileges" in value for value in host["SecurityOpt"])
    assert host["Memory"] == 134217728
    assert host["PidsLimit"] == 64
    assert host["NanoCpus"] == 1000000000
    ports = inspected["NetworkSettings"]["Ports"]["8080/tcp"]
    assert len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1"
    assert int(ports[0]["HostPort"]) > 0
    binds = [mount for mount in inspected["Mounts"] if mount["Type"] == "bind"]
    assert any(mount["Destination"] == "/data" and mount["Source"] == str(data_root) and mount["RW"] for mount in binds)
    assert any(
        mount["Destination"] == "/run/secrets/hub_executor_token"
        and mount["Source"] == str(token_path)
        and mount["RW"] is False
        for mount in binds
    )
    network_names = inspected["NetworkSettings"]["Networks"].keys()
    assert network_names
    network = json.loads(
        _run(["docker", "network", "inspect", next(iter(network_names))], env=env).stdout
    )[0]
    assert network["Internal"] is False
    assert network["Options"].get("com.docker.network.bridge.enable_ip_masquerade") == "false"
    return ports[0]["HostPort"]


def run_smoke():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    scratch_root = Path(tempfile.mkdtemp(prefix="hub-executor-v1-", dir=SCRATCH))
    scratch_root.chmod(0o755)
    data_root = scratch_root / "data"
    data_root.mkdir(mode=0o777)
    data_root.chmod(0o777)
    token_path = scratch_root / "executor-token"
    token = secrets.token_urlsafe(32)
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o444)
    suffix = secrets.token_hex(5)
    project = f"hub-executor-v1-{suffix}"
    image = f"hub-executor-v1-smoke:{suffix}"
    host_port = _free_port()
    env = os.environ.copy()
    for name in (
        "HUB_EXECUTOR_TOKEN",
        "HUB_EXECUTOR_TOKEN_FILE",
        "HUB_EXECUTOR_DATA_DIR",
        "HUB_EXECUTOR_HOST",
        "HUB_EXECUTOR_PORT",
    ):
        env.pop(name, None)
    env.update(
        {
            "HUB_TEST_DATA": str(data_root),
            "HUB_TEST_TOKEN_FILE": str(token_path),
            "HUB_TEST_IMAGE": image,
            "HUB_TEST_PORT": str(host_port),
        }
    )
    compose = ["docker", "compose", "-f", "compose.hub-test.yaml", "-p", project]
    checks = []
    try:
        _run(
            compose + ["up", "--build", "--detach", "--wait", "--wait-timeout", "45"],
            env=env,
            timeout=300,
        )
        port = _inspect_isolation(env, project, data_root, token_path)
        base_url = f"http://127.0.0.1:{port}"
        health_status, health = _request(base_url, "/health")
        assert health_status == 200 and health == {"ok": True}
        checks.append("isolated container policy and loopback-only port")

        candidate = _payload("deploy_fixture", CANDIDATE_DIGEST, CANDIDATE_COMMIT, 1, "candidate")
        unauth_status, unauth = _request(
            base_url, "/v1/commands", method="POST", body=candidate
        )
        assert unauth_status == 401 and unauth == {"error": {"code": "unauthorized"}}
        created_status, created = _request(
            base_url, "/v1/commands", token=token, method="POST", body=candidate
        )
        replay_status, replay = _request(
            base_url, "/v1/commands", token=token, method="POST", body=candidate
        )
        _assert_command_shape(created, candidate)
        _assert_command_shape(replay, candidate)
        assert created_status == 201 and created["state"] == "succeeded"
        assert replay_status == 200 and replay["replayed"] is True
        actual_path = data_root / "targets" / "3d-viewer-staging" / "state.json"
        state_after_deploy = json.loads(actual_path.read_text(encoding="utf-8"))
        assert state_after_deploy["effect_count"] == 1
        assert state_after_deploy["active_artifact_digest"] == CANDIDATE_DIGEST
        assert state_after_deploy["artifact_bytes"] == "engineering hub fixture v2\n"
        checks.append("typed authenticated deploy and command-ID dedupe")

        smoke = _payload("smoke_fixture", CANDIDATE_DIGEST, CANDIDATE_COMMIT, 2, "smoke")
        smoke_status, smoke_result = _request(
            base_url, "/v1/commands", token=token, method="POST", body=smoke
        )
        assert smoke_status == 201 and smoke_result["state"] == "succeeded"
        _assert_command_shape(smoke_result, smoke)
        assert json.loads(actual_path.read_text(encoding="utf-8"))["effect_count"] == 2
        checks.append("bounded fixture smoke action")

        _run(compose + ["restart", "api"], env=env, timeout=30)
        _wait_health(base_url)
        get_status, durable = _request(
            base_url, f"/v1/commands/{candidate['command_id']}", token=token
        )
        assert get_status == 200 and durable["state"] == "succeeded"
        assert json.loads(actual_path.read_text(encoding="utf-8"))["effect_count"] == 2
        checks.append("SQLite and target state survive container restart")

        pause_status, paused = _request(
            base_url, "/v1/pause", token=token, method="POST", body={"paused": True}
        )
        assert pause_status == 200 and paused == {"paused": True}
        paused_command = _payload(
            "deploy_fixture", CANDIDATE_DIGEST, CANDIDATE_COMMIT, 3, "paused-command"
        )
        blocked_status, blocked = _request(
            base_url, "/v1/commands", token=token, method="POST", body=paused_command
        )
        assert blocked_status == 423 and blocked == {"error": {"code": "paused"}}
        _run(compose + ["restart", "api"], env=env, timeout=30)
        _wait_health(base_url)
        still_paused_status, still_paused = _request(
            base_url, "/v1/commands", token=token, method="POST", body=paused_command
        )
        assert still_paused_status == 423 and still_paused == {"error": {"code": "paused"}}
        resume_status, resumed = _request(
            base_url, "/v1/pause", token=token, method="POST", body={"paused": False}
        )
        assert resume_status == 200 and resumed == {"paused": False}
        checks.append("pause admission persists across restart")

        rollback = _payload("rollback_fixture", ROLLBACK_DIGEST, ROLLBACK_COMMIT, 3, "rollback")
        rollback_status, rolled_back = _request(
            base_url, "/v1/commands", token=token, method="POST", body=rollback
        )
        _assert_command_shape(rolled_back, rollback)
        assert rollback_status == 201 and rolled_back["state"] == "succeeded"
        final_state = json.loads(actual_path.read_text(encoding="utf-8"))
        assert final_state["active_artifact_digest"] == ROLLBACK_DIGEST
        assert final_state["artifact_bytes"] == "engineering hub fixture v1\n"
        assert final_state["effect_count"] == 3
        stale = _payload("rollback_fixture", ROLLBACK_DIGEST, ROLLBACK_COMMIT, 2, "stale")
        stale_status, stale_error = _request(
            base_url, "/v1/commands", token=token, method="POST", body=stale
        )
        assert stale_status == 409 and stale_error == {"error": {"code": "stale_fence"}}
        assert token not in json.dumps([created, replay, smoke_result, rolled_back, final_state])
        checks.append("fixture-only rollback, durable fencing, secret-free evidence")
        return checks
    finally:
        _run(compose + ["down", "--timeout", "5", "--remove-orphans"], env=env, timeout=30, allow_failure=True)
        remaining = _run(compose + ["ps", "-q", "api"], env=env, timeout=15, allow_failure=True)
        if remaining.stdout.strip():
            raise RuntimeError("smoke container cleanup incomplete")
        _run(["docker", "image", "rm", image], env=env, timeout=30, allow_failure=True)
        shutil.rmtree(scratch_root, ignore_errors=True)
        if scratch_root.exists():
            raise RuntimeError("scratch bind-root cleanup incomplete")


def main():
    try:
        checks = run_smoke()
    except Exception as error:
        RESULTS.write_text(
            "Engineering Hub V1 real Docker smoke: FAIL\n"
            f"Failure class: {type(error).__name__}\n",
            encoding="utf-8",
        )
        print(f"Engineering Hub V1 real Docker smoke: FAIL ({type(error).__name__})")
        return 1
    report = "Engineering Hub V1 real Docker smoke: PASS\n" + "\n".join(
        f"- {check}" for check in checks
    ) + "\n"
    RESULTS.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
