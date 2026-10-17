import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="Fixture executor requires Linux kernel flock; verify executor acceptance on Linux")


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_module_entrypoint_serves_health_from_private_token_file(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    data_root = tmp_path / "data"
    token_path = tmp_path / "executor-token"
    token_path.write_text("local-test-token-not-a-live-credential", encoding="utf-8")
    token_path.chmod(0o600)
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "HUB_EXECUTOR_DATA_DIR": str(data_root),
            "HUB_EXECUTOR_TOKEN_FILE": str(token_path),
            "HUB_EXECUTOR_HOST": "127.0.0.1",
            "HUB_EXECUTOR_PORT": str(port),
            "PYTHONPATH": os.pathsep.join(
                [str(repository / "src"), str(repository), env.get("PYTHONPATH", "")]
            ),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "hub_executor"],
        cwd=repository,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            if process.poll() is not None:
                raise AssertionError("fixture executor entrypoint exited before health became ready")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                    status = response.status
                    health = json.loads(response.read())
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("fixture executor entrypoint did not become ready")
                time.sleep(0.05)
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    assert status == 200
    assert health == {"ok": True}
    assert (data_root / "ledger.sqlite3").is_file()


def test_container_fixture_paths_exclude_other_users(tmp_path):
    import runpy
    import stat
    repository = Path(__file__).resolve().parents[1]
    smoke = runpy.run_path(str(repository / 'docker/hub-test-smoke.py'))
    assert '_prepare_fixture_files' in smoke
    data, secret = smoke['_prepare_fixture_files'](tmp_path, 'fixture-only-secret')
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(data.stat().st_mode) == 0o2770
    assert stat.S_IMODE(secret.stat().st_mode) == 0o440
    assert secret.read_text() == 'fixture-only-secret'


def test_docker_harness_supports_real_backend_scenario_callback():
    import inspect
    import runpy
    repository = Path(__file__).resolve().parents[1]
    smoke = runpy.run_path(str(repository / 'docker/hub-test-smoke.py'))
    assert 'scenario' in inspect.signature(smoke['run_smoke']).parameters


def test_second_service_process_refuses_a_live_owner(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    token = tmp_path / 'private-token'
    token.write_text('fixture-only-owner-lock-test')
    token.chmod(0o600)
    port = _free_port()
    env = os.environ.copy()
    env.update(HUB_EXECUTOR_DATA_DIR=str(tmp_path / 'data'), HUB_EXECUTOR_TOKEN_FILE=str(token),
               HUB_EXECUTOR_HOST='127.0.0.1', HUB_EXECUTOR_PORT=str(port), PYTHONPATH=str(repository))
    first = subprocess.Popen([sys.executable, '-m', 'hub_executor'], cwd=repository, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    second = None
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=0.2):
                    break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.03)
        other = env | {'HUB_EXECUTOR_PORT': str(_free_port())}
        second = subprocess.Popen([sys.executable, '-m', 'hub_executor'], cwd=repository, env=other,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            second.wait(timeout=0.7)
        except subprocess.TimeoutExpired:
            raise AssertionError('second fixture service adopted state while first owner was live')
        assert second.returncode != 0
        assert first.poll() is None
    finally:
        for process in (second, first):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
