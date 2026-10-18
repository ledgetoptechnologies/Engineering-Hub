"""Baked package identity, independent of runtime configuration or Git checkout."""
import io
import json
from pathlib import Path
import shlex

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_message_bus.auth import AuthSettings
from engineering_hub import build_info
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.dto import HealthDTO


COMMIT = "abcde01234" * 4


class Resource:
    def __init__(self, content):
        self.content = content

    def joinpath(self, name):
        assert name == "_build_identity.json"
        return self

    def open(self, mode):
        assert mode == "rb"
        if self.content is None:
            raise FileNotFoundError
        return io.BytesIO(self.content)


def resource(monkeypatch, content):
    def files(package):
        assert package == "engineering_hub"
        return Resource(content)
    monkeypatch.setattr(build_info.resources, "files", files)


def test_valid_fixed_resource_ignores_runtime_environment(monkeypatch):
    resource(monkeypatch, json.dumps({"schema_version": 1, "build_commit": COMMIT}).encode())
    for name in ("VCS_REF", "HUB_BUILD_COMMIT", "BUILD_COMMIT", "GIT_COMMIT"):
        monkeypatch.setenv(name, "f" * 40)
    assert build_info.read_build_commit() == COMMIT


@pytest.mark.parametrize("content", [
    None, b"", b"{}", b"null", b"[]", b"\xff", b"{" , b"x" * 1025,
    b'{"schema_version":1,"build_commit":null}',
    b'{"schema_version":true,"build_commit":"' + COMMIT.encode() + b'"}',
    b'{"schema_version":2,"build_commit":"' + COMMIT.encode() + b'"}',
    b'{"schema_version":1,"build_commit":"' + COMMIT.encode() + b'","extra":1}',
    b'{"schema_version":1,"build_commit":null,"build_commit":"' + COMMIT.encode() + b'"}',
    *[json.dumps({"schema_version": 1, "build_commit": value}).encode()
      for value in ("unknown", "A" * 40, "a" * 39, "a" * 41, COMMIT + "\n", 1, True, {})],
])
def test_missing_or_malformed_identity_is_unknown(monkeypatch, content):
    resource(monkeypatch, content)
    monkeypatch.setenv("VCS_REF", COMMIT)
    assert build_info.read_build_commit() is None


@pytest.mark.parametrize("commit", [COMMIT, None])
def test_health_reports_liveness_and_baked_identity(tmp_path, monkeypatch, commit):
    resource(monkeypatch, json.dumps({"schema_version": 1, "build_commit": commit}).encode())
    settings = Settings(tmp_path / "hub.sqlite3",
                        AuthSettings("test-hermes", "test-codex", claude_token="test-claude"),
                        AuthConfig("https://hub.test", beau_github_id=42))
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "service": "engineering-hub-control",
                                   "build_commit": commit}


def test_health_dto_rejects_untrusted_claim():
    with pytest.raises(ValidationError):
        HealthDTO(ok=True, service="engineering-hub-control", build_commit="unknown")


@pytest.mark.parametrize("ref", [COMMIT, "unknown", "A" * 40, "a" * 39,
                                  COMMIT + "\n", "';raise RuntimeError();#", ""])
def test_exact_docker_identity_generation_recipe(monkeypatch, ref):
    """Exercise the shipped recipe without building/running Docker or writing files."""
    dockerfile = Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.hub-control"
    recipes = [line.removeprefix("RUN ") for line in dockerfile.read_text().splitlines()
               if line.startswith("RUN python -c ") and "_build_identity.json" in line]
    assert len(recipes) == 1
    command = shlex.split(recipes[0])
    assert command[:2] == ["python", "-c"] and len(command) == 3
    written = []

    class Output(Resource):
        def write_text(self, text, *, encoding):
            assert encoding == "utf-8"
            written.append(json.loads(text))

    monkeypatch.setattr(build_info.resources, "files", lambda package: Output(None)
                        if package == "engineering_hub" else pytest.fail("wrong package"))
    monkeypatch.setenv("VCS_REF", ref)
    if ref in (COMMIT, "unknown"):
        exec(compile(command[2], "<docker-build-identity>", "exec"), {})
        assert written == [{"schema_version": 1,
                            "build_commit": None if ref == "unknown" else ref}]
    else:
        with pytest.raises(SystemExit):
            exec(compile(command[2], "<docker-build-identity>", "exec"), {})
        assert written == []
