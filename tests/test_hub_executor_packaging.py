import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "executor_wheel_preparation", ROOT / "scripts/build_hub_executor_wheel.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
REVISION = "a" * 40


def archive(extra=(), *, comment=REVISION, omit=None):
    members = [(name, (ROOT / name).read_bytes(), tarfile.REGTYPE)
               for name in MODULE._METADATA]
    members += [(f"hub_container_executor/{name}", b"# inert fixture\n", tarfile.REGTYPE)
                for name in ("__init__.py", "service.py", "local_resources.py", "runtime_identity.py",
                             "installed_runner.py", "launch_transport.py")]
    members += [("scripts/not_shipped.py", b"raise RuntimeError()", tarfile.REGTYPE),
                ("hub_executor/fixtures.py", b"fixture", tarfile.REGTYPE)]
    members += list(extra)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": comment}) as target:
        for name, value, kind in members:
            if name == omit:
                continue
            member = tarfile.TarInfo(name)
            member.type, member.size = kind, len(value)
            target.addfile(member, io.BytesIO(value))
    return buffer.getvalue()


def test_prepare_stages_only_explicit_library_and_helper(tmp_path):
    data = archive()
    out = MODULE.prepare_archive(data, revision=REVISION, output_directory=tmp_path / "new")
    actual = {path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file()}
    assert actual == set(MODULE._METADATA.values()) | {
        f"src/hub_container_executor/{name}" for name in (
            "__init__.py", "service.py", "local_resources.py", "runtime_identity.py",
            "installed_runner.py", "launch_transport.py", "_executor_source.json")}
    record = json.loads((out / "src/hub_container_executor/_executor_source.json").read_text())
    assert record["source_commit"] == REVISION
    assert record["source_archive_sha256"] == hashlib.sha256(data).hexdigest()
    for name, digest in record["source_files"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("extra", [
    [("../escape", b"", tarfile.REGTYPE)],
    [("/absolute", b"", tarfile.REGTYPE)],
    [("hub_container_executor/./service.py", b"alias", tarfile.REGTYPE)],
    [("hub_container_executor//service.py", b"alias", tarfile.REGTYPE)],
    [("hub_container_executor/service.py", b"", tarfile.SYMTYPE)],
    [("hub_container_executor/service.py", b"duplicate", tarfile.REGTYPE)],
])
def test_invalid_members_do_not_publish_output(tmp_path, extra):
    with pytest.raises(ValueError):
        MODULE.prepare_archive(archive(extra), revision=REVISION, output_directory=tmp_path / "new")
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("kwargs", [
    {"comment": "b" * 40}, {"omit": "hub_container_executor/service.py"},
    {"omit": "hub_container_executor/installed_runner.py"},
    {"omit": "hub_container_executor/launch_transport.py"},
])
def test_missing_or_mismatched_source_denied(tmp_path, kwargs):
    with pytest.raises(ValueError):
        MODULE.prepare_archive(archive(**kwargs), revision=REVISION, output_directory=tmp_path / "new")


def test_existing_output_preserved(tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("keep")
    with pytest.raises(ValueError):
        MODULE.prepare_archive(archive(), revision=REVISION, output_directory=tmp_path)
    assert marker.read_text() == "keep"


@pytest.mark.parametrize("revision", ["HEAD", "a" * 39, "A" * 40, "--help"])
def test_only_full_commit_allowed(tmp_path, revision):
    with pytest.raises(ValueError):
        MODULE.prepare_archive(archive(), revision=revision, output_directory=tmp_path / "new")


def test_separate_metadata_describes_explicit_runner_without_console_wrapper_or_core_discovery_change():
    metadata = tomllib.loads((ROOT / "packaging/hub-executor/pyproject.toml").read_text())
    assert metadata["project"]["dependencies"] == ["agent-message-bus==0.6.0"]
    assert "installed-runner module" in metadata["project"]["description"]
    assert "scripts" not in metadata["project"]
    assert "entry-points" not in metadata["project"]
    assert metadata["tool"]["setuptools"]["packages"]["find"]["include"] == ["hub_container_executor", "scripts"]
