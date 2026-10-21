"""Offline installed-artifact compatibility check, not an executor/pilot launch.

Requires an already loaded immutable image; never pulls, mounts host paths,
provisions credentials or invokes the installed runner/supervisor service.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import uuid


OWNER = 'hub.executor-smoke-owner'
PYTHON = '/opt/venv/bin/python'
ENTRYPOINT = [PYTHON, '-I', '-m', 'hub_container_executor.installed_runner']
COMMAND = ['--config', '/operator/runner.json']
ENVIRONMENT = ['PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin']
_IMAGE = re.compile(r'(?:ghcr\.io/ledgetoptechnologies/)?engineering-hub-executor'
                    r'(?::build-[a-z0-9-]+|@sha256:[a-f0-9]{64})\Z')

# This is fixed code, not an operator-selected import/command loader. Imports
# check packaging only: none calls a resource factory, parses installed config,
# opens a daemon/issuer socket or constructs mutable executor state.
PROBE = r'''
import hashlib, importlib, json, os, pathlib, sys
assert os.getuid() == os.geteuid() == os.getgid() == os.getegid() == 10001
assert os.getpid() == 1
assert sys.flags.isolated and sys.flags.dont_write_bytecode
assert pathlib.Path.cwd() == pathlib.Path('/')
assert not pathlib.Path('/operator/runner.json').exists()
root = pathlib.Path('/opt/venv/lib/python3.12/site-packages')
package = root / 'hub_container_executor'
record = json.loads((package / '_executor_source.json').read_bytes())
assert record['schema_version'] == 1 and record['source_commit'] == sys.argv[1]
names = ('service', 'local_resources', 'runtime_identity', 'installed_runner',
         'installed_coordinator', 'launch_supervisor', 'launcher_cli', 'launch_transport', 'configuration_commitment')
for name in names:
    module = importlib.import_module('hub_container_executor.' + name)
    path = pathlib.Path(module.__file__)
    assert path == package / (name + '.py')
    expected = record['source_files']['src/hub_container_executor/' + name + '.py']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
helper = importlib.import_module('scripts.inspect_hub_image_identity')
assert pathlib.Path(helper.__file__) == root / 'scripts/inspect_hub_image_identity.py'
assert hashlib.sha256(pathlib.Path(helper.__file__).read_bytes()).hexdigest() == record['source_files']['src/scripts/inspect_hub_image_identity.py']
from engineering_hub.container_pilot_client import ContainerPilotClient
assert callable(getattr(ContainerPilotClient, 'for_private_unix', None)), 'incompatible core'
print(json.dumps({'source_commit': sys.argv[1], 'installed_modules': list(names),
                  'private_unix_client': True, 'installed_artifact': 'passed'}))
'''


def run(*args, timeout=60):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        # Never reflect arbitrary Docker/image output or local configuration.
        raise RuntimeError('Executor artifact check failed; preserve owned evidence') from None


def verify_defaults(info):
    config = info.get('Config')
    if (type(config) is not dict or config.get('User') != '10001:10001'
            or config.get('WorkingDir') != '/' or config.get('Entrypoint') != ENTRYPOINT
            or config.get('Cmd') != COMMAND or config.get('Env') != ENVIRONMENT
            or config.get('Volumes') or config.get('Healthcheck') or config.get('OnBuild')
            or config.get('ExposedPorts')):
        raise ValueError('Executor image defaults do not match the fixed installed runner')


def remove_owned(name, owner):
    result = subprocess.run(['docker', 'container', 'inspect', name],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        if 'no such container' in result.stderr.lower():
            return
        raise RuntimeError('Cannot verify executor fixture ownership; not removed')
    try:
        info, = json.loads(result.stdout)
        valid = info['Config']['Labels'].get(OWNER) == owner
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise RuntimeError('Executor fixture ownership mismatch; not removed')
    run('docker', 'container', 'rm', '-f', name)


def smoke(image, commit):
    if type(image) is not str or not _IMAGE.fullmatch(image):
        raise ValueError('Explicit executor build tag or digest required')
    if type(commit) is not str or not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('Exact source commit required')
    info, = json.loads(run('docker', 'image', 'inspect', image))
    verify_defaults(info)
    image_id = info.get('Id')
    if type(image_id) is not str or not re.fullmatch('sha256:[a-f0-9]{64}', image_id):
        raise ValueError('Exact local image identity required')
    owner = uuid.uuid4().hex
    name = 'hub-executor-artifact-' + owner
    try:
        run('docker', 'container', 'create', '--pull', 'never', '--name', name,
            '--label', OWNER + '=' + owner, '--network', 'none', '--user', '10001:10001',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--memory', '256m', '--memory-swap', '256m', '--cpus', '1', '--pids-limit', '64',
            '--entrypoint', PYTHON, image_id, '-I', '-B', '-c', PROBE, commit)
        report = json.loads(run('docker', 'container', 'start', '--attach', name))
        inspected, = json.loads(run('docker', 'container', 'inspect', name))
        host = inspected.get('HostConfig', {})
        if (inspected['Config']['Labels'].get(OWNER) != owner
                or inspected.get('Image') != image_id or inspected.get('Mounts')
                or inspected['State']['Running'] or inspected['State']['ExitCode'] != 0
                or host.get('NetworkMode') != 'none' or host.get('ReadonlyRootfs') is not True
                or host.get('CapDrop') != ['ALL'] or host.get('CapAdd')
                or host.get('SecurityOpt') != ['no-new-privileges:true']
                or host.get('Privileged') or host.get('PidMode') not in ('', None)
                or host.get('Memory') != 256 * 1024**2
                or host.get('MemorySwap') != 256 * 1024**2
                or host.get('NanoCpus') != 1_000_000_000 or host.get('PidsLimit') != 64
                or host.get('Binds') or host.get('Tmpfs') or host.get('PortBindings')
                or host.get('PublishAllPorts')
                or report.get('source_commit') != commit
                or report.get('installed_artifact') != 'passed'
                or report.get('private_unix_client') is not True):
            raise RuntimeError('Installed executor fixture did not pass')
        return {**report, 'image_id': image_id, 'network': 'none', 'host_mounts': 0,
                'issuer_exchange': 'not_tested', 'staging_pilot': 'not_tested',
                'truenas': 'not_touched'}
    finally:
        remove_owned(name, owner)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--image', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args(argv)
    print(json.dumps(smoke(args.image, args.commit)))


if __name__ == '__main__':
    main()
