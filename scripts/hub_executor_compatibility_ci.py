"""Disposable CI wheel/core compatibility evidence, never deployment."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from build_hub_executor_wheel import prepare_archive
from verify_hub_executor_wheel import verify_wheel
from hub_executor_image_smoke import smoke, docker_command, engine_identity

PACKAGE = 'ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:'
BACKEND_SHA = '51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670'


class CommandFailure(RuntimeError):
    def __init__(self, returncode):
        super().__init__('Compatibility subprocess failed')
        self.returncode = returncode if type(returncode) is int and -255 <= returncode <= 255 else None


def failure(exc, stage):
    # Fixed vocabulary only: never exception text, argv, stdout or stderr.
    kind = ('command_failed' if isinstance(exc, CommandFailure) else
            'command_timeout' if isinstance(exc, subprocess.TimeoutExpired) else
            'io_failed' if isinstance(exc, OSError) else
            'validation_failed' if isinstance(exc, ValueError) else 'unexpected_failure')
    return {'stage': stage, 'kind': kind,
            'exit_code': exc.returncode if isinstance(exc, CommandFailure) else None}


def inputs(env):
    fields = {'SOURCE_COMMIT': 40, 'CORE_COMMIT': 40, 'CORE_DIGEST': 64,
              'CORE_CONFIG': 64, 'WHEEL_SHA256': 64}
    for key, length in fields.items():
        if not re.fullmatch('[0-9a-f]{%d}' % length, env.get(key, '')):
            raise ValueError('Invalid immutable input: ' + key)
    if not re.fullmatch('[0-9]{10}', env.get('SOURCE_DATE_EPOCH', '')):
        raise ValueError('Explicit ten-digit reproducibility epoch required')
    if env.get('GITHUB_REF') != 'refs/heads/main' or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        raise ValueError('Manual main-only invocation required')
    return {key: env[key] for key in (*fields, 'SOURCE_DATE_EPOCH')}


def run(*args, env=None, timeout=180):
    if args and args[0] == 'docker':
        args, docker_env = docker_command(args[1:])
        if env is not None:
            raise ValueError('Explicit Docker environment override forbidden')
        env = docker_env
    result = subprocess.run(args, capture_output=True, env=env, timeout=timeout)
    if result.returncode:
        # Never reflect arbitrary image, registry, pip or configuration output.
        raise CommandFailure(result.returncode)
    return result.stdout


def git(*args):
    return run('git', '-c', 'core.autocrlf=false', *args)


def prepare(values, directory, evidence=None):
    evidence = evidence if evidence is not None else {}
    evidence['stage'] = 'source_ancestry'
    git('merge-base', '--is-ancestor', values['SOURCE_COMMIT'], 'HEAD')
    evidence['stage'] = 'source_archive'
    archive = git('archive', '--format=tar', values['SOURCE_COMMIT'])
    evidence['source_archive_sha256'] = hashlib.sha256(archive).hexdigest()
    evidence['source_archive_bytes'] = len(archive)
    evidence['stage'] = 'source_prepare'
    source = prepare_archive(archive, revision=values['SOURCE_COMMIT'],
                             output_directory=directory / 'source')
    context = directory / 'context'
    evidence['stage'] = 'context_prepare'
    context.mkdir()
    for original, destination in [('docker/Dockerfile.hub-executor', 'Dockerfile'),
                                 ('scripts/verify_hub_executor_wheel.py', 'validate_executor_wheel.py')]:
        (context / destination).write_bytes(git('cat-file', 'blob', values['SOURCE_COMMIT'] + ':' + original))
    return source, context


def wheel(values, directory, evidence=None):
    evidence = evidence if evidence is not None else {}
    evidence['stage'] = 'toolchain_metadata'
    evidence['python_version'] = '.'.join(str(v) for v in sys.version_info[:3])
    pip_version = importlib.metadata.version('pip')
    if not re.fullmatch('[A-Za-z0-9.+_-]{1,64}', pip_version):
        raise ValueError('Invalid pip version metadata')
    evidence['pip_version'] = pip_version
    source, context = prepare(values, directory, evidence)
    evidence['stage'] = 'backend_prepare'
    backend = directory / 'backend'
    backend.mkdir()
    requirement = directory / 'backend-requirements.txt'
    requirement.write_text('setuptools==84.0.0 --hash=sha256:' + BACKEND_SHA + '\n')
    evidence['stage'] = 'backend_download'
    run(sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check',
        'download', '--index-url', 'https://pypi.org/simple', '--require-hashes',
        '--only-binary=:all:', '--no-deps', '-r', str(requirement), '-d', str(backend))
    artifact = backend / 'setuptools-84.0.0-py3-none-any.whl'
    evidence['stage'] = 'backend_verify'
    evidence['observed_backend_sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    evidence['observed_backend_bytes'] = artifact.stat().st_size
    if evidence['observed_backend_sha256'] != BACKEND_SHA:
        raise ValueError('Backend bytes mismatch')
    target = directory / 'backend-site'
    evidence['stage'] = 'backend_install'
    run(sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check',
        'install', '--no-index', '--no-deps', '--no-compile', '--target', str(target), str(artifact))
    destination = directory / 'wheels'
    environment = {**os.environ, 'PYTHONPATH': str(target.resolve()),
                   'PYTHONDONTWRITEBYTECODE': '1', 'SOURCE_DATE_EPOCH': values['SOURCE_DATE_EPOCH']}
    evidence['stage'] = 'wheel_build'
    run(sys.executable, '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check',
        'wheel', '--no-deps', '--no-index', '--no-build-isolation', '--no-cache-dir',
        '--wheel-dir', str(destination), str(source), env=environment)
    evidence['stage'] = 'wheel_select'
    built, = destination.glob('*.whl')
    evidence['observed_wheel_bytes'] = built.stat().st_size
    if not 0 < evidence['observed_wheel_bytes'] <= 8 * 1024**2:
        raise ValueError('Wheel size out of bounds')
    evidence['observed_wheel_sha256'] = hashlib.sha256(built.read_bytes()).hexdigest()
    evidence['stage'] = 'wheel_verify'
    verification = verify_wheel(wheel=str(built), sha256=values['WHEEL_SHA256'],
                                source_commit=values['SOURCE_COMMIT'], core_image=PACKAGE + values['CORE_DIGEST'])
    evidence['stage'] = 'context_complete'
    (context / 'executor.whl').write_bytes(built.read_bytes())
    return {**verification, 'wheel_bytes': built.stat().st_size,
            'backend_sha256': BACKEND_SHA, 'python_version': evidence['python_version'],
            'pip_version': evidence['pip_version'],
            'context_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in context.iterdir()}}


def compatibility(values, directory, evidence=None):
    evidence = evidence if evidence is not None else {}
    evidence['stage'] = 'core_inspect'
    reference = PACKAGE + values['CORE_DIGEST']
    info, = json.loads(run('docker', 'image', 'inspect', reference))
    evidence['stage'] = 'core_identity_verify'
    config = info['Config']
    if (info['Id'] != 'sha256:' + values['CORE_CONFIG'] or reference not in info['RepoDigests']
            or config['Labels'].get('org.opencontainers.image.revision') != values['CORE_COMMIT']
            or config['User'] != '568:568'):
        raise ValueError('Pulled immutable core identity mismatch')
    # Fixed installed metadata only; no config/resource/launcher construction.
    code = "from engineering_hub.build_info import read_build_commit; import sys; assert read_build_commit()==sys.argv[1]; print('installed-core-identity-passed')"
    evidence['stage'] = 'core_installed_identity'
    output = run('docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
        '--read-only', '--user', '568:568', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--cpus', '1', '--memory', '256m', '--memory-swap', '256m', '--pids-limit', '64',
        '--entrypoint', '/opt/venv/bin/python', info['Id'], '-I', '-B', '-c', code, values['CORE_COMMIT'])
    if output.strip() != b'installed-core-identity-passed':
        raise ValueError('Installed core identity failed')
    tag = 'engineering-hub-executor:build-compatibility-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
    evidence['stage'] = 'executor_image_build'
    run('docker', 'build', '--pull=false', '--network=none', '--tag', tag,
        '--build-arg', 'CORE_IMAGE=' + reference, '--build-arg', 'SOURCE_COMMIT=' + values['SOURCE_COMMIT'],
        '--build-arg', 'EXECUTOR_WHEEL_SHA256=' + values['WHEEL_SHA256'], str(directory / 'context'), timeout=600)
    evidence['stage'] = 'executor_import_smoke'
    return {'core_config_digest': info['Id'], 'core_commit': values['CORE_COMMIT'],
            'installed_core_identity': 'passed', 'executor_imports': smoke(tag, values['SOURCE_COMMIT']),
            'engine_identity': engine_identity()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['validate', 'wheel', 'compatibility'])
    parser.add_argument('--directory', type=Path, default=Path('compatibility-work'))
    args = parser.parse_args()
    values = inputs(os.environ)
    if args.mode == 'validate':
        git('merge-base', '--is-ancestor', values['SOURCE_COMMIT'], 'HEAD')
        return
    args.directory.mkdir(exist_ok=True)
    receipt = Path('executor-compatibility-receipt.json')
    record = json.loads(receipt.read_text()) if receipt.exists() else {
        'schema': 'engineering-hub.executor-compatibility-ci.v1', 'inputs': values,
        'staging_acceptance': 'not_tested', 'image_published': False}
    evidence = record[args.mode + '_evidence'] = {'stage': 'phase_start'}
    try:
        record[args.mode] = (wheel if args.mode == 'wheel' else compatibility)(values, args.directory.resolve(), evidence)
        record['state'] = 'passed' if args.mode == 'compatibility' else 'wheel_verified'
    except Exception as exc:
        record['state'] = args.mode + '_failed'
        record['failure'] = failure(exc, evidence['stage'])
        raise RuntimeError('Disposable executor compatibility failed; no deployment attempted') from None
    finally:
        receipt.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
