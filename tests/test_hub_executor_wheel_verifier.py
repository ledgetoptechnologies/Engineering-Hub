"""Offline synthetic ZIP consistency tests; never install/import wheel code."""
import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import warnings
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fixture_executor_wheel_verifier", ROOT / "scripts/verify_hub_executor_wheel.py")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)
COMMIT = "b" * 40
CORE = "ghcr.io/fixture/core@sha256:" + "c" * 64
DIST = VERIFIER.DIST
PROVENANCE = VERIFIER.PROVENANCE
RECORD = DIST + "/RECORD"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def source_map(files):
    return {"src/" + name: hashlib.sha256(raw).hexdigest()
            for name, raw in files.items() if name.endswith(".py")}


def contents(marker):
    # CRLF source and metadata exercise binary file reads on Windows. If wheel
    # code is ever imported/executed, it leaves a detectable external marker.
    code = ("from pathlib import Path\r\nPath(" + repr(str(marker))
            + ").write_text('wheel code executed')\r\n").encode()
    result = {name: code for name in VERIFIER.REQUIRED if name.endswith(".py")}
    result[DIST + "/METADATA"] = (
        b"Metadata-Version: 2.3\r\nName: engineering-hub-container-executor\r\n"
        b"Version: 0.1.0\r\nRequires-Dist: agent-message-bus==0.6.0\r\n\r\n")
    result[DIST + "/WHEEL"] = b"Wheel-Version: 1.0\r\nRoot-Is-Purelib: true\r\nTag: py3-none-any\r\n"
    result[PROVENANCE] = canonical(dict(schema_version=1, source_commit=COMMIT,
        source_archive_sha256="a" * 64, source_files=source_map(result)))
    return result


def record(files):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    for name, raw in sorted(files.items()):
        if name != RECORD:
            digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
            writer.writerow([name, "sha256=" + digest, str(len(raw))])
    writer.writerow([RECORD, "", ""])
    return output.getvalue().encode()


def wheel(tmp_path, *, mutate=None, mutate_record=None, extras=()):
    marker = tmp_path / "must-not-execute"
    files = contents(marker)
    if mutate:
        mutate(files)
    files[RECORD] = record(files)
    if mutate_record:
        files[RECORD] = mutate_record(files[RECORD])
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:", category=UserWarning)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, raw in sorted(files.items()):
                archive.writestr(name, raw)
            for name, raw, mode in extras:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED)
    path = tmp_path / "executor.whl"
    path.write_bytes(output.getvalue())
    return path, hashlib.sha256(output.getvalue()).hexdigest(), marker


def verify(path, digest, **changes):
    values = dict(wheel=path, sha256=digest, source_commit=COMMIT, core_image=CORE)
    values.update(changes)
    return VERIFIER.verify_wheel(**values)


def rejected(path, digest, **changes):
    with pytest.raises(ValueError, match="^executor wheel verification failed$"):
        verify(path, digest, **changes)


def test_valid_raw_crlf_wheel_matches_external_pins_without_import_or_extraction(tmp_path):
    path, digest, marker = wheel(tmp_path)
    assert b"\r\n" in path.read_bytes()  # Raw ZIP bytes must survive text-mode translation.
    assert verify(path, digest) == dict(source_commit=COMMIT, wheel_sha256=digest,
        core_image=CORE, python_files_verified=11)
    assert not marker.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == ["executor.whl"]


@pytest.mark.parametrize('module', ['installed_coordinator', 'launch_supervisor', 'launcher_cli', 'configuration_commitment'])
def test_rejects_self_consistent_wheel_missing_current_installation_module(tmp_path, module):
    def mutate(files):
        del files['hub_container_executor/' + module + '.py']
        provenance = json.loads(files[PROVENANCE])
        del provenance['source_files']['src/hub_container_executor/' + module + '.py']
        files[PROVENANCE] = canonical(provenance)
    path, digest, marker = wheel(tmp_path, mutate=mutate)
    rejected(path, digest)
    assert not marker.exists()


@pytest.mark.parametrize("changes", [
    {"sha256": "0" * 64}, {"sha256": "A" * 64}, {"sha256": "short"},
    {"source_commit": "d" * 40}, {"source_commit": "B" * 40},
    {"core_image": "ghcr.io/fixture/core:latest"}, {"core_image": "ghcr.io/fixture/core"},
    {"core_image": "ghcr.io/fixture/core@sha256:" + "D" * 64},
])
def test_requires_exact_external_hash_commit_and_digest_pinned_core(tmp_path, changes):
    path, digest, marker = wheel(tmp_path)
    rejected(path, digest, **changes)
    assert not marker.exists()


@pytest.mark.parametrize("name", [
    "../escape.py", "/absolute.py", "hub_container_executor\\extra.py",
    "hub_container_executor//extra.py", "hub_container_executor/./extra.py",
    "hub_container_executor/extra.pth", "unapproved.pth", "unapproved/extra.py",
    DIST + "/", "hub_container_executor/__init__.py",
])
def test_rejects_unsafe_unapproved_directory_and_duplicate_members(tmp_path, name):
    path, digest, marker = wheel(tmp_path, extras=[(name, b"fixture", stat.S_IFREG | 0o644)])
    rejected(path, digest)
    assert not marker.exists()
    assert not (tmp_path / "escape.py").exists()


def test_rejects_symlink_member_before_any_installation(tmp_path):
    path, digest, marker = wheel(tmp_path, extras=[
        ("hub_container_executor/extra.py", b"/fixture/target", stat.S_IFLNK | 0o777)])
    rejected(path, digest)
    assert not marker.exists()


def test_rejects_member_expanding_beyond_bound(tmp_path):
    path, digest, _ = wheel(tmp_path, extras=[("hub_container_executor/large.py",
        b"x" * (VERIFIER.MAX_MEMBER_BYTES + 1), stat.S_IFREG | 0o644)])
    assert path.stat().st_size < VERIFIER.MAX_WHEEL_BYTES
    rejected(path, digest)


def test_rejects_total_expanded_size_and_member_count_bounds(tmp_path):
    path, digest, _ = wheel(tmp_path, extras=[("hub_container_executor/large" + str(i) + ".py",
        b"x" * VERIFIER.MAX_MEMBER_BYTES, stat.S_IFREG | 0o644) for i in range(9)])
    rejected(path, digest)
    path, digest, _ = wheel(tmp_path, extras=[("hub_container_executor/n" + str(i) + ".py",
        b"", stat.S_IFREG | 0o644) for i in range(1025)])
    rejected(path, digest)


@pytest.mark.parametrize("mutation", ["commit", "source-hash", "missing-source", "extra-field", "archive-hash", "duplicate-json"])
def test_rejects_inconsistent_or_malformed_embedded_source_record(tmp_path, mutation):
    def mutate(files):
        value = json.loads(files[PROVENANCE])
        if mutation == "commit":
            value["source_commit"] = "d" * 40
        elif mutation == "source-hash":
            value["source_files"]["src/hub_container_executor/installed_runner.py"] = "0" * 64
        elif mutation == "missing-source":
            value["source_files"].pop("src/hub_container_executor/installed_runner.py")
        elif mutation == "extra-field":
            value["approved"] = True
        elif mutation == "archive-hash":
            value["source_archive_sha256"] = "not-a-digest"
        files[PROVENANCE] = canonical(value)
        if mutation == "duplicate-json":
            files[PROVENANCE] = files[PROVENANCE][:-1] + b',"schema_version":1}'
    path, digest, _ = wheel(tmp_path, mutate=mutate)
    rejected(path, digest)


@pytest.mark.parametrize("mutation", ["digest", "size", "missing", "duplicate", "self-hash", "unlisted"])
def test_rejects_record_mismatch_or_nonclosed_membership(tmp_path, mutation):
    def mutate(raw):
        rows = list(csv.reader(io.StringIO(raw.decode())))
        if mutation == "digest":
            rows[0][1] = "sha256=" + "a" * 43
        elif mutation == "size":
            rows[0][2] = str(int(rows[0][2]) + 1)
        elif mutation == "missing":
            rows.pop(0)
        elif mutation == "duplicate":
            rows.append(rows[0])
        elif mutation == "self-hash":
            rows[-1][1] = "sha256=" + "a" * 43
        elif mutation == "unlisted":
            rows.append(["unlisted.py", "sha256=" + "a" * 43, "1"])
        result = io.StringIO(newline="")
        csv.writer(result, lineterminator="\r\n").writerows(rows)
        return result.getvalue().encode()
    path, digest, _ = wheel(tmp_path, mutate_record=mutate)
    rejected(path, digest)


@pytest.mark.parametrize("old,new", [
    (b"Name: engineering-hub-container-executor", b"Name: other-package"),
    (b"Version: 0.1.0", b"Version: 0.2.0"),
    (b"Requires-Dist: agent-message-bus==0.6.0", b"Requires-Dist: agent-message-bus>=0.6.0"),
])
def test_rejects_distribution_name_version_or_dependency_mismatch(tmp_path, old, new):
    def mutate(files):
        files[DIST + "/METADATA"] = files[DIST + "/METADATA"].replace(old, new)
    path, digest, _ = wheel(tmp_path, mutate=mutate)
    rejected(path, digest)


@pytest.mark.parametrize("extra", [
    b"Name: conflicting-package", b"name: engineering-hub-container-executor",
    b"Version: 9.9.9", b"version: 0.1.0",
    b"Requires-Dist: unapproved-package==1.0", b"requires-dist: agent-message-bus==0.6.0",
])
def test_rejects_conflicting_case_variant_headers_and_additional_dependencies(tmp_path, extra):
    def mutate(files):
        files[DIST + "/METADATA"] = files[DIST + "/METADATA"].rstrip(b"\r\n") + b"\r\n" + extra + b"\r\n\r\n"
    path, digest, _ = wheel(tmp_path, mutate=mutate)
    rejected(path, digest)


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "not-zip", "oversized"])
def test_invalid_wheel_inputs_fail_sanitized_without_extraction(tmp_path, kind):
    path = tmp_path / "input"
    if kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes(b"" if kind == "empty" else b"not a wheel")
        if kind == "oversized":
            with path.open("r+b") as value:
                value.truncate(VERIFIER.MAX_WHEEL_BYTES + 1)
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "0" * 64
    rejected(path, digest)


def test_recipe_is_separate_pinned_offline_consumer_not_mutable_checkout_install():
    text = (ROOT / "docker/Dockerfile.hub-executor").read_text()
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert lines[0] == "ARG CORE_IMAGE"
    assert "ARG CORE_IMAGE=" not in text
    assert "FROM ${CORE_IMAGE} AS core" in lines
    assert any(line.startswith("FROM python:3.12-slim@sha256:") for line in lines)
    assert "FROM scratch" in lines
    assert "COPY executor.whl /build/engineering_hub_container_executor-0.1.0-py3-none-any.whl" in lines
    assert "COPY validate_executor_wheel.py /build/validate_executor_wheel.py" in lines
    assert text.index("runpy.run_path") < text.index("pip install")
    assert "RUN --network=none python -I" in text
    assert "--no-deps --no-index --no-compile --no-cache-dir --disable-pip-version-check" in text
    assert "COPY --from=core /opt/venv/ /opt/venv/" in lines
    assert text.index("COPY --from=executor-installer") < text.index("importlib.import_module")
    assert "RUN --network=none /opt/venv/bin/python -I -B" in text
    assert "engineering_hub.current_executor_observation" in text
    assert "engineering_hub.current_executor_transport" in text
    assert "record['source_files']" in text and "is_relative_to(root)" in text
    assert "USER 10001:10001" in lines
    assert 'ENTRYPOINT ["/opt/venv/bin/python", "-I", "-m", "hub_container_executor.installed_runner"]' in lines
    assert 'CMD ["--config", "/operator/runner.json"]' in lines
    assert not any(line.startswith(("ADD ", "COPY .", "HEALTHCHECK ", "EXPOSE ")) for line in lines)
    assert not any(term in text for term in ("pip install -e", "curl ", "wget ", "apt-get", "docker.sock", "signing_key"))
