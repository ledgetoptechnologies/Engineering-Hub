"""Prepare an inert, TEST-only missing-dashboard-index image build context.

No Docker invocation, network, credential, deployment or trusted build record is
created. The caller must independently inspect/review the selected base image,
which must be the ordinary Hub image with runtime user 568:568. A source commit
claim here records caller input; it does not authenticate upstream provenance.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile


_BASE = re.compile(
    r"ghcr\.io/ledgetoptechnologies/engineering-hub-control@sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_FAULT_SCRIPT = """from pathlib import Path
import stat,sys
root=Path(sys.argv[1])
if root.resolve(strict=True)!=root or root.is_symlink():
    raise SystemExit('Expected canonical dashboard directory')
target=root/'index.html'
if not stat.S_ISREG(target.lstat().st_mode):
    raise SystemExit('Expected regular dashboard index')
target.unlink()
"""


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class PreparedFaultContext:
    archive: bytes
    recipe_digest: str
    inputs_digest: str
    context_digest: str


def prepare_context(*, base_image: str, base_source_commit: str,
                    fixture_source_commit: str) -> PreparedFaultContext:
    """Return deterministic bytes containing only recipe and public inputs.

    `base_source_commit` remains the unchanged application source identity;
    `fixture_source_commit` identifies the separately reviewed fault generator.
    The fault does not change packaged application code/build identity, health,
    authentication routes, ENTRYPOINT, CMD or ENV. Deleting the required HTML
    index must make the closed application-assets expectation fail. Actual
    health/auth behavior and rollback still require a bounded native pilot.
    """
    if type(base_image) is not str or not _BASE.fullmatch(base_image):
        raise ValueError("Exact immutable Hub base repository and digest required")
    for value in (base_source_commit, fixture_source_commit):
        if type(value) is not str or not _COMMIT.fullmatch(value):
            raise ValueError("Lowercase full source commits required")
    # -I ignores PYTHONDONTWRITEBYTECODE; -B is required to avoid importing
    # pathlib/stat writing unrelated .pyc files into the build layer.
    command = ["/opt/venv/bin/python", "-I", "-B", "-c", _FAULT_SCRIPT, "/app/dashboard"]
    recipe = (
        "# TEST ONLY: intentionally unservable dashboard; never promote.\n"
        f"FROM {base_image}\n"
        "USER 0:0\n"
        "RUN --network=none " + json.dumps(command, separators=(",", ":")) + "\n"
        "USER 568:568\n"
        'LABEL engineering-hub.test-only="rollback-missing-index-v1"\n'
    ).encode("utf-8")
    inputs = _canonical({
        "schema": "hub_rollback_fault_inputs_v1",
        "test_only": True,
        "fault": "missing_dashboard_index",
        "base_image": base_image,
        "base_source_commit": base_source_commit,
        "fixture_source_commit": fixture_source_commit,
        "build_recipe_digest": _sha(recipe),
        "changed_path": "/app/dashboard/index.html",
        "expected_runtime_user": "568:568",
        "expected_failure": "hub_app_assets_v1",
        "runtime_acceptance": False,
        "trusted_build_record": False,
    })
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, raw in (("Dockerfile", recipe), ("fault-inputs.json", inputs)):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.mtime = len(raw), 0o600, 0
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            archive.addfile(entry, io.BytesIO(raw))
    raw = stream.getvalue()
    return PreparedFaultContext(raw, _sha(recipe), _sha(inputs), _sha(raw))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--base-source-commit", required=True)
    parser.add_argument("--fixture-source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        context = prepare_context(base_image=args.base_image,
            base_source_commit=args.base_source_commit,
            fixture_source_commit=args.fixture_source_commit)
        # Trusted output parent required. Exclusive creation never overwrites an
        # existing archive; no source checkout or ambient files enter context.
        with args.output.open("xb") as destination:
            destination.write(context.archive)
    except (OSError, ValueError):
        parser.exit(1, "Fault context preparation failed; no build was invoked.\n")
    print(json.dumps({"context_digest": context.context_digest,
                      "build_recipe_digest": context.recipe_digest,
                      "build_inputs_digest": context.inputs_digest,
                      "test_only": True, "build_invoked": False}, sort_keys=True))


if __name__ == "__main__":
    main()
