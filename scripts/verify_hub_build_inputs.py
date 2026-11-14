"""Verify explicitly supplied Hub build inputs without fetching or building.

The model directory must be a quiescent, independently protected, materialized,
regular-file-only snapshot. This is not a fence against concurrent writers. A
trusted external reviewer supplies the exact manifest digest and node image
reference; this tool does not discover either value, attest origin, or change
the Dockerfile. A successful check is input consistency, not build acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

MAX_MANIFEST = 256 * 1024
MAX_FILES = 256
MAX_TOTAL = 2 * 1024 * 1024 * 1024
HASH = re.compile(r"[0-9a-f]{64}\Z")
NODE = re.compile(r"(?:docker\.io/)?library/node@sha256:[0-9a-f]{64}\Z")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate manifest key')
        result[key] = value
    return result


def read_manifest(path: Path) -> bytes:
    """Read a bounded regular manifest without following a final symlink."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('regular bounded manifest required')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_MANIFEST:
            raise ValueError('regular bounded manifest required')
        data = bytearray()
        while len(data) <= MAX_MANIFEST:
            chunk = os.read(fd, min(65536, MAX_MANIFEST + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != info.st_size or len(data) > MAX_MANIFEST:
            raise ValueError('regular bounded manifest required')
        return bytes(data)
    finally:
        os.close(fd)


def verify_inputs(*, node_image: str, manifest: bytes, manifest_sha256: str, model_dir: Path):
    if type(node_image) is not str or not NODE.fullmatch(node_image):
        raise ValueError('independently pinned Node image required')
    if (type(manifest) is not bytes or not 0 < len(manifest) <= MAX_MANIFEST
            or type(manifest_sha256) is not str or not HASH.fullmatch(manifest_sha256)
            or hashlib.sha256(manifest).hexdigest() != manifest_sha256):
        raise ValueError('independent model manifest pin required')
    doc = json.loads(manifest, object_pairs_hook=_unique,
                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
    if (type(doc) is not dict or set(doc) != {'schema', 'model', 'files'}
            or doc['schema'] != 'engineering-hub.model-snapshot.v1'
            or doc['model'] != 'BAAI/bge-small-en-v1.5'
            or type(doc['files']) is not dict or not 1 <= len(doc['files']) <= MAX_FILES):
        raise ValueError('invalid model manifest')
    expected = doc['files']
    for name, digest in expected.items():
        path = PurePosixPath(name)
        if (type(name) is not str or not name or '\\' in name or path.is_absolute()
                or path.as_posix() != name or any(part in ('.', '..') for part in path.parts)
                or type(digest) is not str or not HASH.fullmatch(digest)):
            raise ValueError('invalid model file entry')
    if not {'model_optimized.onnx', 'tokenizer.json', 'config.json'} <= set(expected):
        raise ValueError('required model files missing')
    root = Path(model_dir)
    if not root.is_dir() or root.is_symlink():
        raise ValueError('regular snapshot directory required')
    actual, total = set(), 0
    def walk_error(error):
        raise ValueError('model snapshot traversal failed') from error
    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        base = Path(current)
        for entry in dirs:
            if (base / entry).is_symlink():
                raise ValueError('model links forbidden')
        for entry in files:
            path = base / entry
            relative = path.relative_to(root).as_posix()
            if relative not in expected or relative in actual:
                raise ValueError('unexpected model file')
            if path.is_symlink():
                raise ValueError('model links forbidden')
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('non-regular model file')
                total += info.st_size
                if total > MAX_TOTAL:
                    raise ValueError('model size limit exceeded')
                digest = hashlib.sha256()
                while True:
                    data = os.read(fd, 1024 * 1024)
                    if not data:
                        break
                    digest.update(data)
                if digest.hexdigest() != expected[relative]:
                    raise ValueError('model file digest mismatch')
            finally:
                os.close(fd)
            actual.add(relative)
    if actual != set(expected):
        raise ValueError('model file missing')
    return {'node_image': node_image, 'manifest_sha256': manifest_sha256,
            'model': doc['model'], 'files': len(actual), 'bytes': total}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node-image', required=True)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--model-dir', required=True, type=Path)
    args = parser.parse_args()
    result = verify_inputs(node_image=args.node_image, manifest=read_manifest(args.manifest),
                           manifest_sha256=args.manifest_sha256, model_dir=args.model_dir)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
