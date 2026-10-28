#!/usr/bin/env python3
"""Verify a Hub control image artifact before and after loading it locally."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")
LABEL = "org.opencontainers.image.revision"
EXPECTED_SMOKE_OUTPUTS = [
    "candidate/hub-artifact-startup.json",
    "candidate/hub-artifact-split-startup.json",
    "candidate/hub-artifact-unified-startup.json",
    "candidate/hub-artifact-settings-startup.json",
    "candidate/hub-artifact-protected-startup.json",
]
EXPECTED_KEYS = {
    "schema_version",
    "source_commit",
    "source_tree",
    "workflow_commit",
    "run_id",
    "run_attempt",
    "image_id",
    "image_revision_label",
    "image_tar_bytes",
    "image_tar_sha256",
    "smoke_outputs",
    "runtime_activated",
}


def fail(message: str) -> None:
    raise SystemExit(message)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def require_git_sha(name: str, value: str) -> None:
    if not GIT_SHA.fullmatch(value):
        fail(f"{name} must be a lowercase 40-character Git SHA")


def verify_receipt_and_archive(args: argparse.Namespace) -> dict[str, Any]:
    for name in ("source_commit", "source_tree", "workflow_commit"):
        require_git_sha(name, getattr(args, name))

    try:
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(f"could not read valid UTF-8 JSON receipt: {exc}")
    if not isinstance(receipt, dict) or set(receipt) != EXPECTED_KEYS:
        fail("receipt keys do not match the strict supported schema")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        fail("unsupported receipt schema_version")
    for key, expected in (
        ("source_commit", args.source_commit),
        ("source_tree", args.source_tree),
        ("workflow_commit", args.workflow_commit),
    ):
        if receipt[key] != expected:
            fail(f"receipt {key} differs from the operator-reviewed pin")
    if not isinstance(receipt["run_id"], str) or not re.fullmatch(r"[0-9]+", receipt["run_id"]):
        fail("receipt run_id is malformed")
    if not isinstance(receipt["run_attempt"], str) or not re.fullmatch(r"[1-9][0-9]*", receipt["run_attempt"]):
        fail("receipt run_attempt is malformed")
    if not isinstance(receipt["image_id"], str) or not IMAGE_ID.fullmatch(receipt["image_id"]):
        fail("receipt image_id must match ^sha256:[a-f0-9]{64}$")
    if receipt["image_revision_label"] != args.source_commit:
        fail("receipt image revision label differs from source commit")
    if type(receipt["image_tar_bytes"]) is not int or receipt["image_tar_bytes"] <= 0:
        fail("receipt image_tar_bytes must be a positive integer")
    if not isinstance(receipt["image_tar_sha256"], str) or not HEX_SHA256.fullmatch(receipt["image_tar_sha256"]):
        fail("receipt image_tar_sha256 must be 64 lowercase hexadecimal characters")
    if receipt["smoke_outputs"] != EXPECTED_SMOKE_OUTPUTS:
        fail("receipt smoke_outputs do not match the five required smoke evidence files")
    if receipt["runtime_activated"] is not False:
        fail("receipt must affirm runtime_activated=false")

    actual_hash, actual_size = sha256_file(args.archive)
    if actual_hash != receipt["image_tar_sha256"]:
        fail("image archive SHA-256 differs from receipt")
    if actual_size != receipt["image_tar_bytes"]:
        fail("image archive size differs from receipt")
    return receipt


def verify_loaded_image(receipt: dict[str, Any], args: argparse.Namespace) -> None:
    image_id = os.environ.get("STAGING_IMAGE_ID", "")
    if not IMAGE_ID.fullmatch(image_id):
        fail("STAGING_IMAGE_ID must match ^sha256:[a-f0-9]{64}$")
    if image_id != receipt["image_id"]:
        fail("STAGING_IMAGE_ID differs from receipt image_id")
    if not args.docker_context:
        fail("--docker-context is required in post-load phase")

    try:
        active_context = subprocess.check_output(["docker", "context", "show"], text=True).strip()
        if active_context != args.docker_context:
            fail("active Docker context differs from the reviewed --docker-context")
        inspect_text = subprocess.check_output(
            ["docker", "--context", args.docker_context, "image", "inspect", image_id],
            text=True,
        )
        inspect = json.loads(inspect_text)[0]
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, IndexError) as exc:
        fail(f"could not inspect the loaded image in the reviewed Docker context: {exc}")

    if inspect.get("Id") != image_id or inspect.get("Id") != receipt["image_id"]:
        fail("loaded Docker image ID differs from receipt and STAGING_IMAGE_ID")
    revision = inspect.get("Config", {}).get("Labels", {}).get(LABEL)
    if revision != args.source_commit or revision != receipt["image_revision_label"]:
        fail("loaded image revision label differs from source commit and receipt")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("pre-load", "post-load"), required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--workflow-commit", required=True)
    parser.add_argument("--docker-context", help="Required in post-load phase; must be the active context")
    args = parser.parse_args()

    receipt = verify_receipt_and_archive(args)
    if args.phase == "pre-load":
        if args.docker_context is not None:
            fail("--docker-context is not accepted in pre-load phase")
        print("Pre-load receipt, source pins, and streamed archive checksum verified; Docker was not invoked")
        return
    verify_loaded_image(receipt, args)
    print("Post-load receipt, archive, Docker context, image ID, and revision verified")


if __name__ == "__main__":
    main()
