"""Bounded, read-only local Docker archive identity inspection (not provenance).

No image/container mutations, archive extraction, or configuration output. Layer
payloads are skipped, not authenticated: this verifies metadata identity only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import threading
from typing import BinaryIO

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_ARCHIVE = 8 * 1024**3
MAX_METADATA = 1024**2
MAX_METADATA_TOTAL = 16 * 1024**2
MAX_ENTRIES = 20000
DEADLINE = 90
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"}


class IdentityError(ValueError):
    pass


def digest(value: object) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise IdentityError("Invalid immutable SHA-256 reference")
    return value


def strict_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise IdentityError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid(_):
        raise IdentityError("Non-finite JSON number")
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise IdentityError("Invalid archive JSON") from exc


def archive_metadata(stream: BinaryIO) -> dict[str, bytes]:
    """Parse ordinary Docker-save tar entries without extracting any paths."""
    total = 0
    metadata_total = 0
    entries: dict[str, bytes] = {}
    seen = set()

    def read(size):
        nonlocal total
        if total + size > MAX_ARCHIVE:
            raise IdentityError("Archive byte limit exceeded")
        chunks = []
        remaining = size
        while remaining:
            part = stream.read(min(remaining, 65536))
            if not part:
                raise IdentityError("Truncated archive")
            chunks.append(part)
            remaining -= len(part)
            total += len(part)
        return b"".join(chunks)

    for _ in range(MAX_ENTRIES):
        header = read(512)
        if header == bytes(512):
            if read(512) != bytes(512):
                raise IdentityError("Invalid tar terminator")
            # Drain trailing padding to avoid a blocked producer; reject data.
            while True:
                part = stream.read(65536)
                if not part:
                    return entries
                total += len(part)
                if total > MAX_ARCHIVE or any(part):
                    raise IdentityError("Invalid trailing archive data")
        try:
            checksum = int(header[148:156].strip(b"\0 "), 8)
            size = int(header[124:136].strip(b"\0 ") or b"0", 8)
            name = header[:100].split(b"\0", 1)[0].decode("utf-8")
            prefix = header[345:500].split(b"\0", 1)[0].decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise IdentityError("Unsupported tar header") from exc
        if checksum != sum(header[:148]) + 8 * 32 + sum(header[156:]):
            raise IdentityError("Invalid tar checksum")
        if prefix:
            name = prefix + "/" + name
        name = name.removeprefix("./").rstrip("/")
        if not name or name.startswith("/") or "\\" in name or any(p in {"", ".", ".."} for p in name.split("/")):
            raise IdentityError("Unsafe archive path")
        if name in seen:
            raise IdentityError("Duplicate archive entry")
        seen.add(name)
        kind = header[156:157]
        if kind not in {b"0", b"\0", b"5"} or size < 0:
            raise IdentityError("Unsupported archive entry")
        if kind == b"5" and size:
            raise IdentityError("Invalid directory entry")
        metadata = kind != b"5" and (name.endswith(".json") or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name))
        if metadata and size <= MAX_METADATA:
            metadata_total += size
            if metadata_total > MAX_METADATA_TOTAL:
                raise IdentityError("Metadata budget exceeded")
            entries[name] = read(size)
        else:
            if name.endswith(".json") and size > MAX_METADATA:
                raise IdentityError("Metadata entry too large")
            remaining = size
            while remaining:
                amount = min(remaining, 65536)
                read(amount)
                remaining -= amount
        read((-size) % 512)
    raise IdentityError("Archive entry limit exceeded")


def inspect_metadata(entries: dict[str, bytes], image: str, platform: str) -> dict[str, str]:
    image = digest(image)
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise IdentityError("Unsupported platform")
    os_name, arch = platform.split("/")

    def blob(descriptor):
        if not isinstance(descriptor, dict):
            raise IdentityError("Invalid descriptor")
        identity = digest(descriptor.get("digest"))
        size = descriptor.get("size")
        if type(size) is not int or size < 0 or size > MAX_METADATA:
            raise IdentityError("Invalid metadata descriptor size")
        data = entries.get("blobs/sha256/" + identity[7:])
        if data is None or len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != identity:
            raise IdentityError("Missing or mismatched metadata blob")
        return strict_json(data)

    if "index.json" in entries:
        root = strict_json(entries["index.json"])
        candidates = []
        visited = 0

        def walk(index, chain, depth):
            nonlocal visited
            if depth > 8 or not isinstance(index, dict) or type(index.get("schemaVersion")) is not int or index.get("schemaVersion") != 2 or not isinstance(index.get("manifests"), list):
                raise IdentityError("Invalid OCI index")
            for descriptor in index["manifests"]:
                visited += 1
                if visited > MAX_ENTRIES or not isinstance(descriptor, dict):
                    raise IdentityError("Descriptor traversal budget exceeded")
                identity = digest(descriptor.get("digest"))
                media = descriptor.get("mediaType")
                if not isinstance(media, str):
                    raise IdentityError("Invalid descriptor media type")
                item = blob(descriptor)
                path = chain + [identity]
                if media in INDEX_TYPES:
                    walk(item, path, depth + 1)
                elif media in MANIFEST_TYPES:
                    if not isinstance(item, dict) or type(item.get("schemaVersion")) is not int or item.get("schemaVersion") != 2:
                        raise IdentityError("Invalid image manifest")
                    declared = descriptor.get("platform")
                    if declared is not None and not isinstance(declared, dict):
                        raise IdentityError("Invalid descriptor platform")
                    # BuildKit attestations use unknown/unknown and are not
                    # runnable platform candidates. Do not mistake their
                    # ordinary image-config defaults for an amd64 image.
                    if declared and (declared.get("os"), declared.get("architecture")) != (os_name, arch):
                        continue
                    config_desc = item.get("config")
                    if not isinstance(config_desc, dict) or not isinstance(config_desc.get("mediaType"), str) or config_desc["mediaType"] not in CONFIG_TYPES:
                        raise IdentityError("Invalid config media type")
                    config = blob(config_desc)
                    if not isinstance(config, dict):
                        raise IdentityError("Invalid image config")
                    if declared and (declared.get("os") != config.get("os") or declared.get("architecture") != config.get("architecture")):
                        raise IdentityError("Descriptor/config platform mismatch")
                    if config.get("os") == os_name and config.get("architecture") == arch and image in path:
                        candidates.append((identity, digest(config_desc.get("digest")), path))
                else:
                    raise IdentityError("Unsupported OCI descriptor media type")

        walk(root, [], 0)
        if len(candidates) != 1:
            raise IdentityError("Image/platform selection is absent or ambiguous")
        manifest, config, path = candidates[0]
        return {"selection_reference": image, "archive_format": "oci", "platform": platform, "platform_manifest_digest": manifest, "config_digest": config, "selected_root_digest": path[0]}

    manifest = strict_json(entries.get("manifest.json", b"null"))
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise IdentityError("Ambiguous Docker-compatible archive")
    entry = manifest[0]
    name = entry.get("Config") if isinstance(entry, dict) else None
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}\.json", name):
        raise IdentityError("Unsupported Docker config reference")
    config_id = "sha256:" + name[:-5]
    raw = entries.get(name)
    if raw is None or "sha256:" + hashlib.sha256(raw).hexdigest() != config_id or image != config_id:
        raise IdentityError("Docker config identity mismatch")
    config = strict_json(raw)
    if not isinstance(config, dict) or config.get("os") != os_name or config.get("architecture") != arch:
        raise IdentityError("Config platform mismatch")
    return {"selection_reference": image, "archive_format": "docker-compatible", "platform": platform, "config_digest": config_id}


def inspect_local(image: str, platform: str) -> dict[str, str]:
    digest(image)
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise IdentityError("Unsupported platform")
    # No shell, user options, context changes, host archive, or stderr disclosure.
    # --platform rewrites the exported index and drops the requested index blob
    # on Docker 29. Export all locally present metadata to prove the full chain.
    process = subprocess.Popen(["docker", "image", "save", image], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    expired = threading.Event()

    def cancel():
        expired.set()
        try:
            process.kill()
        except OSError:
            pass  # The producer may have exited concurrently with the timer.

    timer = threading.Timer(DEADLINE, cancel)
    timer.daemon = True
    timer.start()
    try:
        if process.stdout is None:
            raise IdentityError("Docker archive stream unavailable")
        entries = archive_metadata(process.stdout)
        code = process.wait(timeout=5)
        if expired.is_set() or code:
            raise IdentityError("Docker save failed or exceeded deadline")
        return inspect_metadata(entries, image, platform)
    finally:
        timer.cancel()
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", choices=["linux/amd64", "linux/arm64"], required=True)
    args = parser.parse_args()
    try:
        report = inspect_local(args.image, args.platform)
    except (IdentityError, OSError, subprocess.SubprocessError):
        parser.exit(1, "Image metadata identity verification failed; no archive contents disclosed.\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
