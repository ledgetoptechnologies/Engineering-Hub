"""Prepare, but never build or activate, an optional executor wheel context."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile


_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_METADATA = {
    "packaging/hub-executor/pyproject.toml": "pyproject.toml",
    "packaging/hub-executor/README.md": "README.md",
    "scripts/inspect_hub_image_identity.py": "src/scripts/inspect_hub_image_identity.py",
}


def prepare_archive(archive: bytes, *, revision: str, output_directory: Path) -> Path:
    """Stage an allowlisted git archive into a new directory; no tool invocation.

    The output parent must be trusted and exclusively controlled by this build.
    Existence checks are not race-free publication against concurrent writers.
    Git's archive comment must match the independently supplied full commit.
    This binds input metadata, not remote provenance or permission to run it.
    """
    if type(revision) is not str or not _REVISION.fullmatch(revision):
        raise ValueError("a lowercase full commit is required")
    output = Path(output_directory)
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError("output must be a new directory with an existing parent")
    selected = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        if source.pax_headers.get("comment") != revision:
            raise ValueError("archive commit does not match revision")
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ValueError("unsafe archive member")
            raw_name = member.name.removesuffix("/") if member.isdir() else member.name
            if raw_name != path.as_posix():
                raise ValueError("noncanonical archive member")
            target = _METADATA.get(member.name)
            if (len(path.parts) == 2 and path.parts[0] == "hub_container_executor"
                    and path.suffix == ".py"):
                target = "src/" + member.name
            if target is None:
                continue
            if not member.isfile() or member.size > 2 * 1024 * 1024 or target in selected:
                raise ValueError("invalid selected archive member")
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError("missing selected archive member")
            with stream:
                selected[target] = stream.read()
    required = set(_METADATA.values()) | {
        "src/hub_container_executor/__init__.py",
        "src/hub_container_executor/service.py",
        "src/hub_container_executor/local_resources.py",
        "src/hub_container_executor/runtime_identity.py",
    }
    if not required.issubset(selected):
        raise ValueError("archive lacks required executor packaging files")
    provenance = json.dumps({
        "schema_version": 1, "source_commit": revision,
        "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "source_files": {name: hashlib.sha256(value).hexdigest()
                         for name, value in sorted(selected.items())},
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    selected["src/hub_container_executor/_executor_source.json"] = provenance
    # Build in a disposable directory; publishing never overwrites existing data.
    with tempfile.TemporaryDirectory(prefix="eh-executor-", dir=output.parent) as temporary:
        staged = Path(temporary) / "context"
        staged.mkdir()
        for name, value in selected.items():
            destination = staged / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
        if output.exists() or output.is_symlink():
            raise ValueError("output appeared during preparation")
        staged.rename(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args(argv)
    if not _REVISION.fullmatch(args.revision):
        parser.error("revision must be a lowercase full commit")
    # No shell, arbitrary revision expressions, checkout reads, or network calls.
    archive = subprocess.run(["git", "archive", "--format=tar", args.revision],
                             check=True, stdout=subprocess.PIPE).stdout
    prepare_archive(archive, revision=args.revision,
                    output_directory=args.output_directory)


if __name__ == "__main__":
    main()
