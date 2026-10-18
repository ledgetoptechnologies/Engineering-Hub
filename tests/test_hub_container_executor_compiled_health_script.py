"""Execute the actual compiled probe with controlled HTTP, no Docker or network.

Extract the literal script without importing the Linux-only host package so its
closed health contract can also be regression tested on Windows.
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def probe_script():
    source = Path(__file__).parents[1] / "hub_container_executor" / "daemon_backend.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "_PROBE_SCRIPT" for target in node.targets))


BOOTSTRAP = r'''
import json,sys,urllib.request
script,body,commit=sys.argv[1:]
class Response:
    def getcode(self): return 200
    def read(self,limit):
        assert limit == 8193
        return body.encode()[:limit]
    def close(self): pass
class Opener:
    def open(self,request,timeout):
        assert request.full_url == 'http://127.0.0.1:8770/health'
        assert request.get_method() == 'GET'
        assert 0 < timeout <= 1
        return Response()
urllib.request.build_opener=lambda *args: Opener()
sys.argv=['probe','health_json','/health','200','hub_health_v1','8192','1000',commit]
exec(compile(script,'<actual-compiled-probe>','exec'))
'''


def execute(body, commit="a" * 40):
    # Do not inherit real agent/service credentials into this synthetic process.
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP") if key in os.environ}
    return subprocess.run([sys.executable, "-c", BOOTSTRAP, probe_script(), json.dumps(body), commit],
                          capture_output=True, text=True, timeout=5, env=environment)


@pytest.mark.parametrize("changes,remove,ok", [
    ({}, None, True),
    ({}, "build_commit", False), ({"build_commit": None}, None, False),
    ({"build_commit": "b" * 40}, None, False), ({"build_commit": "A" * 40}, None, False),
    ({"build_commit": "a" * 39}, None, False), ({"build_commit": 123}, None, False),
    ({"extra": "not allowed"}, None, False), ({"service": "other-service"}, None, False),
    ({"ok": 1}, None, False), ({"ok": False}, None, False), ({"ok": "true"}, None, False),
])
def test_real_compiled_health_requires_exact_typed_three_field_body_and_trusted_source(changes, remove, ok):
    body = {"ok": True, "service": "engineering-hub-control", "build_commit": "a" * 40, **changes}
    if remove:
        body.pop(remove)
    result = execute(body)
    assert result.returncode == (0 if ok else 1), result.stderr
    assert json.loads(result.stdout) == {"expectation_id": "hub_health_v1", "ok": ok, "status": 200}
    assert result.stderr == ""


@pytest.mark.parametrize("commit", ["", "A" * 40, "a" * 39, "not-a-source-commit"])
def test_actual_compiled_script_rejects_invalid_expected_commit_without_health_success(commit):
    result = execute({"ok": True, "service": "engineering-hub-control", "build_commit": commit}, commit)
    assert result.returncode == 2 and result.stdout == "" and result.stderr == ""


def test_rollback_response_requires_rollback_commit_not_candidate_commit():
    body = {"ok": True, "service": "engineering-hub-control", "build_commit": "f" * 40}
    assert execute(body, "f" * 40).returncode == 0
    result = execute(body, "a" * 40)
    assert result.returncode == 1 and json.loads(result.stdout)["ok"] is False
