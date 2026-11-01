#!/usr/bin/env python3
"""Build an inactive static application gate on an explicitly provisioned Linux toolchain.

The commit is caller-declared; the record contains measured hashes, not verified
source provenance or a complete toolchain attestation. The operator must
independently establish the key, source and compiler trust.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile

SOURCES = ('main.c', 'json.c', 'wire.c', 'identity.c', 'enrollment.c', 'decision.c')
HEADERS = ('json.h', 'wire.h', 'identity.h', 'enrollment.h', 'decision.h')


def validate_elf_reports(header, program, dynamic):
    """Reject ELF interpreters, dynamic segments and dynamic dependencies."""
    if ('ELF64' not in header or 'Advanced Micro Devices X86-64' not in header
            or re.search(r'^\s*(?:INTERP|DYNAMIC)\s', program, re.MULTILINE)
            or '(NEEDED)' in dynamic):
        raise RuntimeError('unexpected executable architecture or dynamic dependencies')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--public-key-hex', required=True)
    parser.add_argument('--source-commit', required=True, help='declared revision; independently verify against source hashes')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--channel-version', choices=('2', '3'), default='2',
                        help='2 retains legacy inherited FD; 3 uses the enrolled fixed socket path')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{64}', args.public_key_hex):
        parser.error('public key must be exactly 64 lowercase hexadecimal characters')
    if not re.fullmatch(r'[0-9a-f]{40}', args.source_commit):
        parser.error('source commit must be exactly 40 lowercase hexadecimal characters')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        parser.error('this build supports Linux x86-64 only')
    out = args.output_dir.resolve(strict=True)
    if not out.is_dir() or any(out.iterdir()):
        parser.error('output directory must already exist and be empty')
    root = Path(__file__).resolve().parents[1]
    source = root / 'hub_container_executor' / 'static_gate'
    files = {name: (source / name).read_bytes() for name in SOURCES + HEADERS}
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    gcc = shutil.which('gcc'); readelf = shutil.which('readelf'); linker = shutil.which('ld')
    if not gcc or not readelf or not linker:
        parser.error('gcc, readelf and ld are required')
    compiler = subprocess.run([gcc, '--version'], check=True, capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
    linker_version = subprocess.run([linker, '--version'], check=True, capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
    crypto_path = Path(subprocess.run([gcc, '-print-file-name=libcrypto.a'], check=True, capture_output=True,
                                      text=True, timeout=10).stdout.strip()).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='hub-static-gate-build-') as temp:
        stage = Path(temp)
        for name, content in files.items(): (stage / name).write_bytes(content)
        binary = stage / 'gate'
        cmd = [gcc, '-static', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
               '-DGATE_PUBLIC_KEY_HEX="' + args.public_key_hex + '"',
               '-DGATE_CHANNEL_VERSION=' + args.channel_version,
               *(str(stage / name) for name in SOURCES), '-lcrypto', '-ldl', '-pthread', '-o', str(binary)]
        subprocess.run(cmd, check=True, timeout=120)
        header = subprocess.run([readelf, '-h', str(binary)], check=True, capture_output=True, text=True, timeout=10).stdout
        program = subprocess.run([readelf, '-l', str(binary)], check=True, capture_output=True, text=True, timeout=10).stdout
        dynamic = subprocess.run([readelf, '-d', str(binary)], check=True, capture_output=True, text=True, timeout=10).stdout
        validate_elf_reports(header, program, dynamic)
        data = binary.read_bytes()
        record = {'schema': 1, 'public_key_hex': args.public_key_hex, 'architecture': 'linux/amd64',
                  'channel_version': int(args.channel_version),
                  'binary_sha256': hashlib.sha256(data).hexdigest(), 'compiler': compiler,
                  'compiler_sha256': hashlib.sha256(Path(gcc).resolve().read_bytes()).hexdigest(),
                  'linker': linker_version, 'linker_sha256': hashlib.sha256(Path(linker).resolve().read_bytes()).hexdigest(),
                  'libcrypto_sha256': hashlib.sha256(crypto_path.read_bytes()).hexdigest(),
                  'declared_source_commit': args.source_commit,
                  'sources_sha256': hashes, 'static_elf_check': True}
        # Exclusive creation keeps a prior artifact from being silently replaced.
        with (out / 'gate').open('xb') as stream: stream.write(data)
        os.chmod(out / 'gate', 0o555)
        with (out / 'gate-build.json').open('x', encoding='utf-8') as stream:
            json.dump(record, stream, sort_keys=True, separators=(',', ':')); stream.write('\n')
        print(json.dumps(record, sort_keys=True))


if __name__ == '__main__': main()
