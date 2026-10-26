"""Disposable CI wheel/core compatibility evidence, never deployment."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from build_hub_executor_wheel import prepare_archive
from verify_hub_executor_wheel import verify_wheel
from hub_executor_image_smoke import smoke

PACKAGE = 'ghcr.io/ledgetoptechnologies/engineering-hub-control@sha256:'
BACKEND_SHA = '51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670'


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
    result = subprocess.run(args, capture_output=True, env=env, timeout=timeout)
    if result.returncode:
        # Never reflect arbitrary image, registry, pip or configuration output.
        raise RuntimeError('Compatibility command failed: ' + args[0])
    return result.stdout


def git(*args):
    return run('git', '-c', 'core.autocrlf=false', *args)


def prepare(values, directory):
    git('merge-base', '--is-ancestor', values['SOURCE_COMMIT'], 'HEAD')
    archive = git('archive', '--format=tar', values['SOURCE_COMMIT'])
    source = prepare_archive(archive, revision=values['SOURCE_COMMIT'],
                             output_directory=directory / 'source')
    context = directory / 'context'
    context.mkdir()
    for original, destination in [('docker/Dockerfile.hub-executor', 'Dockerfile'),
                                 ('scripts/verify_hub_executor_wheel.py', 'validate_executor_wheel.py')]:
        (context / destination).write_bytes(git('cat-file', 'blob', values['SOURCE_COMMIT'] + ':' + original))
    return source, context


def wheel(values, directory):
    source, context = prepare(values, directory)
    backend = directory / 'backend'
    backend.mkdir()
    requirement = directory / 'backend-requirements.txt'
    requirement.write_text('setuptools==84.0.0 --hash=sha256:' + BACKEND_SHA + '\n')
    run(sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check',
        'download', '--index-url', 'https://pypi.org/simple', '--require-hashes',
        '--only-binary=:all:', '--no-deps', '-r', str(requirement), '-d', str(backend))
    artifact = backend / 'setuptools-84.0.0-py3-none-any.whl'
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != BACKEND_SHA:
        raise ValueError('Backend bytes mismatch')
    target = directory / 'backend-site'
    run(sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check',
        'install', '--no-index', '--no-deps', '--no-compile', '--target', str(target), str(artifact))
    destination = directory / 'wheels'
    environment = {**os.environ, 'PYTHONPATH': str(target.resolve()),
                   'PYTHONDONTWRITEBYTECODE': '1', 'SOURCE_DATE_EPOCH': values['SOURCE_DATE_EPOCH']}
    run(sys.executable, '-B', '-m', 'pip', '--isolated', '--disable-pip-version-check',
        'wheel', '--no-deps', '--no-index', '--no-build-isolation', '--no-cache-dir',
        '--wheel-dir', str(destination), str(source), env=environment)
    built, = destination.glob('*.whl')
    verification = verify_wheel(wheel=str(built), sha256=values['WHEEL_SHA256'],
                                source_commit=values['SOURCE_COMMIT'], core_image=PACKAGE + values['CORE_DIGEST'])
    (context / 'executor.whl').write_bytes(built.read_bytes())
    return {**verification, 'wheel_bytes': built.stat().st_size,
            'backend_sha256': BACKEND_SHA, 'python_version': sys.version.split()[0],
            'pip_version': run(sys.executable, '-m', 'pip', '--version').decode().split()[1],
            'context_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in context.iterdir()}}


def compatibility(values, directory):
    reference = PACKAGE + values['CORE_DIGEST']
    info, = json.loads(run('docker', 'image', 'inspect', reference))
    config = info['Config']
    if (info['Id'] != 'sha256:' + values['CORE_CONFIG'] or reference not in info['RepoDigests']
            or config['Labels'].get('org.opencontainers.image.revision') != values['CORE_COMMIT']
            or config['User'] != '568:568'):
        raise ValueError('Pulled immutable core identity mismatch')
    # Fixed installed metadata only; no config/resource/launcher construction.
    code = "from engineering_hub.build_info import read_build_commit; import sys; assert read_build_commit()==sys.argv[1]; print('installed-core-identity-passed')"
    output = run('docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
        '--read-only', '--user', '568:568', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--cpus', '1', '--memory', '256m', '--memory-swap', '256m', '--pids-limit', '64',
        '--entrypoint', '/opt/venv/bin/python', info['Id'], '-I', '-B', '-c', code, values['CORE_COMMIT'])
    if output.strip() != b'installed-core-identity-passed':
        raise ValueError('Installed core identity failed')
    tag = 'engineering-hub-executor:build-compatibility-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
    run('docker', 'build', '--pull=false', '--network=none', '--tag', tag,
        '--build-arg', 'CORE_IMAGE=' + reference, '--build-arg', 'SOURCE_COMMIT=' + values['SOURCE_COMMIT'],
        '--build-arg', 'EXECUTOR_WHEEL_SHA256=' + values['WHEEL_SHA256'], str(directory / 'context'), timeout=600)
    return {'core_config_digest': info['Id'], 'core_commit': values['CORE_COMMIT'],
            'installed_core_identity': 'passed', 'executor_imports': smoke(tag, values['SOURCE_COMMIT'])}


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
    try:
        record[args.mode] = (wheel if args.mode == 'wheel' else compatibility)(values, args.directory.resolve())
        record['state'] = 'passed' if args.mode == 'compatibility' else 'wheel_verified'
    except Exception:
        record['state'] = args.mode + '_failed'
        raise RuntimeError('Disposable executor compatibility failed; no deployment attempted') from None
    finally:
        receipt.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
