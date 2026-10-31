"""Prepare a local OCI layout from an untrusted Docker image-save archive.

Integrity validation is not release provenance or deployment approval. On
failure the exclusively created output is deliberately preserved for review.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile

MAX_META = 1024 * 1024
MAX_TOTAL = 3 * 1024**3
MAX_MEMBERS = 10000
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
IMAGE = {'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}
INDEX = {'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'}
CONFIG = {'application/vnd.oci.image.config.v1+json', 'application/vnd.docker.container.image.v1+json'}
LAYER = {'application/vnd.oci.image.layer.v1.tar', 'application/vnd.oci.image.layer.v1.tar+gzip',
         'application/vnd.docker.image.rootfs.diff.tar.gzip'}


def _json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate JSON key')
            out[key] = value
        return out
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def prepare(stream, output, requested):
    if not isinstance(requested, str) or not DIGEST.fullmatch(requested):
        raise ValueError('exact manifest digest required')
    output = Path(output)
    # Parent must be controlled by the caller. Never adopt an existing directory.
    output.mkdir(mode=0o700)
    (output / 'blobs').mkdir(mode=0o700)
    (output / 'blobs' / 'sha256').mkdir(mode=0o700)
    names, sizes, meta, total = set(), {}, {}, 0
    with tarfile.open(fileobj=stream, mode='r|*') as archive:
        for number, member in enumerate(archive, 1):
            if number > MAX_MEMBERS:
                raise ValueError('invalid archive member')
            name = member.name
            p = PurePosixPath(name)
            if (name != p.as_posix() or p.is_absolute() or '..' in p.parts or '\\' in name
                    or name in names or member.size < 0):
                raise ValueError('unsafe archive member')
            names.add(name)
            # Docker's OCI export includes these directory entries; never
            # materialize arbitrary archive directories or trust their modes.
            if member.isdir() and name in ('blobs', 'blobs/sha256'):
                continue
            if not member.isfile():
                raise ValueError('invalid archive member')
            if name in ('index.json', 'oci-layout', 'manifest.json', 'repositories'):
                if member.size > MAX_META:
                    raise ValueError('metadata too large')
                meta[name] = _json(archive.extractfile(member).read())
                continue
            if not re.fullmatch(r'blobs/sha256/[0-9a-f]{64}', name):
                raise ValueError('unsupported archive member')
            total += member.size
            if total > MAX_TOTAL:
                raise ValueError('archive too large')
            digest = name.rsplit('/', 1)[1]
            source = archive.extractfile(member)
            h = hashlib.sha256()
            count = 0
            with open(output / name, 'xb') as dest:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    h.update(chunk)
                    dest.write(chunk)
            if count != member.size or h.hexdigest() != digest:
                raise ValueError('blob integrity mismatch')
            sizes['sha256:' + digest] = count
    if meta.get('oci-layout') != {'imageLayoutVersion': '1.0.0'}:
        raise ValueError('invalid OCI layout')

    def descriptor(row, accepted):
        if (type(row) is not dict or row.get('mediaType') not in accepted
                or type(row.get('digest')) is not str or not DIGEST.fullmatch(row['digest'])
                or type(row.get('size')) is not int or sizes.get(row['digest']) != row['size']):
            raise ValueError('invalid descriptor')
        return row['digest']

    def read_blob(digest):
        if sizes[digest] > MAX_META:
            raise ValueError('manifest too large')
        return _json((output / 'blobs' / 'sha256' / digest[7:]).read_bytes())

    index = meta.get('index.json')
    if type(index) is not dict or index.get('schemaVersion') != 2 or type(index.get('manifests')) is not list:
        raise ValueError('invalid index')
    # Only traverse available index branches; selected digest must be advertised
    # with linux/amd64, or be the index's single platform-unspecified image.
    selected = []
    def visit(rows, depth=0):
        if depth > 4 or len(rows) > 256:
            raise ValueError('index limit')
        for row in rows:
            if type(row) is not dict or row.get('digest') not in sizes:
                continue  # Docker saves may omit other platforms.
            kind = row.get('mediaType')
            digest = descriptor(row, IMAGE | INDEX)
            if kind in INDEX:
                child = read_blob(digest)
                if child.get('schemaVersion') != 2 or type(child.get('manifests')) is not list:
                    raise ValueError('invalid nested index')
                visit(child['manifests'], depth + 1)
            elif digest == requested:
                platform = row.get('platform')
                if platform is not None and (type(platform) is not dict or platform.get('os') != 'linux'
                                             or platform.get('architecture') != 'amd64'):
                    raise ValueError('wrong platform')
                selected.append(row)
    visit(index['manifests'])
    if len(selected) != 1:
        raise ValueError('requested manifest unavailable or ambiguous')
    chosen = selected[0]
    image = read_blob(requested)
    if image.get('schemaVersion') != 2 or image.get('mediaType') != chosen['mediaType']:
        raise ValueError('invalid image manifest')
    config_digest = descriptor(image.get('config'), CONFIG)
    config = read_blob(config_digest)
    if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
        raise ValueError('config platform mismatch')
    layers = image.get('layers')
    if type(layers) is not list or len(layers) > 256:
        raise ValueError('invalid layers')
    for layer in layers:
        descriptor(layer, LAYER)
    # Write a minimal index for the exact selected graph. Existing files never replaced.
    (output / 'oci-layout').write_bytes(b'{"imageLayoutVersion":"1.0.0"}')
    result = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
              'manifests': [chosen]}
    (output / 'index.json').write_text(json.dumps(result, sort_keys=True, separators=(',', ':')), encoding='utf-8')
    return {'manifest': requested, 'config': config_digest, 'layers': len(layers), 'blob_bytes': total}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', required=True, help='image-save tar path, or - for stdin')
    p.add_argument('--output', required=True, help='new path in a caller-controlled parent')
    p.add_argument('--manifest', required=True, help='exact linux/amd64 image manifest sha256')
    args = p.parse_args()
    import sys
    if args.archive == '-':
        print(json.dumps(prepare(sys.stdin.buffer, args.output, args.manifest)))
    else:
        with open(args.archive, 'rb') as source:
            print(json.dumps(prepare(source, args.output, args.manifest)))


if __name__ == '__main__':
    main()
