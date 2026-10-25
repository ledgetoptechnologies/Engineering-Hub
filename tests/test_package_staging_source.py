import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest


spec = importlib.util.spec_from_file_location(
    "package_staging_source", Path(__file__).parents[1] / "scripts/package_staging_source.py")
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


def run(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    run(tmp_path, "init", "-q")
    run(tmp_path, "config", "user.name", "Fixture")
    run(tmp_path, "config", "user.email", "fixture@example.invalid")
    run(tmp_path, "config", "core.autocrlf", "false")
    for name in pack.LOCK_LABELS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((name + "\n").encode())
    (tmp_path / "main.py").write_bytes(b"print('committed')\n")
    commit(tmp_path)
    return tmp_path


def commit(repo):
    run(repo, "add", "--all")
    run(repo, "commit", "-qm", "fixture")
    return run(repo, "rev-parse", "HEAD").decode().strip()


def test_exact_commit_dirty_tree_and_dependency_binding(repo):
    expected = run(repo, "rev-parse", "HEAD").decode().strip()
    first, meta = pack.build_package(repo, "HEAD")
    (repo / "main.py").write_bytes(b"dirty")
    (repo / "uv.lock").write_bytes(b"dirty dependency")
    (repo / "untracked").write_bytes(b"not committed")
    second, current = pack.build_package(repo, expected)
    assert first == second and meta == current
    with zipfile.ZipFile(io.BytesIO(second)) as archive:
        assert archive.read("main.py") == b"print('committed')\n"
        assert "untracked" not in archive.namelist()
        assert meta["entries"] == len(archive.namelist())
        assert meta["expanded_bytes"] == sum(i.file_size for i in archive.infolist())
        for name, label in pack.LOCK_LABELS.items():
            digest = hashlib.sha256(archive.read(name)).hexdigest()
            assert meta["dependency_sha256"][name] == digest
            assert meta["required_cache_labels"][label] == digest
    assert meta["source_commit"] == expected
    assert meta["source_digest"] == hashlib.sha256(second).hexdigest()
    assert not meta["provenance"]["server_commit_verified"]
    assert meta["provenance"]["worker_source_commit_verification"] == "declared"
    assert not meta["cache_image_verified"]


def test_attributes_do_not_omit_or_substitute_committed_bytes(repo):
    (repo / ".gitattributes").write_text("main.py export-ignore\nversion.txt export-subst\n")
    (repo / "version.txt").write_bytes(b"$Format:%H$\n")
    commit(repo)
    (repo / ".git/info/attributes").write_text("uv.lock export-ignore\n")
    payload, _ = pack.build_package(repo, "HEAD")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.read("version.txt") == b"$Format:%H$\n"
        assert archive.read("main.py") == b"print('committed')\n"
        assert archive.read("uv.lock") == b"uv.lock\n"


def test_replacement_objects_and_caller_git_routing_cannot_rewrite_package(repo, monkeypatch):
    original, meta = pack.build_package(repo, "HEAD")
    blob = run(repo, "rev-parse", "HEAD:main.py").decode().strip()
    replacement = subprocess.check_output(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=b"replacement\n").decode().strip()
    run(repo, "replace", blob, replacement)
    monkeypatch.setenv("GIT_DIR", str(repo / "not-a-repository"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    assert pack.build_package(repo, "HEAD") == (original, meta)


@pytest.mark.parametrize("kind", ["symlink", "submodule", "lfs", "case", "directory_alias"])
def test_unsupported_committed_entries_fail_closed(repo, kind):
    if kind in ("symlink", "submodule"):
        oid = run(repo, "rev-parse", "HEAD:main.py" if kind == "symlink" else "HEAD").decode().strip()
        run(repo, "update-index", "--add", "--cacheinfo",
            ("120000" if kind == "symlink" else "160000") + "," + oid + ",unsupported")
        run(repo, "commit", "-qm", "unsupported")
    else:
        if kind == "lfs":
            (repo / "large.dat").write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 100\n")
        elif kind == "case":
            oid = run(repo, "rev-parse", "HEAD:main.py").decode().strip()
            run(repo, "update-index", "--add", "--cacheinfo", "100644," + oid + ",MAIN.py")
            run(repo, "commit", "-qm", "case")
        else:
            # Index-only construction works on case-insensitive Windows too.
            oid = run(repo, "rev-parse", "HEAD:main.py").decode().strip()
            run(repo, "update-index", "--add", "--cacheinfo", "100644," + oid + ",MAIN.PY/child")
            run(repo, "commit", "-qm", "alias")
        if kind == "lfs":
            commit(repo)
    with pytest.raises(pack.PackageError):
        pack.build_package(repo, "HEAD")


@pytest.mark.parametrize("setting,value,reason", [
    ("MAX_ENTRIES", 1, "entry_limit"),
    ("MAX_EXPANDED", 1, "expanded_limit"),
    ("MAX_ARCHIVE", 1, "archive_limit"),
])
def test_limits_reject_before_publication(repo, monkeypatch, setting, value, reason):
    monkeypatch.setattr(pack, setting, value)
    with pytest.raises(pack.PackageError, match=reason):
        pack.build_package(repo, "HEAD")


@pytest.mark.parametrize("ref", ["--output=owned", "HEAD;echo injected", "missing", ""])
def test_untrusted_refs_are_not_options_or_shell(repo, ref):
    with pytest.raises(pack.PackageError):
        pack.build_package(repo, ref)
    assert not (repo / "owned").exists()


def test_missing_lock_refuses(repo):
    run(repo, "rm", "uv.lock")
    run(repo, "commit", "-qm", "missing")
    with pytest.raises(pack.PackageError, match="dependency_files_missing"):
        pack.build_package(repo, "HEAD")


def test_missing_promisor_blob_never_runs_external_transport(repo):
    marker = repo / "transport-was-invoked"
    trap = repo / "transport_trap.py"
    trap.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
    run(repo, "config", "extensions.partialClone", "origin")
    run(repo, "config", "remote.origin.promisor", "true")
    run(repo, "config", "remote.origin.url", "ext::" + Path(sys.executable).as_posix() + " " + trap.as_posix())
    run(repo, "config", "protocol.ext.allow", "always")
    oid = run(repo, "rev-parse", "HEAD:uv.lock").decode().strip()
    missing = repo / ".git/objects" / oid[:2] / oid[2:]
    missing.chmod(0o600)  # Git objects carry Windows read-only attributes.
    missing.unlink()
    # Positive control: the fixture really is a fetch-capable promisor with a
    # working local-only external transport trap (it never opens a network).
    subprocess.run(["git", "-C", str(repo), "cat-file", "blob", oid],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    assert marker.exists()
    marker.unlink()
    started = time.monotonic()
    with pytest.raises(pack.PackageError):
        pack.build_package(repo, "HEAD")
    assert time.monotonic() - started < 10
    assert not marker.exists()


@pytest.mark.parametrize("body", [
    "import time;time.sleep(10)",
    "import sys,time\nfor _ in range(100):\n sys.stdout.write('x');sys.stdout.flush();time.sleep(.05)",
])
def test_git_whole_process_deadline_includes_slow_output(repo, monkeypatch, body):
    original = pack.subprocess.Popen
    captured = []

    def substitute(command, **kwargs):
        captured.append((command, kwargs["env"]))
        return original([sys.executable, "-c", body], **kwargs)

    monkeypatch.setattr(pack.subprocess, "Popen", substitute)
    monkeypatch.setattr(pack, "GIT_TIMEOUT", .2)
    started = time.monotonic()
    with pytest.raises(pack.PackageError, match="git_deadline"):
        pack.git(repo, "rev-parse", "HEAD", limit=128)
    assert time.monotonic() - started < 3
    command, env = captured[0]
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert "protocol.allow=never" in command


def test_git_output_budget_stops_oversized_producer(repo, monkeypatch):
    original = pack.subprocess.Popen

    def substitute(command, **kwargs):
        return original([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*1000000)"], **kwargs)

    monkeypatch.setattr(pack.subprocess, "Popen", substitute)
    with pytest.raises(pack.PackageError, match="git_output_limit"):
        pack.git(repo, "rev-parse", "HEAD", limit=128)


@pytest.mark.parametrize("name", [b"../file", b"a\\b", b"/absolute", b"C:drive", b"a//b", b"a/./b", b"a\nfile", b"\xff"])
def test_unsafe_names(name):
    with pytest.raises(pack.PackageError):
        pack.safe_path(name)


def test_cli_exclusive_output_and_safe_failure(repo, tmp_path):
    output = repo / "package"
    script = Path(pack.__file__)
    import sys
    args = [sys.executable, str(script), "--repo", str(repo), "--ref", "HEAD", "--output", str(output)]
    first = subprocess.run(args, capture_output=True)
    assert first.returncode == 0 and json.loads(first.stdout)["submitted"] is False
    original = (output / "source.zip").read_bytes()
    again = subprocess.run(args, capture_output=True)
    assert again.returncode == 1
    assert (output / "source.zip").read_bytes() == original
