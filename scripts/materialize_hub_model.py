"""Materialize the reviewed public model revision for the pinned image build.

The pinned hashes establish byte identity, not independent publisher provenance.
No credentials are sent. An unavailable or changed upstream fails the build.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen

REVISION = 'aa8f8b060edb00e03bfdd08813a2949946c8ba55'
BASE = f'https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q/resolve/{REVISION}/'
MANIFEST_SHA = 'f40477aa8dd45b6000f16892cf3aa6ecfa310e3965baa5fded84fbc2147a203f'
MAX_BYTES = 128 * 1024 * 1024
MAX_SECONDS = 300


def materialize(manifest: Path, destination: Path):
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA:
        raise ValueError('reviewed model manifest changed')
    data = json.loads(raw)
    expected = {'config.json', 'model_optimized.onnx', 'special_tokens_map.json',
                'tokenizer.json', 'tokenizer_config.json'}
    if (data.get('schema') != 'engineering-hub.model-snapshot.v1'
            or data.get('model') != 'BAAI/bge-small-en-v1.5'
            or set(data.get('files', {})) != expected):
        raise ValueError('unexpected model manifest')
    # Destination must be a new job-owned directory, never an existing cache.
    destination.mkdir(mode=0o700)
    model = destination / 'model'
    model.mkdir(mode=0o700)
    (destination / 'manifest.json').write_bytes(raw)
    deadline = time.monotonic() + MAX_SECONDS
    for name in sorted(expected):
        digest = hashlib.sha256()
        size = 0
        request = Request(BASE + name, headers={'User-Agent': 'engineering-hub-build/1'})
        with urlopen(request, timeout=20) as response, (model / name).open('xb') as output:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError('model download deadline exceeded')
                # read1 returns after at most one underlying read, so a peer
                # sending a trickle cannot reset the overall deadline forever.
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('model file too large')
                digest.update(chunk)
                output.write(chunk)
        if not size or digest.hexdigest() != data['files'][name]:
            raise ValueError('model file digest mismatch')
    print('Reviewed model snapshot materialized and hashed')


def run_bounded(argv, timeout=MAX_SECONDS):
    """Bound the whole worker, including DNS, redirects and response headers.

    subprocess.run kills and reaps the worker on timeout. The destination is
    retained as incomplete and must never be reused after an uncertain result.
    The worker receives no GitHub token or other inherited credentials.
    """
    allowed = ('SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'PROGRAMDATA', 'TEMP', 'TMP')
    clean_env = {name: os.environ[name] for name in allowed if name in os.environ}
    return subprocess.run(argv, env=clean_env, timeout=timeout, check=True)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        materialize(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 3:
        run_bounded([sys.executable, '-I', str(Path(__file__).resolve()),
                     '--worker', *sys.argv[1:]])
    else:
        raise SystemExit('usage: materialize_hub_model.py MANIFEST NEW_DESTINATION')
