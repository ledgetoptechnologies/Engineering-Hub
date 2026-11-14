"""Bind a registry manifest and pulled image to the locally tested config ID."""
import argparse
import hashlib
import json
import re
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
MEDIA = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


def verify(raw: bytes, descriptor: object, expected_config: str, pulled_config: str | None = None) -> str:
    if not DIGEST.fullmatch(expected_config) or not 0 < len(raw) <= 1024 * 1024:
        raise ValueError("Invalid expected image ID or manifest size")
    if pulled_config is not None and pulled_config != expected_config:
        raise ValueError("Pulled image differs from tested local image")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    manifest = json.loads(raw)
    if (not isinstance(descriptor, dict) or descriptor.get("digest") != digest
            or not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2
            or manifest.get("mediaType") not in MEDIA
            or not isinstance(manifest.get("config"), dict)
            or manifest["config"].get("digest") != expected_config
            or not isinstance(manifest.get("layers"), list) or not manifest["layers"]):
        raise ValueError("Registry manifest does not bind the tested image")
    return digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--expected-config", required=True)
    parser.add_argument("--pulled-config")
    args = parser.parse_args()
    print(verify(args.manifest.read_bytes(), json.loads(args.descriptor.read_bytes()),
                 args.expected_config, args.pulled_config))


if __name__ == "__main__":
    main()
