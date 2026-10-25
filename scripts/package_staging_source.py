"""Package committed Git objects for modular staging; never submit a job."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import zipfile

MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_ENTRIES = 5000
GIT_TIMEOUT = 30.0
LOCK_LABELS = {
    "pyproject.toml": "io.engineering-hub.ci.pyproject-sha256",
    "uv.lock": "io.engineering-hub.ci.uv-lock-sha256",
    "hub-ui/package.json": "io.engineering-hub.ci.package-sha256",
    "hub-ui/package-lock.json": "io.engineering-hub.ci.npm-lock-sha256",
}


class PackageError(ValueError):
    """Safe, fixed diagnostic without repository content."""


def git(repo: Path, *args: str, limit: int) -> bytes:
    env = os.environ.copy()
    # Ignore caller Git routing/config injection; keep normal executable lookup.
    for key in tuple(env):
        if key.startswith("GIT_"):
            del env[key]
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    # An empty explicit allowlist overrides even per-protocol repository config.
    env["GIT_ALLOW_PROTOCOL"] = ""
    env["GIT_PROTOCOL_FROM_USER"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    command = ["git", "--no-replace-objects", "-c", "protocol.allow=never",
               "-C", str(repo), *args]
    deadline = time.monotonic() + GIT_TIMEOUT
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=env, shell=False)
    assert proc.stdout is not None
    data = bytearray()
    errors = []

    def read_bounded():
        try:
            while len(data) <= limit:
                chunk = proc.stdout.read(min(65536, limit + 1 - len(data)))
                if not chunk:
                    return
                data.extend(chunk)
            proc.kill()  # Only our read-only Git subprocess; never a service.
        except OSError:
            errors.append(True)

    reader = threading.Thread(target=read_bounded, daemon=True)
    try:
        reader.start()
        proc.wait(timeout=max(0, deadline - time.monotonic()))
        reader.join(timeout=max(0, deadline - time.monotonic()))
        if reader.is_alive():
            raise subprocess.TimeoutExpired(command, GIT_TIMEOUT)
        if len(data) > limit:
            raise PackageError("git_output_limit")
        if errors or proc.returncode != 0:
            raise PackageError("git_object_read_failed")
        return bytes(data)
    except subprocess.TimeoutExpired:
        raise PackageError("git_deadline") from None
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # No unbounded cleanup wait.
        if reader.ident is not None:
            reader.join(timeout=1)
        if not reader.is_alive():
            proc.stdout.close()


def safe_path(raw: bytes) -> str:
    try:
        name = raw.decode("utf-8", "strict")
    except UnicodeError:
        raise PackageError("non_utf8_path") from None
    parts = name.split("/")
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(p in ("", ".", "..") for p in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise PackageError("unsafe_path")
    return name


def build_package(repo: Path, ref: str) -> tuple[bytes, dict]:
    if not ref or len(ref) > 1024 or "\x00" in ref:
        raise PackageError("invalid_ref")
    commit = git(repo, "rev-parse", "--verify", "--end-of-options",
                 ref + "^{commit}", limit=128).strip().decode("ascii")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise PackageError("unsupported_commit_format")
    tree = git(repo, "ls-tree", "-r", "-l", "-z", commit, limit=2 * 1024 * 1024)
    rows = tree.split(b"\x00")
    if rows[-1] != b"" or not 1 <= len(rows) - 1 <= MAX_ENTRIES:
        raise PackageError("entry_limit")
    entries = []
    seen = set()
    total = 0
    for row in rows[:-1]:
        head, raw_name = row.split(b"\t", 1)
        mode, kind, oid, size = head.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise PackageError("unsupported_tree_entry")
        name = safe_path(raw_name)
        if name.casefold() in seen:
            raise PackageError("case_colliding_paths")
        seen.add(name.casefold())
        if not size.isdigit():
            raise PackageError("missing_blob_size")
        length = int(size)
        total += length
        if total > MAX_EXPANDED:
            raise PackageError("expanded_limit")
        entries.append((name, oid.decode("ascii"), length, mode))
    # File/directory aliases also collide on case-insensitive worker filesystems.
    files = {name.casefold() for name, *_ in entries}
    if any("/".join(name.casefold().split("/")[:i]) in files
           for name, *_ in entries for i in range(1, len(name.split("/")))):
        raise PackageError("file_directory_collision")
    sink = io.BytesIO()
    dependencies = {}
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for name, oid, length, mode in entries:
            blob = git(repo, "cat-file", "blob", oid, limit=length)
            if len(blob) != length:
                raise PackageError("blob_size_mismatch")
            if hashlib.sha1(b"blob " + str(length).encode() + b"\x00" + blob).hexdigest() != oid:
                raise PackageError("blob_object_mismatch")
            if blob.startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise PackageError("lfs_pointer_requires_materialized_provenance")
            if name in LOCK_LABELS:
                dependencies[name] = hashlib.sha256(blob).hexdigest()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (int(mode, 8) << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, blob)
            if sink.tell() > MAX_ARCHIVE:
                raise PackageError("archive_limit")
    payload = sink.getvalue()
    if len(payload) > MAX_ARCHIVE:
        raise PackageError("archive_limit")
    if dependencies.keys() != LOCK_LABELS.keys():
        raise PackageError("dependency_files_missing")
    binding = json.dumps(dependencies, sort_keys=True, separators=(",", ":")).encode()
    metadata = {
        "schema": "engineering-hub.staging-source-package.v1",
        "source_commit": commit,
        "source_digest": hashlib.sha256(payload).hexdigest(),
        "archive_bytes": len(payload), "expanded_bytes": total, "entries": len(entries),
        "provenance": {"local_git_objects_checked": True,
                       "server_commit_verified": False,
                       "worker_source_commit_verification": "declared",
                       "attributes": "exact_blobs_export_ignore_and_export_subst_not_applied"},
        "dependency_sha256": dependencies,
        "required_cache_labels": {LOCK_LABELS[n]: dependencies[n] for n in LOCK_LABELS},
        "dependency_binding_sha256": hashlib.sha256(binding).hexdigest(),
        "cache_image_verified": False,
    }
    return payload, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    args = parser.parse_args()
    try:
        payload, metadata = build_package(args.repo, args.ref)
        # Validation finishes before creating output; never overwrite older packages.
        args.output.mkdir(mode=0o700)
        with (args.output / "source.zip").open("xb") as stream:
            stream.write(payload)
        with (args.output / "metadata.json").open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(metadata, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except (PackageError, OSError, UnicodeError, ValueError):
        parser.exit(1, "Source packaging failed; no job was submitted.\n")
    print(json.dumps({"packaged": True, "source_commit": metadata["source_commit"],
                      "source_digest": metadata["source_digest"], "submitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
