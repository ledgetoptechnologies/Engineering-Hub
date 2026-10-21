"""Explicit local-observer build-record drafts: never trust or enrollment.

Metadata export is an opt-in read-only local Docker-save operation. No pulls,
builds, runtime, signing, SSH, credential/config discovery or file writes occur.
Callers preserve raw evidence and publish drafts into a new directory themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import types
from uuid import UUID

from engineering_hub.container_pilot_manifest import Platform, SOURCE_REPOSITORY
from scripts import inspect_hub_image_identity as identity


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def native_validators():
    """Load the exact pure native validator file without package __init__/fcntl.

    No validator substitution or mock Linux module is installed. The module's
    raw digest is exposed alongside output so the caller can pin what ran.
    """
    path = Path(__file__).parents[1] / "hub_container_executor/build_record.py"
    raw = path.read_bytes()
    name = "_eh_native_record_" + hashlib.sha256(raw).hexdigest()
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        try:
            exec(compile(raw, str(path), "exec"), module.__dict__)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module, sha(raw)


@dataclass(frozen=True)
class RecordDraft:
    record: bytes
    inputs: bytes
    native_validator_digest: str


def prepare_record(*, source_commit: str, context_digest: str, recipe_digest: str,
                   observations: dict, manifest_raw: bytes, config_raw: bytes,
                   preparation_id: str) -> RecordDraft:
    """Canonical native shape/chain validation only; no trusted verifier exists.

    preparation_id is newly generated for preparation, not a fabricated original
    Docker/CI build invocation. Application commit and separate recipe provenance
    must remain truthful in observations; the caller supplies actual evidence.
    """
    if str(UUID(preparation_id)) != preparation_id or type(observations) is not dict:
        raise ValueError("Explicit preparation identity and public observations required")
    inputs = canonical({
        "schema": "hub_local_observer_unenrolled_inputs_v1",
        "invocation_id_basis": "record_preparation_uuid_not_original_build_invocation",
        "preparation_id": preparation_id,
        "source_commit": source_commit,
        "observations": observations,
        "assurance": "local_observer_only_not_signed_ci_or_builder_trust",
        "trusted_or_enrolled": False,
    })
    manifest, config = identity.strict_json(manifest_raw), identity.strict_json(config_raw)
    manifest_digest, config_digest = sha(manifest_raw), sha(config_raw)
    if (type(manifest) is not dict or type(config) is not dict
            or manifest.get("config", {}).get("digest") != config_digest
            or manifest.get("config", {}).get("size") != len(config_raw)):
        raise ValueError("Raw selected metadata chain mismatch")
    record = {
        "schema_version": "hub_container_build_record_v1",
        "builder_id": "codex-local-observer-unenrolled-v1",
        "invocation_id": preparation_id,
        "source": {"repository": SOURCE_REPOSITORY, "commit": source_commit,
                   "build_context_digest": context_digest,
                   "build_recipe_digest": recipe_digest,
                   "build_inputs_digest": sha(inputs)},
        "platform": {"os": "linux", "architecture": "amd64", "variant": None},
        "selection": {"runnable_reference": manifest_digest,
                      "runnable_media_type": manifest.get("mediaType"),
                      "platform_manifest_digest": manifest_digest,
                      "image_config_id": config_digest},
        "metadata_blobs": [
            {"digest": manifest_digest, "media_type": manifest.get("mediaType"),
             "size_bytes": len(manifest_raw)},
            {"digest": config_digest, "media_type": manifest["config"].get("mediaType"),
             "size_bytes": len(config_raw)}],
    }
    raw = canonical(record)
    native, native_digest = native_validators()
    parsed = native._load_record(raw)
    native._verify_descriptor_chain(parsed, Platform.model_validate(record["platform"]),
        {manifest_digest: manifest_raw, config_digest: config_raw},
        {item["digest"]: item["media_type"] for item in record["metadata_blobs"]})
    return RecordDraft(raw, inputs, native_digest)


def export_selection(*, index: str, manifest: str, config: str):
    """Bounded owned local metadata producer, not layer-content verification."""
    for value in (index, manifest, config):
        identity.digest(value)
    process = subprocess.Popen(["docker", "image", "save", index],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    expired = threading.Event()
    def timeout():
        expired.set()
        if process.poll() is None:
            process.kill()  # This owned export producer only, never a container.
    timer = threading.Timer(identity.DEADLINE, timeout)
    timer.daemon = True
    timer.start()
    try:
        entries = identity.archive_metadata(process.stdout)
        code = process.wait(timeout=5)
        if code or expired.is_set():
            raise ValueError("Local read-only metadata export failed or timed out")
        report = identity.inspect_metadata(entries, index, "linux/amd64")
        if (report.get("archive_format") != "oci"
                or report.get("platform_manifest_digest") != manifest
                or report.get("config_digest") != config):
            raise ValueError("Selected index/manifest/config does not match supplied pins")
        manifest_raw = entries["blobs/sha256/" + manifest[7:]]
        config_raw = entries["blobs/sha256/" + config[7:]]
        if sha(manifest_raw) != manifest or sha(config_raw) != config:
            raise ValueError("Raw metadata digest mismatch")
        return report, manifest_raw, config_raw
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
