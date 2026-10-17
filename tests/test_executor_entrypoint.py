import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


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
