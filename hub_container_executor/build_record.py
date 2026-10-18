"""Closed content-addressed build-record validation; trust remains external.

The record hash selects bytes but does not authenticate a builder. The host must
supply an operator-enrolled verifier. No tag, archive path or image pull occurs.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from engineering_hub.container_pilot_manifest import Platform, PilotManifest, SOURCE_REPOSITORY
from scripts.inspect_hub_image_identity import IdentityError, strict_json

RECORD_SCHEMA = "hub_container_build_record_v1"
RECORD_LIMIT = 64 * 1024
BLOB_LIMIT = 1024 * 1024
BLOB_TOTAL_LIMIT = 16 * 1024 * 1024
MAX_BLOBS = 64
MAX_INDEX_DEPTH = 8
SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPAQUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
CONFIG_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
RUNNABLE_TYPES = INDEX_TYPES | MANIFEST_TYPES
METADATA_TYPES = RUNNABLE_TYPES | CONFIG_TYPES


class BuildRecordError(ValueError):
    """A safe validation error without record, image or credential contents."""


class ContentStore(Protocol):
    def get(self, digest: str) -> bytes: ...


class TrustedBuildVerifier(Protocol):
    def verify_build_record(self, record_bytes: bytes, record: dict,
                            manifest: PilotManifest, role: str) -> bool: ...


@dataclass(frozen=True)
class VerifiedImage:
    role: str
    source_commit: str
    runnable_reference: str
    runnable_media_type: str
    platform_manifest_digest: str
    image_config_id: str
    platform: Platform
    image_config: dict
    record_digest: str


def _canonical(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BuildRecordError("invalid build record encoding") from None


def _no_floats(value):
    if isinstance(value, float):
        raise BuildRecordError("build record numbers must be integers")
    if isinstance(value, dict):
        for child in value.values():
            _no_floats(child)
    elif isinstance(value, list):
        for child in value:
            _no_floats(child)


def _exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise BuildRecordError(f"invalid {label} shape")


def _sha(value, label):
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise BuildRecordError(f"invalid {label} digest")
    return value


def _positive_int(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise BuildRecordError(f"invalid {label} integer")
    return value


def _load_record(raw: bytes) -> dict:
    if type(raw) is not bytes or not 1 <= len(raw) <= RECORD_LIMIT:
        raise BuildRecordError("build record is absent or exceeds its byte limit")
    try:
        record = strict_json(raw)
    except (IdentityError, RecursionError):
        raise BuildRecordError("invalid build record JSON") from None
    _no_floats(record)
    if _canonical(record) != raw:
        raise BuildRecordError("build record is not canonical")
    _exact(record, {"schema_version", "builder_id", "invocation_id", "source",
                    "platform", "selection", "metadata_blobs"}, "build record")
    if record["schema_version"] != RECORD_SCHEMA:
        raise BuildRecordError("unsupported build record schema")
    if not isinstance(record["builder_id"], str) or not OPAQUE_RE.fullmatch(record["builder_id"]):
        raise BuildRecordError("invalid enrolled builder identifier")
    try:
        invocation = record["invocation_id"]
        if not isinstance(invocation, str) or str(UUID(invocation)) != invocation:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise BuildRecordError("invalid build invocation identity") from None

    source = record["source"]
    _exact(source, {"repository", "commit", "build_context_digest",
                    "build_recipe_digest", "build_inputs_digest"}, "source")
    if source["repository"] != SOURCE_REPOSITORY:
        raise BuildRecordError("unsupported source repository")
    if not isinstance(source["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise BuildRecordError("invalid source commit")
    for field in ("build_context_digest", "build_recipe_digest", "build_inputs_digest"):
        _sha(source[field], field)

    try:
        platform = Platform.model_validate(record["platform"])
    except Exception:
        raise BuildRecordError("invalid build platform") from None
    selection = record["selection"]
    _exact(selection, {"runnable_reference", "runnable_media_type",
                       "platform_manifest_digest", "image_config_id"}, "selection")
    _sha(selection["runnable_reference"], "runnable reference")
    _sha(selection["platform_manifest_digest"], "platform manifest")
    _sha(selection["image_config_id"], "image config")
    if selection["runnable_media_type"] not in RUNNABLE_TYPES:
        raise BuildRecordError("runnable selection uses a non-runnable media type")

    entries = record["metadata_blobs"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_BLOBS:
        raise BuildRecordError("invalid metadata blob count")
    seen = set()
    total = 0
    for entry in entries:
        _exact(entry, {"digest", "media_type", "size_bytes"}, "metadata blob")
        identity = _sha(entry["digest"], "metadata blob")
        if identity in seen:
            raise BuildRecordError("duplicate metadata blob digest")
        seen.add(identity)
        if entry["media_type"] not in METADATA_TYPES:
            raise BuildRecordError("unsupported metadata media type")
        size = _positive_int(entry["size_bytes"], 1, BLOB_LIMIT, "metadata size")
        total += size
        if total > BLOB_TOTAL_LIMIT:
            raise BuildRecordError("metadata byte budget exceeded")
    return record


def _platform_matches(value, platform: Platform, *, required: bool) -> bool:
    if value is None:
        if required:
            raise BuildRecordError("selected descriptor omits platform")
        return False
    if not isinstance(value, dict) or set(value) - {"os", "architecture", "variant"}:
        raise BuildRecordError("invalid descriptor platform")
    if value.get("os") != platform.os or value.get("architecture") != platform.architecture:
        return False
    variant = value.get("variant")
    if platform.architecture == "arm64":
        if variant != platform.variant:
            return False
    elif variant not in (None, ""):
        return False
    return True


def _array_of_strings(value, label):
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BuildRecordError(f"invalid image-default {label}")
    return tuple(value)


def _verify_descriptor_chain(record: dict, platform: Platform, blob_data: dict[str, bytes],
                             blob_media: dict[str, str]) -> tuple[str, str, dict]:
    selection = record["selection"]
    root_id = selection["runnable_reference"]
    root_media = selection["runnable_media_type"]
    if blob_media.get(root_id) != root_media:
        raise BuildRecordError("runnable reference media type mismatch")
    visited_indexes = set()
    referenced = {root_id}
    candidates = []
    descriptor_count = 0

    def load(identity, media):
        if blob_media.get(identity) != media or identity not in blob_data:
            raise BuildRecordError("descriptor references missing or mismatched metadata")
        return blob_data[identity]

    def visit(identity, media, depth, ancestry, declared_platform=None, from_index=False):
        nonlocal descriptor_count
        if identity in ancestry:
            raise BuildRecordError("descriptor cycle")
        if media in INDEX_TYPES:
            if depth > MAX_INDEX_DEPTH:
                raise BuildRecordError("index nesting exceeds limit")
            if identity in visited_indexes:
                raise BuildRecordError("duplicate index reference")
            visited_indexes.add(identity)
            try:
                index = strict_json(load(identity, media))
            except (IdentityError, RecursionError):
                raise BuildRecordError("invalid image index bytes") from None
            if (not isinstance(index, dict) or set(index) - {"schemaVersion", "mediaType", "manifests"}
                    or type(index.get("schemaVersion")) is not int or index.get("schemaVersion") != 2
                    or index.get("mediaType", media) != media or not isinstance(index.get("manifests"), list)):
                raise BuildRecordError("unsupported image index layout")
            next_ancestry = ancestry | {identity}
            for descriptor in index["manifests"]:
                descriptor_count += 1
                if descriptor_count > MAX_BLOBS:
                    raise BuildRecordError("descriptor traversal exceeds limit")
                if not isinstance(descriptor, dict):
                    raise BuildRecordError("invalid index descriptor")
                _exact(descriptor, {"mediaType", "digest", "size"} if "platform" not in descriptor
                       else {"mediaType", "digest", "size", "platform"}, "index descriptor")
                child_id = _sha(descriptor["digest"], "descriptor")
                child_media = descriptor["mediaType"]
                if child_media not in INDEX_TYPES | MANIFEST_TYPES:
                    raise BuildRecordError("unsupported runnable descriptor media type")
                size = _positive_int(descriptor["size"], 1, BLOB_LIMIT, "descriptor size")
                raw = load(child_id, child_media)
                if len(raw) != size:
                    raise BuildRecordError("descriptor size mismatch")
                if child_id in referenced:
                    raise BuildRecordError("duplicate descriptor reference")
                referenced.add(child_id)
                descriptor_platform = descriptor.get("platform")
                if descriptor_platform is not None:
                    if not isinstance(descriptor_platform, dict):
                        raise BuildRecordError("invalid descriptor platform")
                    _exact(descriptor_platform, {"os", "architecture", "variant"}
                           if "variant" in descriptor_platform else {"os", "architecture"},
                           "descriptor platform")
                    if (not isinstance(descriptor_platform.get("os"), str)
                            or not isinstance(descriptor_platform.get("architecture"), str)
                            or ("variant" in descriptor_platform
                                and not isinstance(descriptor_platform["variant"], str))):
                        raise BuildRecordError("invalid descriptor platform values")
                if child_media in INDEX_TYPES and descriptor_platform is not None:
                    raise BuildRecordError("nested index platform selectors are unsupported")
                if child_media in MANIFEST_TYPES and descriptor_platform is None:
                    raise BuildRecordError("platform manifest descriptor omits platform")
                visit(child_id, child_media, depth + 1, next_ancestry,
                      descriptor_platform, from_index=True)
        elif media in MANIFEST_TYPES:
            try:
                image_manifest = strict_json(load(identity, media))
            except (IdentityError, RecursionError):
                raise BuildRecordError("invalid image manifest bytes") from None
            if (not isinstance(image_manifest, dict)
                    or set(image_manifest) - {"schemaVersion", "mediaType", "config", "layers"}
                    or type(image_manifest.get("schemaVersion")) is not int
                    or image_manifest.get("schemaVersion") != 2
                    or image_manifest.get("mediaType", media) != media
                    or not isinstance(image_manifest.get("layers"), list)):
                raise BuildRecordError("unsupported image manifest layout")
            config_descriptor = image_manifest.get("config")
            _exact(config_descriptor, {"mediaType", "digest", "size"}, "config descriptor")
            config_id = _sha(config_descriptor["digest"], "config descriptor")
            config_media = config_descriptor["mediaType"]
            if config_media not in CONFIG_TYPES:
                raise BuildRecordError("unsupported image config media type")
            size = _positive_int(config_descriptor["size"], 1, BLOB_LIMIT, "config descriptor size")
            config_raw = load(config_id, config_media)
            if len(config_raw) != size:
                raise BuildRecordError("config descriptor size mismatch")
            if config_id in referenced:
                raise BuildRecordError("duplicate config reference")
            referenced.add(config_id)
            try:
                config = strict_json(config_raw)
            except (IdentityError, RecursionError):
                raise BuildRecordError("invalid image config bytes") from None
            if (not isinstance(config, dict) or not isinstance(config.get("os"), str)
                    or not isinstance(config.get("architecture"), str)):
                raise BuildRecordError("invalid image config platform")
            config_variant = config.get("variant")
            if config_variant is not None and not isinstance(config_variant, str):
                raise BuildRecordError("invalid image config variant")
            if from_index:
                if declared_platform is None:
                    raise BuildRecordError("indexed platform manifest omits platform")
                declared_variant = declared_platform.get("variant")
                if (declared_platform["os"] != config["os"]
                        or declared_platform["architecture"] != config["architecture"]
                        or declared_variant != config_variant):
                    raise BuildRecordError("descriptor/config platform or variant mismatch")
                descriptor_matches_target = _platform_matches(declared_platform, platform, required=True)
            else:
                descriptor_matches_target = True
            config_platform_matches = (config.get("os") == platform.os
                                       and config.get("architecture") == platform.architecture
                                       and (config_variant == platform.variant if platform.architecture == "arm64"
                                            else config_variant in (None, "")))
            defaults = config.get("config")
            if defaults is not None and not isinstance(defaults, dict):
                raise BuildRecordError("invalid image config defaults")
            defaults = defaults or {}
            entrypoint = _array_of_strings(defaults.get("Entrypoint"), "entrypoint")
            command = _array_of_strings(defaults.get("Cmd"), "command")
            env = _array_of_strings(defaults.get("Env"), "environment")
            env_names = []
            for item in env:
                if "=" not in item or not item.split("=", 1)[0]:
                    raise BuildRecordError("invalid image-default environment")
                env_names.append(item.split("=", 1)[0])
            if len(env_names) != len(set(env_names)):
                raise BuildRecordError("duplicate image-default environment key")
            if config_platform_matches and descriptor_matches_target:
                candidates.append((identity, config_id, config, entrypoint, command, env))
        else:
            raise BuildRecordError("unsupported descriptor role")

    visit(root_id, root_media, 0, frozenset())
    record_blob_ids = set(blob_data)
    if referenced != record_blob_ids:
        raise BuildRecordError("unreferenced or missing metadata blob")
    if len(candidates) != 1:
        raise BuildRecordError("platform image selection is absent or ambiguous")
    manifest_id, config_id, image_config, _entrypoint, _command, _env = candidates[0]
    if manifest_id != selection["platform_manifest_digest"]:
        raise BuildRecordError("platform manifest identity mismatch")
    if selection["runnable_media_type"] in MANIFEST_TYPES and (
            selection["runnable_reference"] != selection["platform_manifest_digest"]):
        raise BuildRecordError("direct manifest selection identity mismatch")
    if config_id != selection["image_config_id"]:
        raise BuildRecordError("config identity does not match build record")
    return manifest_id, config_id, image_config


def verify_image_record(manifest: PilotManifest, role: str, record_store: ContentStore,
                        metadata_store: ContentStore,
                        verifier: TrustedBuildVerifier | None) -> VerifiedImage:
    """Verify one manifest-selected role; a missing trusted verifier always denies."""
    if role not in {"candidate", "rollback"}:
        raise BuildRecordError("invalid image role")
    if verifier is None or not callable(getattr(verifier, "verify_build_record", None)):
        raise BuildRecordError("trusted build verifier is required")
    selection = getattr(manifest, role)
    record_digest = selection.provenance.attestation_digest
    try:
        record_raw = record_store.get(record_digest)
    except Exception:
        raise BuildRecordError("build record is unavailable") from None
    if (type(record_raw) is not bytes
            or "sha256:" + hashlib.sha256(record_raw).hexdigest() != record_digest):
        raise BuildRecordError("attestation digest does not match record bytes")
    record = _load_record(record_raw)
    source = record["source"]
    if (source["repository"] != selection.source_repository
            or source["commit"] != selection.source_commit
            or source["build_context_digest"] != selection.provenance.build_context_digest
            or source["build_recipe_digest"] != selection.provenance.build_recipe_digest
            or source["build_inputs_digest"] != selection.provenance.build_inputs_digest):
        raise BuildRecordError("build source inputs do not match the manifest")
    try:
        platform = Platform.model_validate(record["platform"])
    except Exception:
        raise BuildRecordError("invalid platform") from None
    if platform != manifest.runner_binding.platform:
        raise BuildRecordError("build platform does not match runner binding")
    if record["selection"]["image_config_id"] != selection.image_config_id:
        raise BuildRecordError("config identity does not match manifest role")

    blob_data = {}
    blob_media = {}
    for entry in record["metadata_blobs"]:
        identity = entry["digest"]
        try:
            raw = metadata_store.get(identity)
        except Exception:
            raise BuildRecordError("metadata blob is unavailable") from None
        if type(raw) is not bytes or len(raw) != entry["size_bytes"] or len(raw) > BLOB_LIMIT:
            raise BuildRecordError("metadata blob size mismatch")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != identity:
            raise BuildRecordError("metadata blob digest mismatch")
        blob_data[identity] = raw
        blob_media[identity] = entry["media_type"]

    manifest_id, config_id, image_config = _verify_descriptor_chain(
        record, platform, blob_data, blob_media
    )
    if config_id != selection.image_config_id:
        raise BuildRecordError("config identity does not match manifest role")
    try:
        approved = verifier.verify_build_record(record_raw, record, manifest, role)
    except Exception:
        raise BuildRecordError("trusted build verifier failed") from None
    if type(approved) is not bool or not approved:
        raise BuildRecordError("trusted build verifier did not approve record")
    return VerifiedImage(role=role, source_commit=selection.source_commit,
                         runnable_reference=record["selection"]["runnable_reference"],
                         runnable_media_type=record["selection"]["runnable_media_type"],
                         platform_manifest_digest=manifest_id,
                         image_config_id=config_id, platform=platform,
                         image_config=image_config, record_digest=record_digest)
