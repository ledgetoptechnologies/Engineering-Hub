"""Prepare a local OCI layout from an untrusted Docker image-save archive.

Integrity validation is not release provenance or deployment approval. On
failure the exclusively created output is deliberately preserved for review.
Layer decompression, diff IDs, and runtime behavior are not validated.
The caller must exclusively control the output parent and layout throughout
preparation and after return. Checks are not atomic against same-UID writers.
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


class _BoundedReader:
    def __init__(self, stream):
        self.stream, self.remaining = stream, MAX_TOTAL + 64 * 1024**2

    def read(self, size=-1):
        if size < 0 or size > self.remaining:
            raise ValueError('raw archive limit')
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data


class _BoundedTarInfo(tarfile.TarInfo):
    def _proc_pax(self, archive):
        if self.size > MAX_META:
            raise ValueError('archive extension too large')
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        if self.size > MAX_META:
            raise ValueError('archive extension too large')
        return super()._proc_gnulong(archive)


def _json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate JSON key')
            out[key] = value
        return out
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _prepare(stream, output, requested, fds):
    if not isinstance(requested, str) or not DIGEST.fullmatch(requested):
        raise ValueError('exact manifest digest required')
    output = Path(output)
    # Parent must be controlled by the caller. Never adopt an existing directory.
    output.mkdir(mode=0o700)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(output, flags)
    fds.append(root_fd)
    os.mkdir('blobs', 0o700, dir_fd=root_fd)
    blob_fd = os.open('blobs', flags, dir_fd=root_fd)
    fds.append(blob_fd)
    os.mkdir('sha256', 0o700, dir_fd=blob_fd)
    sha_fd = os.open('sha256', flags, dir_fd=blob_fd)
    fds.append(sha_fd)
    root_identity = os.fstat(root_fd)
    def stable():
        named = os.stat(output, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (root_identity.st_dev, root_identity.st_ino):
            raise ValueError('output directory replaced')
        for parent, name, child in ((root_fd, 'blobs', blob_fd), (blob_fd, 'sha256', sha_fd)):
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            actual = os.fstat(child)
            if (named.st_dev, named.st_ino) != (actual.st_dev, actual.st_ino):
                raise ValueError('output directory replaced')
    names, sizes, meta, total = set(), {}, {}, 0
    with tarfile.open(fileobj=_BoundedReader(stream), mode='r|*', tarinfo=_BoundedTarInfo) as archive:
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
            stable()
            fd = os.open(digest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=sha_fd)
            with os.fdopen(fd, 'wb') as dest:
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

    def verify_blob(digest, *, collect=False):
        stable()
        fd = os.open(digest[7:], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=sha_fd)
        try:
            before = os.fstat(fd)
            named = os.stat(digest[7:], dir_fd=sha_fd, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino):
                raise ValueError('blob replaced')
            h, count, content = hashlib.sha256(), 0, bytearray()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > sizes[digest]:
                    raise ValueError('blob changed')
                h.update(chunk)
                if collect:
                    content.extend(chunk)
            after = os.fstat(fd)
            named = os.stat(digest[7:], dir_fd=sha_fd, follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if (identity(before) != identity(after) or identity(after) != identity(named)
                    or count != sizes[digest] or h.hexdigest() != digest[7:]):
                raise ValueError('blob changed')
            return bytes(content)
        finally:
            os.close(fd)

    def read_blob(digest):
        if sizes[digest] > MAX_META:
            raise ValueError('manifest too large')
        return _json(verify_blob(digest, collect=True))

    index = meta.get('index.json')
    if type(index) is not dict or index.get('schemaVersion') != 2 or type(index.get('manifests')) is not list:
        raise ValueError('invalid index')
    # Only traverse available index branches; selected digest must be advertised
    # with linux/amd64, or be the index's single platform-unspecified image.
    selected = []
    def visit(rows, depth=0, ancestors=()):
        if depth > 4 or len(rows) > 256:
            raise ValueError('index limit')
        for row in rows:
            if type(row) is not dict or row.get('digest') not in sizes:
                continue  # Docker saves may omit other platforms.
            kind = row.get('mediaType')
            digest = descriptor(row, IMAGE | INDEX)
            platform = row.get('platform')
            if platform is not None and (type(platform) is not dict or platform.get('os') != 'linux'
                                         or platform.get('architecture') != 'amd64'):
                if digest == requested:
                    raise ValueError('wrong platform')
                continue
            if kind in INDEX:
                child = read_blob(digest)
                if child.get('schemaVersion') != 2 or type(child.get('manifests')) is not list:
                    raise ValueError('invalid nested index')
                visit(child['manifests'], depth + 1, ancestors + (len(rows),))
            elif digest == requested:
                if platform is None and (len(rows) != 1 or any(n != 1 for n in ancestors)):
                    raise ValueError('ambiguous unspecified platform')
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
    # Repeat every selected graph blob check immediately before publishing the
    # index; exclusive caller control remains required after this check.
    for digest in {requested, config_digest, *(layer['digest'] for layer in layers)}:
        verify_blob(digest)
    # Write a minimal index for the exact selected graph. Existing files never replaced.
    stable()
    def write_exclusive(name, data):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
        with os.fdopen(fd, 'wb') as target:
            target.write(data)
    write_exclusive('oci-layout', b'{"imageLayoutVersion":"1.0.0"}')
    result = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
              'manifests': [chosen]}
    stable()
    write_exclusive('index.json', json.dumps(result, sort_keys=True, separators=(',', ':')).encode())
    stable()
    for digest in {requested, config_digest, *(layer['digest'] for layer in layers)}:
        verify_blob(digest)
    return {'manifest': requested, 'config': config_digest, 'layers': len(layers), 'blob_bytes': total}


def prepare(stream, output, requested):
    fds = []
    try:
        return _prepare(stream, output, requested, fds)
    finally:
        for fd in reversed(fds):
            os.close(fd)


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
