"""Bounded offline wheel consistency check; expected artifact pins are external.

Embedded source metadata is not build approval or source attestation. This
command never extracts, installs, imports wheel code, or contacts a registry.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import stat
import zipfile


MAX_WHEEL_BYTES = 8 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
DIST = 'engineering_hub_container_executor-0.1.0.dist-info'
PROVENANCE = 'hub_container_executor/_executor_source.json'
REQUIRED = frozenset('hub_container_executor/' + name + '.py' for name in (
    '__init__', 'service', 'local_resources', 'runtime_identity', 'installed_runner',
    'installed_coordinator', 'launch_supervisor', 'configuration_commitment',
    'launch_transport')) | {'scripts/inspect_hub_image_identity.py', PROVENANCE,
                            DIST + '/METADATA', DIST + '/WHEEL', DIST + '/RECORD'}


def _require(value):
    if not value:
        raise ValueError('executor wheel verification failed')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _json(raw):
    def reject(_):
        raise ValueError('executor wheel verification failed')
    return json.loads(raw, object_pairs_hook=_unique, parse_float=reject,
                      parse_constant=reject)


def verify_wheel(*, wheel, sha256, source_commit, core_image):
    """Check bytes against operator-supplied pins, not authority inferred in-wheel."""
    fd = None
    try:
        _require(type(sha256) is str and re.fullmatch('[0-9a-f]{64}', sha256))
        _require(type(source_commit) is str and re.fullmatch('[0-9a-f]{40}', source_commit))
        _require(type(core_image) is str and len(core_image) <= 512 and re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}', core_image))
        fd = os.open(wheel, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_WHEEL_BYTES)
        data = bytearray()
        while len(data) <= MAX_WHEEL_BYTES:
            part = os.read(fd, min(65536, MAX_WHEEL_BYTES + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        _require(len(data) == info.st_size and hashlib.sha256(data).hexdigest() == sha256)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            _require(0 < len(infos) <= 1024 and sum(row.file_size for row in infos)
                     <= MAX_UNCOMPRESSED_BYTES)
            contents = {}
            for row in infos:
                name = row.filename
                path = PurePosixPath(name)
                _require(name == path.as_posix() and not path.is_absolute()
                         and '..' not in path.parts and '\\' not in name
                         and '\x00' not in name and not row.is_dir()
                         and name not in contents and not row.flag_bits & 1
                         and 0 <= row.file_size <= MAX_MEMBER_BYTES
                         and not stat.S_ISLNK(row.external_attr >> 16))
                allowed = ((len(path.parts) == 2 and path.parts[0] == 'hub_container_executor'
                            and (path.suffix == '.py' or name == PROVENANCE))
                           or name == 'scripts/inspect_hub_image_identity.py'
                           or (len(path.parts) == 2 and path.parts[0] == DIST
                               and path.name in {'METADATA', 'WHEEL', 'RECORD', 'top_level.txt'}))
                _require(allowed)
                contents[name] = archive.read(row)
                _require(len(contents[name]) == row.file_size)
        _require(REQUIRED <= contents.keys())
        provenance = _json(contents[PROVENANCE])
        _require(type(provenance) is dict and set(provenance) == {
            'schema_version', 'source_commit', 'source_archive_sha256', 'source_files'}
            and type(provenance['schema_version']) is int and provenance['schema_version'] == 1
            and provenance['source_commit'] == source_commit
            and type(provenance['source_archive_sha256']) is str
            and re.fullmatch('[0-9a-f]{64}', provenance['source_archive_sha256']))
        sources = provenance['source_files']
        _require(type(sources) is dict and 1 <= len(sources) <= 1024)
        python_names = set()
        for name, digest in sources.items():
            _require(type(digest) is str and re.fullmatch('[0-9a-f]{64}', digest))
            if name.startswith('src/') and name.endswith('.py'):
                installed = name.removeprefix('src/')
                _require(installed in contents and hashlib.sha256(contents[installed]).hexdigest() == digest)
                python_names.add(installed)
            else:
                _require(name in {'pyproject.toml', 'README.md'})
        _require(python_names == {name for name in contents if name.endswith('.py')})
        record_name = DIST + '/RECORD'
        rows = list(csv.reader(io.StringIO(contents[record_name].decode('utf-8'))))
        recorded = set()
        for row in rows:
            _require(len(row) == 3)
            name, digest, size = row
            _require(name in contents and name not in recorded)
            recorded.add(name)
            if name == record_name:
                _require(digest == size == '')
            else:
                expected = base64.urlsafe_b64encode(hashlib.sha256(contents[name]).digest()).rstrip(b'=').decode()
                _require(digest == 'sha256=' + expected and size == str(len(contents[name])))
        _require(recorded == contents.keys())
        metadata = contents[DIST + '/METADATA'].decode('utf-8').splitlines()
        for header, expected in (
            ('name:', 'Name: engineering-hub-container-executor'),
            ('version:', 'Version: 0.1.0'),
            ('requires-dist:', 'Requires-Dist: agent-message-bus==0.6.0'),
        ):
            _require([line for line in metadata if line.lower().startswith(header)] == [expected])
        return {'source_commit': source_commit, 'wheel_sha256': sha256,
                'core_image': core_image, 'python_files_verified': len(python_names)}
    except Exception:
        raise ValueError('executor wheel verification failed') from None
    finally:
        if fd is not None:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--wheel', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--core-image', required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(verify_wheel(**vars(args)), sort_keys=True))
        return 0
    except ValueError:
        parser.exit(2, 'executor wheel verification failed\n')


if __name__ == '__main__':
    raise SystemExit(main())
