"""Pure bounded Docker-save OCI content verification for the TEST index fault.

Inputs are caller-owned streams and public digest pins, never Docker commands.
Every blob is hashed; every supported layer also has its uncompressed DiffID
verified. Only bounded JSON metadata and the explicitly selected fault layer
are retained. This is content equivalence, not builder/network provenance,
permission to deploy, health acceptance or proof that no registry was contacted.
The caller must impose a transport timeout on reads of an untrusted stream.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import re
import zlib

from scripts.inspect_hub_image_identity import (
    CONFIG_TYPES, MANIFEST_TYPES, IdentityError, digest, inspect_metadata, strict_json,
)
from scripts.prepare_hub_rollback_fault import _FAULT_SCRIPT


MAX_ARCHIVE = 8 * 1024**3
MAX_EXPANDED_LAYER = 1024**3
MAX_EXPANDED_TOTAL = 3 * 1024**3
MAX_METADATA = 1024**2
MAX_METADATA_TOTAL = 16 * 1024**2
MAX_FAULT_LAYER = 1024**2
MAX_FAULT_EXPANDED = 8 * 1024**2
_RAW = {"application/vnd.oci.image.layer.v1.tar"}
_GZIP = {"application/vnd.oci.image.layer.v1.tar+gzip",
         "application/vnd.docker.image.rootfs.diff.tar.gzip"}


def _require(condition):
    if not condition:
        raise IdentityError("TEST fault content contract mismatch")


class _Reader:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.total = stream, limit, 0

    def read(self, size):
        raw = self.stream.read(size)
        _require(type(raw) is bytes and len(raw) <= size)
        self.total += len(raw)
        _require(self.total <= self.limit)
        return raw

    def exact(self, size):
        parts = []
        while size:
            part = self.read(min(size, 65536))
            _require(bool(part))
            parts.append(part)
            size -= len(part)
        return b"".join(parts)


def _tar(reader):
    """Yield ordinary file/directory headers only; caller consumes each body."""
    seen = set()
    for _ in range(20000):
        header = reader.exact(512)
        if header == bytes(512):
            _require(reader.exact(512) == bytes(512))
            while True:
                trailing = reader.read(65536)
                if not trailing:
                    return
                _require(not any(trailing))
        try:
            checksum = int(header[148:156].strip(b"\0 "), 8)
            size = int(header[124:136].strip(b"\0 ") or b"0", 8)
            mode = int(header[100:108].strip(b"\0 ") or b"0", 8)
            uid = int(header[108:116].strip(b"\0 ") or b"0", 8)
            gid = int(header[116:124].strip(b"\0 ") or b"0", 8)
            name = header[:100].split(b"\0", 1)[0].decode("utf-8")
            prefix = header[345:500].split(b"\0", 1)[0].decode("utf-8")
        except (ValueError, UnicodeError):
            raise IdentityError("Invalid bounded tar header") from None
        _require(checksum == sum(header[:148]) + 8 * 32 + sum(header[156:]))
        if prefix:
            name = prefix + "/" + name
        name = name.removeprefix("./").rstrip("/")
        _require(name and not name.startswith("/") and "\\" not in name
                 and all(part not in {"", ".", ".."} for part in name.split("/"))
                 and name not in seen and size >= 0 and 0 <= mode <= 0o7777
                 and 0 <= uid < 2**32 and 0 <= gid < 2**32)
        seen.add(name)
        kind = header[156:157]
        _require(kind in {b"0", b"\0", b"5"}
                 and not any(header[157:257]) and (kind != b"5" or size == 0))
        yield name, kind, size, mode, uid, gid
        _require(not any(reader.exact((-size) % 512)))
    raise IdentityError("Tar entry budget exceeded")


@dataclass(frozen=True)
class _Blob:
    size: int
    raw_digest: str
    gzip_diff_id: str | None


@dataclass
class _Archive:
    metadata: dict[str, bytes]
    blobs: dict[str, _Blob]
    fault: bytes | None


def _archive(stream, *, fault_digest=None):
    reader = _Reader(stream, MAX_ARCHIVE)
    metadata, blobs, fault = {}, {}, None
    retained, expanded_total = 0, 0
    for name, kind, size, _mode, _uid, _gid in _tar(reader):
        if kind == b"5":
            continue
        is_blob = re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", name)
        capture_fault = is_blob and "sha256:" + is_blob[1] == fault_digest
        _require(not capture_fault or size <= MAX_FAULT_LAYER)
        capture_small = size <= MAX_METADATA
        raw_hash, diff_hash = hashlib.sha256(), hashlib.sha256()
        decoder, gzip_mode, expanded = None, None, 0
        chunks, remaining = [], size
        while remaining:
            raw = reader.exact(min(remaining, 65536))
            remaining -= len(raw)
            raw_hash.update(raw)
            if capture_small or capture_fault:
                chunks.append(raw)
            if gzip_mode is None:
                gzip_mode = raw.startswith(b"\x1f\x8b")
                if gzip_mode:
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            if gzip_mode:
                pending = raw
                try:
                    while pending:
                        decoded = decoder.decompress(pending, 65536)
                        pending = decoder.unconsumed_tail
                        expanded += len(decoded)
                        expanded_total += len(decoded)
                        _require(expanded <= MAX_EXPANDED_LAYER
                                 and expanded_total <= MAX_EXPANDED_TOTAL)
                        diff_hash.update(decoded)
                        _require(not decoder.unused_data)
                except zlib.error:
                    raise IdentityError("Invalid gzip layer") from None
            else:
                expanded_total += len(raw)
                _require(expanded_total <= MAX_EXPANDED_TOTAL)
        identity = "sha256:" + raw_hash.hexdigest()
        if is_blob:
            _require(identity == "sha256:" + is_blob[1])
            _require(not gzip_mode or decoder.eof)
            blobs[identity] = _Blob(size, identity,
                "sha256:" + diff_hash.hexdigest() if gzip_mode else None)
        body = b"".join(chunks)
        if capture_fault:
            fault = body
        if capture_small:
            # Layer bytes are not retained as metadata merely because small.
            try:
                parsed = strict_json(body)
            except IdentityError:
                parsed = None
            if name in {"index.json", "oci-layout"} or is_blob and type(parsed) is dict:
                retained += len(body)
                _require(retained <= MAX_METADATA_TOTAL)
                metadata[name] = body
    _require(strict_json(metadata.get("oci-layout", b"null")) == {"imageLayoutVersion": "1.0.0"})
    return _Archive(metadata, blobs, fault)


def _selection(archive, reference, platform):
    selected = inspect_metadata(archive.metadata, reference, platform)
    _require(selected["archive_format"] == "oci")
    def load(identity):
        raw = archive.metadata.get("blobs/sha256/" + identity[7:])
        _require(raw is not None)
        return strict_json(raw)
    manifest = load(selected["platform_manifest_digest"])
    config = load(selected["config_digest"])
    _require(type(manifest) is dict and type(config) is dict
             and manifest.get("mediaType") in MANIFEST_TYPES
             and manifest.get("config", {}).get("mediaType") in CONFIG_TYPES)
    layers = manifest.get("layers")
    rootfs = config.get("rootfs")
    _require(type(layers) is list and 0 < len(layers) <= 256
             and type(rootfs) is dict and set(rootfs) == {"type", "diff_ids"}
             and rootfs["type"] == "layers" and type(rootfs["diff_ids"]) is list
             and len(rootfs["diff_ids"]) == len(layers))
    for descriptor, expected_diff in zip(layers, rootfs["diff_ids"]):
        _require(type(descriptor) is dict and set(descriptor) == {"mediaType", "digest", "size"})
        identity = digest(descriptor["digest"])
        blob = archive.blobs.get(identity)
        _require(blob is not None and type(descriptor["size"]) is int
                 and descriptor["size"] == blob.size and blob.size <= MAX_EXPANDED_LAYER)
        media = descriptor["mediaType"]
        _require(media in _RAW | _GZIP)
        actual_diff = blob.raw_digest if media in _RAW else blob.gzip_diff_id
        _require(actual_diff is not None and digest(expected_diff) == actual_diff)
    return selected, manifest, config


def _fault_layer(raw, media):
    _require(raw is not None)
    if media in _GZIP:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = decoder.decompress(raw, MAX_FAULT_EXPANDED + 1)
        except zlib.error:
            raise IdentityError("Invalid fault gzip") from None
        _require(len(raw) <= MAX_FAULT_EXPANDED and decoder.eof
                 and not decoder.unconsumed_tail and not decoder.unused_data)
    _require(len(raw) <= MAX_FAULT_EXPANDED)
    reader, whiteouts = _Reader(io.BytesIO(raw), MAX_FAULT_EXPANDED), 0
    for name, kind, size, mode, uid, gid in _tar(reader):
        _require(uid == gid == 0)
        if kind == b"5":
            _require(name in {"app", "app/dashboard"} and mode == 0o755)
        else:
            _require(name == "app/dashboard/.wh.index.html" and size == 0 and mode in {0, 0o644})
            whiteouts += 1
        _require(size == 0)
    _require(whiteouts == 1)


def _history(base, child):
    before, after = base.get("history"), child.get("history")
    _require(type(before) is list and type(after) is list and after[:len(before)] == before)
    extra = after[len(before):]
    _require(len(extra) == 4)
    for value in (base, child):
        if "created" in value:
            _timestamp(value["created"])
    command = ["/opt/venv/bin/python", "-I", "-B", "-c", _FAULT_SCRIPT, "/app/dashboard"]
    runs = {"RUN " + " ".join(command) + " # buildkit",
            "RUN " + json.dumps(command, separators=(",", ":")) + " # buildkit"}
    expected = [("USER 0:0", True), (runs, False), ("USER 568:568", True),
                ("LABEL engineering-hub.test-only=rollback-missing-index-v1", True)]
    for entry, (created_by, empty) in zip(extra, expected):
        _require(type(entry) is dict and set(entry) <= {"created", "created_by", "empty_layer", "comment"}
                 and entry.get("empty_layer", False) is empty
                 and type(entry.get("created_by")) is str
                 and entry.get("created_by") in (created_by if type(created_by) is set else {created_by})
                 and entry.get("comment", "buildkit.dockerfile.v0") == "buildkit.dockerfile.v0")
        if "created" in entry:
            _timestamp(entry["created"])


def _timestamp(value):
    _require(type(value) is str and len(value) <= 64
             and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|\+00:00)", value))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise IdentityError("Invalid build timestamp") from None
    _require(parsed.utcoffset() == timezone.utc.utcoffset(parsed))


def verify_fault_archive(base_stream, fault_stream, *, base_reference: str,
                         fault_reference: str, expected_fault_layer: str,
                         platform: str, base_source_commit: str) -> dict:
    """Validate the full selected content chain; no filesystem/runtime effects."""
    for value in (base_reference, fault_reference, expected_fault_layer):
        digest(value)
    _require(type(base_source_commit) is str and re.fullmatch("[0-9a-f]{40}", base_source_commit))
    base = _archive(base_stream)
    fault = _archive(fault_stream, fault_digest=expected_fault_layer)
    base_selected, bm, bc = _selection(base, base_reference, platform)
    child_selected, cm, cc = _selection(fault, fault_reference, platform)
    _require(cm["layers"][:-1] == bm["layers"] and len(cm["layers"]) == len(bm["layers"]) + 1
             and cm["layers"][-1]["digest"] == expected_fault_layer
             and cc["rootfs"]["diff_ids"][:-1] == bc["rootfs"]["diff_ids"])
    left, right = dict(bc), dict(cc)
    for key in ("rootfs", "history", "created"):
        left.pop(key, None)
        right.pop(key, None)
    _require(type(left.get("config")) is dict and type(right.get("config")) is dict)
    left_config, right_config = dict(left.pop("config")), dict(right.pop("config"))
    _require(type(left_config.get("Labels")) is dict and type(right_config.get("Labels")) is dict)
    left_labels, right_labels = dict(left_config.pop("Labels")), dict(right_config.pop("Labels"))
    _require(left_config == right_config and left == right
             and left_config.get("User") == "568:568"
             and left_labels.get("org.opencontainers.image.revision") == base_source_commit
             and "engineering-hub.test-only" not in left_labels
             and right_labels.pop("engineering-hub.test-only", None) == "rollback-missing-index-v1"
             and right_labels == left_labels)
    _history(bc, cc)
    _fault_layer(fault.fault, cm["layers"][-1]["mediaType"])
    return {"schema": "hub_test_fault_content_v1", "test_only": True,
            "base": base_selected, "fault": child_selected,
            "fault_layer_digest": expected_fault_layer,
            "fault_diff_id": cc["rootfs"]["diff_ids"][-1],
            "verified_fault": "missing_dashboard_index", "runtime_acceptance": False,
            "registry_contact": "not_established", "builder_provenance": "not_established"}
