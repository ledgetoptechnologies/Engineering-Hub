"""Prepare, but never build, install, download or activate, a tracking client."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile


_REVISION = re.compile(r'[0-9a-f]{40}\Z')
_FILES = {
    'packaging/tracking-client/pyproject.toml': 'pyproject.toml',
    'packaging/tracking-client/README.md': 'README.md',
    'packaging/tracking-client/uv.lock': 'uv.lock',
    'packaging/tracking-client/requirements.txt': 'requirements.txt',
    'src/agent_message_bus/__init__.py': 'src/agent_message_bus/__init__.py',
    'src/agent_message_bus/agent_session_client.py': 'src/agent_message_bus/agent_session_client.py',
    'src/engineering_hub/__init__.py': 'src/engineering_hub/__init__.py',
    'src/engineering_hub/attention_client.py': 'src/engineering_hub/attention_client.py',
    'src/engineering_hub/tracking_client.py': 'src/engineering_hub/tracking_client.py',
    'src/engineering_hub/tracking_outbox.py': 'src/engineering_hub/tracking_outbox.py',
}


def prepare_archive(archive: bytes, *, revision: str, output_directory: Path) -> Path:
    """Prepare an allowlisted context in a new directory from a Git archive.

    A matching archive comment binds these bytes to the supplied identifier only
    if the caller independently verifies the Git objects and archive origin.
    The output parent must be private and exclusively controlled by the caller.
    """
    if type(revision) is not str or not _REVISION.fullmatch(revision):
        raise ValueError('a lowercase full commit is required')
    output = Path(output_directory)
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError('output must be a new directory with an existing parent')
    selected = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source:
        if source.pax_headers.get('comment') != revision:
            raise ValueError('archive commit does not match revision')
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or '\\' in member.name:
                raise ValueError('unsafe archive member')
            raw_name = member.name.removesuffix('/') if member.isdir() else member.name
            if raw_name != path.as_posix():
                raise ValueError('noncanonical archive member')
            target = _FILES.get(member.name)
            if target is None:
                continue
            if not member.isfile() or member.size > 2 * 1024 * 1024 or target in selected:
                raise ValueError('invalid selected archive member')
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError('missing selected archive member')
            with stream:
                selected[target] = stream.read()
    if set(selected) != set(_FILES.values()):
        raise ValueError('archive lacks required client files')
    provenance = json.dumps({
        'schema_version': 1, 'source_commit': revision,
        'source_archive_sha256': hashlib.sha256(archive).hexdigest(),
        'source_files': {name: hashlib.sha256(value).hexdigest()
                         for name, value in sorted(selected.items())},
    }, sort_keys=True, separators=(',', ':')).encode('utf-8')
    selected['src/engineering_hub/_tracking_client_source.json'] = provenance
    with tempfile.TemporaryDirectory(prefix='eh-tracking-client-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'context'
        staged.mkdir()
        for name, value in selected.items():
            destination = staged / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
            destination.chmod(0o644)
        if output.exists() or output.is_symlink():
            raise ValueError('output appeared during preparation')
        staged.rename(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output-directory', required=True, type=Path)
    args = parser.parse_args(argv)
    if not _REVISION.fullmatch(args.revision):
        parser.error('revision must be a lowercase full commit')
    archive = subprocess.run(['git', '-c', 'core.autocrlf=false', 'archive', '--format=tar', args.revision],
                             check=True, stdout=subprocess.PIPE).stdout
    prepare_archive(archive, revision=args.revision, output_directory=args.output_directory)


if __name__ == '__main__':
    main()
