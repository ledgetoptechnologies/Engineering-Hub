"""Source-only rollback fixture checks; no image build or runtime acceptance."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from scripts.prepare_hub_rollback_fault import prepare_context, main


BASE = "ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:" + "a" * 64


def prepared(**changes):
    values = dict(base_image=BASE, base_source_commit="b" * 40,
                  fixture_source_commit="c" * 40)
    values.update(changes)
    return prepare_context(**values)


def entries(context):
    with tarfile.open(fileobj=io.BytesIO(context.archive), mode="r:") as archive:
        members = archive.getmembers()
        assert [item.name for item in members] == ["Dockerfile", "fault-inputs.json"]
        assert all(item.isfile() and item.mtime == item.uid == item.gid == 0
                   and item.mode == 0o600 for item in members)
        return {item.name: archive.extractfile(item).read() for item in members}


def test_deterministic_isolated_context_and_honest_distinct_source_identities():
    first = prepared()
    assert first == prepared()
    files = entries(first)
    inputs = json.loads(files["fault-inputs.json"])
    assert inputs["base_source_commit"] == "b" * 40
    assert inputs["fixture_source_commit"] == "c" * 40
    assert inputs["trusted_build_record"] is inputs["runtime_acceptance"] is False
    assert inputs["test_only"] is True
    assert inputs["build_recipe_digest"] == first.recipe_digest
    recipe = files["Dockerfile"].decode()
    assert f"FROM {BASE}\n" in recipe
    assert "RUN --network=none [" in recipe
    assert "USER 0:0\n" in recipe and "USER 568:568\n" in recipe
    assert not any(line.startswith(("COPY", "ADD", "ENV", "CMD", "ENTRYPOINT", "ARG"))
                   for line in recipe.splitlines())
    assert "org.opencontainers.image.revision" not in recipe


@pytest.mark.parametrize("base", [
    "engineering-hub-control:latest", "sha256:" + "a" * 64,
    BASE + "\nRUN evil", BASE.replace("@sha256:", ":latest@sha256:"),
    BASE.replace("ledgetoptechnologies", "other"), BASE.upper(),
    BASE[:-1], None,
])
def test_unpinned_foreign_or_injected_base_denied(base):
    with pytest.raises(ValueError):
        prepared(base_image=base)


@pytest.mark.parametrize("field", ["base_source_commit", "fixture_source_commit"])
@pytest.mark.parametrize("value", ["main", "a" * 39, "A" * 40, "a" * 40 + "\n", None])
def test_invalid_source_claims_denied(field, value):
    with pytest.raises(ValueError):
        prepared(**{field: value})


def fault_command():
    recipe = entries(prepared())["Dockerfile"].decode()
    line = next(item for item in recipe.splitlines() if item.startswith("RUN "))
    return json.loads(line.removeprefix("RUN --network=none "))


def test_exact_embedded_fault_only_removes_required_index(tmp_path):
    root = tmp_path / "dashboard"
    root.mkdir()
    index = root / "index.html"
    index.write_text("<title>Engineering Hub</title><script src='/assets/app.js'></script>")
    assets = root / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('unchanged')")
    outside = tmp_path / "hub.sqlite3"
    outside.write_bytes(b"unchanged database fixture")
    command = fault_command()
    assert command[0:2] == ["/opt/venv/bin/python", "-I"]
    assert command[-1] == "/app/dashboard"
    # Substitute interpreter and disposable directory only; execute the exact
    # embedded deletion program. No installed container or application touched.
    completed = subprocess.run([sys.executable, *command[1:-1], str(root)],
                               capture_output=True, timeout=5)
    assert completed.returncode == 0
    assert not index.exists()
    assert (assets / "app.js").read_text() == "console.log('unchanged')"
    assert outside.read_bytes() == b"unchanged database fixture"
    again = subprocess.run([sys.executable, *command[1:-1], str(root)],
                          capture_output=True, timeout=5)
    assert again.returncode != 0  # Missing index cannot silently pass a build.


def test_directory_index_denied_without_deleting_other_paths(tmp_path):
    root = tmp_path / "dashboard"
    root.mkdir()
    (root / "index.html").mkdir()
    command = fault_command()
    result = subprocess.run([sys.executable, *command[1:-1], str(root)],
                            capture_output=True, timeout=5)
    assert result.returncode != 0 and (root / "index.html").is_dir()


def test_cli_exclusive_output_does_not_overwrite(tmp_path):
    output = tmp_path / "fault.tar"
    args = ["--base-image", BASE, "--base-source-commit", "b" * 40,
            "--fixture-source-commit", "c" * 40, "--output", str(output)]
    main(args)
    assert output.read_bytes() == prepared().archive
    with pytest.raises(SystemExit) as failure:
        main(args)
    assert failure.value.code == 1
    assert output.read_bytes() == prepared().archive


def test_missing_index_breaks_real_app_root_but_not_health_or_auth(tmp_path):
    from fastapi.testclient import TestClient
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig

    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (dashboard / "index.html").write_text("<title>Engineering Hub</title>")
    settings = Settings(tmp_path / "hub.sqlite3",
        AuthSettings("synthetic-hermes", "synthetic-codex", claude_token="synthetic-claude"),
        AuthConfig("https://hub.test", beau_github_id=42), ui_directory=dashboard)
    with TestClient(create_app(settings), base_url="https://hub.test") as client:
        assert client.get("/").status_code == 200
        command = fault_command()
        changed = subprocess.run([sys.executable, *command[1:-1], str(dashboard)],
                                 capture_output=True, timeout=5)
        assert changed.returncode == 0
        assert client.get("/").status_code == 404
        health = client.get("/health")
        assert health.status_code == 200 and health.json()["ok"] is True
        assert health.json()["service"] == "engineering-hub-control"
        assert client.get("/api/hub/v1/overview").status_code == 401
    # This is native ASGI source integration only: no installed-image,
    # compiled Engine probe, container deployment or rollback claim.
