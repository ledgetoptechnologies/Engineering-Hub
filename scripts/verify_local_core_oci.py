"""Check an independently pinned, protected local OCI core layout.

This is a read-only preflight, not a builder attestation, a concurrency fence,
or evidence that an executor actually consumed the layout. The entire input
directory must remain protected and quiescent through build and verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

SHA = re.compile(r'sha256:[a-f0-9]{64}\Z')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]*@(sha256:[a-f0-9]{64})\Z')
COMMIT = re.compile(r'[a-f0-9]{40}\Z')
INDEX = {'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'}
MANIFEST = {'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}
JSON_LIMIT = 4 * 1024 * 1024
LAYER_LIMIT = 4 * 1024 * 1024 * 1024
TOTAL_LIMIT = 8 * 1024 * 1024 * 1024
DESCRIPTOR_LIMIT = 256


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate JSON key')
        obj[key] = value
    return obj


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))


def _read_file(path, limit, expected_digest=None, expected_size=None, *, retain=True):
    if path.is_symlink():
        raise ValueError('symbolic links forbidden')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit
                or (expected_size is not None and info.st_size != expected_size)):
            raise ValueError('invalid OCI blob size or type')
        count, digest, chunks = 0, hashlib.sha256(), []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > limit:
                raise ValueError('OCI blob size limit exceeded')
            digest.update(chunk)
            if retain:
                chunks.append(chunk)
        if (count != info.st_size or (expected_digest is not None
                and 'sha256:' + digest.hexdigest() != expected_digest)):
            raise ValueError('OCI blob differs from descriptor')
        return b''.join(chunks) if retain else count
    finally:
        os.close(fd)


def _descriptor(value):
    if (type(value) is not dict or type(value.get('digest')) is not str
            or not SHA.fullmatch(value['digest']) or type(value.get('size')) is not int
            or value['size'] <= 0 or type(value.get('mediaType')) is not str):
        raise ValueError('invalid OCI descriptor')
    return value


def verify_layout(*, layout: Path, core_image: str, context_digest: str, source_commit: str):
    match = REF.fullmatch(core_image) if type(core_image) is str else None
    if (not match or len(core_image) > 512 or type(context_digest) is not str or not SHA.fullmatch(context_digest)
            or match.group(1) != context_digest or type(source_commit) is not str
            or not COMMIT.fullmatch(source_commit)):
        raise ValueError('exact independently selected core binding required')
    root = Path(layout)
    if not root.is_absolute():
        raise ValueError('absolute OCI layout path required')
    for directory in (root, root / 'blobs', root / 'blobs' / 'sha256'):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('protected OCI directory required')
    version = _json(_read_file(root / 'oci-layout', JSON_LIMIT))
    if type(version) is not dict or version.get('imageLayoutVersion') != '1.0.0':
        raise ValueError('invalid OCI layout version')
    index = _json(_read_file(root / 'index.json', JSON_LIMIT))
    if type(index) is not dict or index.get('schemaVersion') != 2:
        raise ValueError('invalid OCI index')
    remaining = [(index, 0)]
    seen, candidates, count = set(), [], 0
    while remaining:
        document, depth = remaining.pop()
        entries = document.get('manifests')
        if type(entries) is not list or depth > 8:
            raise ValueError('invalid OCI index graph')
        for item in entries:
            count += 1
            if count > DESCRIPTOR_LIMIT:
                raise ValueError('OCI descriptor limit exceeded')
            item = _descriptor(item)
            digest = item['digest']
            if item['mediaType'] in INDEX:
                if digest in seen:
                    raise ValueError('duplicate OCI index')
                seen.add(digest)
                raw = _read_file(root / 'blobs' / 'sha256' / digest[7:], JSON_LIMIT,
                                 digest, item['size'])
                child = _json(raw)
                if type(child) is not dict or child.get('schemaVersion') != 2:
                    raise ValueError('invalid nested OCI index')
                remaining.append((child, depth + 1))
            elif digest == context_digest and item['mediaType'] in MANIFEST:
                platform = item.get('platform')
                if platform is not None and (type(platform) is not dict
                        or platform.get('os') != 'linux' or platform.get('architecture') != 'amd64'):
                    raise ValueError('core manifest platform differs')
                candidates.append(item)
    if len(candidates) != 1:
        raise ValueError('exact core manifest not uniquely present')
    item = candidates[0]
    manifest = _json(_read_file(root / 'blobs' / 'sha256' / context_digest[7:], JSON_LIMIT,
                                context_digest, item['size']))
    if type(manifest) is not dict or manifest.get('schemaVersion') != 2:
        raise ValueError('invalid core image manifest')
    config_desc = _descriptor(manifest.get('config'))
    if config_desc['mediaType'] not in {'application/vnd.oci.image.config.v1+json',
                                         'application/vnd.docker.container.image.v1+json'}:
        raise ValueError('unsupported core config media type')
    config = _json(_read_file(root / 'blobs' / 'sha256' / config_desc['digest'][7:], JSON_LIMIT,
                              config_desc['digest'], config_desc['size']))
    cfg = config.get('config') if type(config) is dict else None
    if (type(cfg) is not dict or config.get('os') != 'linux' or config.get('architecture') != 'amd64'
            or cfg.get('User') != '568:568' or type(cfg.get('Labels')) is not dict
            or cfg['Labels'].get('org.opencontainers.image.revision') != source_commit):
        raise ValueError('core config identity mismatch')
    layers = manifest.get('layers')
    if type(layers) is not list or not 1 <= len(layers) <= 128:
        raise ValueError('invalid core image layers')
    total = 0
    for layer in layers:
        layer = _descriptor(layer)
        if total + layer['size'] > TOTAL_LIMIT:
            raise ValueError('total layer size limit exceeded')
        total += _read_file(root / 'blobs' / 'sha256' / layer['digest'][7:], LAYER_LIMIT,
                            layer['digest'], layer['size'], retain=False)
    return {'schema': 'engineering-hub.local-core-oci-check.v1', 'core_image': core_image,
            'context_digest': context_digest, 'config_digest': config_desc['digest'],
            'source_commit': source_commit, 'platform': 'linux/amd64',
            'layers': len(layers), 'layer_bytes': total}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--layout', required=True, type=Path)
    parser.add_argument('--core-image', required=True)
    parser.add_argument('--context-digest', required=True)
    parser.add_argument('--source-commit', required=True)
    args = parser.parse_args()
    print(json.dumps(verify_layout(layout=args.layout, core_image=args.core_image,
                                  context_digest=args.context_digest,
                                  source_commit=args.source_commit), sort_keys=True))


if __name__ == '__main__':
    main()
