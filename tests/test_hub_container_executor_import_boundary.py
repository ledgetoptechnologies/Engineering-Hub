"""Importing optional host source must not acquire execution capabilities."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Host executor requires Linux ownership")
def test_optional_host_imports_do_not_construct_clients_open_state_or_start_listeners(tmp_path):
    script = r'''
import os, pathlib, socket, sqlite3, subprocess, sys
import httpx, uvicorn
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'src'))
def forbidden(*args, **kwargs):
    raise AssertionError('Optional host import attempted an effect')
sqlite3.connect = forbidden
httpx.Client = forbidden
httpx.AsyncClient = forbidden
uvicorn.run = forbidden
socket.socket.bind = forbidden
socket.socket.connect = forbidden
subprocess.run = forbidden
subprocess.Popen = forbidden
os.open = forbidden
os.mkdir = forbidden
import hub_container_executor
import hub_container_executor.asgi
import hub_container_executor.daemon_backend
import hub_container_executor.operation_journal
import hub_container_executor.local_providers
assert not list(pathlib.Path.cwd().iterdir())
print('optional-host-imports-inert')
'''
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", script, str(Path(__file__).parents[1])],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, "Optional host import was not inert"
    assert result.stdout.strip() == "optional-host-imports-inert"
    assert not list(tmp_path.iterdir())
