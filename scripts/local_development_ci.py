"""One-shot rootless local CI; no daemon setup, publication or deployment.

Only committed development refs and an explicitly prepared cached image are
accepted. The image bootstrap is trusted operator code; branch code executes
only after host-side kernel resource checks and never gets a Docker socket.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

MEMORY = 2 * 1024**3
TMPFS = 1024**3
FILES = {'pyproject.toml': 'pyproject', 'uv.lock': 'uv-lock',
         'hub-ui/package.json': 'package', 'hub-ui/package-lock.json': 'npm-lock'}
IMAGE = re.compile(r'^sha256:[a-f0-9]{64}$')
CID = re.compile(r'^[a-f0-9]{64}$')


class CIError(RuntimeError):
    pass


def checked_output(argv, *, timeout=30, environment=None, cwd=None, merge_stderr=False):
    # Fixed trusted CLI metadata only. Never stream arbitrary stderr to chat.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(argv, cwd=cwd, env=environment, stdout=output,
                                stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL, timeout=timeout)
        if result.returncode:
            raise CIError('Local CI command failed; inspect retained evidence')
        output.seek(0)
        raw = output.read(1024**2 + 1)
    if len(raw) > 1024**2:
        raise CIError('Local CI metadata exceeded its bound')
    return raw


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise CIError('Canonical existing private directory required')
    value = path.stat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700:
        raise CIError('Current-user private directory required')
    return path


def engine_socket(value):
    if not value.startswith('unix:///'):
        raise CIError('Explicit local rootless Unix socket required')
    path = Path(value[7:])
    private_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise CIError('Current-user rootless socket required')
    return value


def private_lock(path):
    """One current-UID, single-link admission inode shared by start/run/stop."""
    import fcntl
    fd = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        info = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)):
            raise CIError('Private admission lock identity unavailable')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise CIError('Another operation owns this local CI admission') from None


def write_intent(path, value):
    """Durable exclusive intent before effects; never overwrite prior state."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        raw = json.dumps(value, sort_keys=True).encode()
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def clear_own_intent(path, intent):
    """Clear only our original marker after authoritative terminal inspection."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > 8192):
            raise CIError('Retained CI intent identity unavailable')
        value = json.loads(os.read(fd, 8193))
        named = os.stat(path, follow_symlinks=False)
        if value.get('intent') != intent or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino):
            raise CIError('Retained CI intent changed; preserve it')
        os.unlink(path)
    finally:
        os.close(fd)


def validate_engine(info, expected):
    if (info.get('ID') != expected or info.get('OSType') != 'linux'
            or info.get('CgroupVersion') != '2' or info.get('CgroupDriver') != 'systemd'
            or not any('rootless' in item for item in info.get('SecurityOptions', []))):
        raise CIError('Pinned rootless cgroup-v2/systemd engine required')


def verify_kernel_limits(pid):
    if type(pid) is not int or pid <= 1:
        raise CIError('Owned container PID unavailable')
    start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    rows = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
    paths = [row[3:] for row in rows if row.startswith('0::')]
    if len(paths) != 1 or '..' in Path(paths[0]).parts:
        raise CIError('Container cgroup unavailable')
    root = Path('/sys/fs/cgroup') / paths[0].lstrip('/')
    cpu = (root / 'cpu.max').read_text().split()
    memory = (root / 'memory.max').read_text().strip()
    swap = (root / 'memory.swap.max').read_text().strip()
    pids = (root / 'pids.max').read_text().strip()
    if (len(cpu) != 2 or cpu[0] == 'max' or int(cpu[0]) > int(cpu[1])
            or memory == 'max' or int(memory) > MEMORY or swap != '0'
            or pids == 'max' or int(pids) > 256):
        raise CIError('Effective kernel CI limits unavailable')
    if Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19] != start:
        raise CIError('Container PID changed during verification')


def create_arguments(image, name, input_directory, profile, intent):
    return ['container', 'create', '--name', name, '--pull', 'never', '--network', 'none',
        '--user', '10001:10001', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--cpus', '1', '--memory', str(MEMORY),
        '--memory-swap', str(MEMORY), '--pids-limit', '256', '--restart', 'no',
        '--no-healthcheck', '--log-driver', 'json-file', '--log-opt', 'max-size=1m',
        '--log-opt', 'max-file=1', '--tmpfs', f'/tmp:rw,nosuid,nodev,exec,size={TMPFS},mode=1777',
        '--mount', f'type=bind,source={input_directory},target=/input,readonly',
        '--entrypoint', '/opt/validation-venv/bin/python', image,
        '/opt/eh-ci/bootstrap.py', profile, intent]


def verify_created(row, image, name, input_directory, profile, intent):
    host, config = row['HostConfig'], row['Config']
    mounts = row['Mounts']
    if (row['Image'] != image or row['Name'] != '/' + name
            or config['User'] != '10001:10001' or config['Entrypoint'] != ['/opt/validation-venv/bin/python']
            or config['Cmd'] != ['/opt/eh-ci/bootstrap.py', profile, intent]
            or not host['ReadonlyRootfs'] or host['Privileged'] or host['NetworkMode'] != 'none'
            or host.get('PortBindings') or host.get('CapAdd') or host['CapDrop'] != ['ALL']
            or not any(value in {'no-new-privileges', 'no-new-privileges=true'} for value in host['SecurityOpt'])
            or host['NanoCpus'] != 1_000_000_000 or host['Memory'] != MEMORY
            or host['MemorySwap'] != MEMORY or host['PidsLimit'] != 256
            or host['RestartPolicy']['Name'] != 'no' or host.get('Devices') or host.get('DeviceRequests')
            or host.get('PidMode', '') not in {'', 'private'} or host.get('UTSMode', '') not in {'', 'private'}
            or host.get('IpcMode', 'private') != 'private' or host.get('CgroupnsMode', 'private') != 'private'
            or host.get('VolumesFrom') or host.get('Links') or host.get('ExtraHosts')
            or host.get('LogConfig') != {'Type': 'json-file', 'Config': {'max-size': '1m', 'max-file': '1'}}
            or host.get('Tmpfs') != {'/tmp': f'rw,nosuid,nodev,exec,size={TMPFS},mode=1777'} or len(mounts) != 1
            or mounts[0].get('Type') != 'bind' or mounts[0].get('RW') is not False
            or mounts[0].get('Source') != str(input_directory) or mounts[0].get('Destination') != '/input'):
        raise CIError('Actual container profile mismatch')


def run(args):
    if sys.platform != 'linux' or not IMAGE.fullmatch(args.image) or not 1 <= args.timeout <= 300:
        raise CIError('Linux bounded immutable-image CI required')
    if not re.fullmatch(r'refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,180}', args.ref) or args.ref == 'refs/heads/main':
        raise CIError('Explicit committed development branch required')
    checkout = Path(args.checkout).resolve(strict=True)
    output = private_directory(args.output_root)
    engine_socket(args.engine)
    engine_directory = Path(args.engine[7:]).parent
    with ExitStack() as stack:
        engine_lock = private_lock(engine_directory / 'admission.lock')
        stack.callback(os.close, engine_lock)
        if any(os.path.lexists(engine_directory / name) for name in ('STOPPING.json', 'HELD.json')):
            raise CIError('Engine is stopping or has retained CI intent; admission denied')
        lock = private_lock(output / 'runner.lock')
        stack.callback(os.close, lock)
        return _run_admitted(args, checkout, output, engine_directory)


def _run_admitted(args, checkout, output, engine_directory):
    held = output / 'HELD.json'
    engine_held = engine_directory / 'HELD.json'
    container = None
    intent = None
    terminal = False
    result = {'schema': 'engineering-hub.local-ci.v1', 'state': 'preflight',
              'advisory': True, 'execution_authorization': False, 'profile': args.profile}
    root = Path(tempfile.mkdtemp(prefix='run-', dir=output))
    result['evidence_directory'] = str(root)
    os.chmod(root, 0o700)
    config = root / 'docker-config'
    config.mkdir(mode=0o700)
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(root)}
    prefix = ['/usr/bin/docker', '--config', str(config), '--host', args.engine]
    docker = lambda *parts, **kw: checked_output([*prefix, *parts], environment=environment, **kw)
    inspect = lambda: json.loads(docker('container', 'inspect', container))[0]
    try:
        if os.path.lexists(held):
            raise CIError('Uncertain prior CI operation retained; explicit reconciliation required')
        if shutil.disk_usage(output).free < 3 * 1024**3:
            raise CIError('At least 3 GiB free workspace capacity required')
        validate_engine(json.loads(docker('info', '--format', '{{json .}}')), args.engine_id)
        commit = checked_output(['/usr/bin/git', 'rev-parse', '--verify', args.ref + '^{commit}'],
                                cwd=checkout, environment=environment).decode().strip()
        if not re.fullmatch('[a-f0-9]{40}', commit):
            raise CIError('Exact committed source unavailable')
        result.update(source_commit=commit, source_ref=args.ref, image_id=args.image, engine_id=args.engine_id)
        image = json.loads(docker('image', 'inspect', args.image))[0]
        if image.get('Id') != args.image:
            raise CIError('Immutable cached image unavailable')
        labels = image['Config'].get('Labels', {})
        for filename, label in FILES.items():
            raw = checked_output(['/usr/bin/git', 'show', commit + ':' + filename], cwd=checkout, environment=environment)
            if hashlib.sha256(raw).hexdigest() != labels.get('io.engineering-hub.ci.' + label + '-sha256'):
                raise CIError('Dependency locks differ; prepare a new reviewed CI image')
        tree = checked_output(['/usr/bin/git', 'ls-tree', '-rlz', commit], cwd=checkout, environment=environment)
        sizes = [int(row.split(b'\t', 1)[0].split()[-1]) for row in tree.split(b'\0') if row]
        if sum(sizes) > 64 * 1024**2:
            raise CIError('Committed snapshot exceeds 64 MiB bound')
        inputs = root / 'input'
        inputs.mkdir(mode=0o755)
        archive = inputs / 'source.tar'
        subprocess.run(['/usr/bin/git', 'archive', '--format=tar', '--output', str(archive), commit],
                       cwd=checkout, check=True, timeout=30, env=environment,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        archive.chmod(0o444)
        if archive.stat().st_size > 128 * 1024**2:
            raise CIError('Archive exceeds 128 MiB bound')
        intent = uuid4().hex
        name = 'eh-local-ci-' + intent
        # Persist original intent before a potentially ambiguous Docker create.
        # If the CLI times out after creation, the name remains reconcilable and
        # a subsequent invocation may not silently abandon it or start new work.
        marker = {'intent': intent, 'container_name': name, 'engine_id': args.engine_id,
                  'evidence_directory': str(root), 'output_root': str(output)}
        write_intent(engine_held, marker)
        write_intent(held, marker)
        container = name
        cid = docker(*create_arguments(args.image, name, inputs, args.profile, intent)).decode().strip()
        if not CID.fullmatch(cid):
            raise CIError('Created container identity unavailable')
        container = cid
        verify_created(inspect(), args.image, name, inputs, args.profile, intent)
        docker('container', 'start', cid)
        verify_kernel_limits(inspect()['State']['Pid'])
        (inputs / 'admit.json').write_text(json.dumps({'intent': intent}))
        (inputs / 'admit.json').chmod(0o444)
        try:
            docker('container', 'wait', cid, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            result['timed_out'] = True
            docker('container', 'stop', '--time', '10', cid, timeout=20)
        state = inspect()['State']
        terminal = state.get('Status') == 'exited' and not any(state.get(k) for k in ('Running', 'Paused', 'Restarting'))
        if not terminal:
            raise CIError('CI quiescence unverified; preserve ownership')
        result.update(state='passed' if state['ExitCode'] == 0 and not result.get('timed_out') else 'failed',
                      exit_code=state['ExitCode'], container_id=cid, terminal_verified=True)
        (root / 'test.log').write_bytes(docker('container', 'logs', '--tail', '5000', cid, merge_stderr=True))
        return result
    except Exception:
        result['state'] = 'held' if container else 'preflight_failed'
        if container and not terminal:
            try:
                docker('container', 'stop', '--time', '10', container, timeout=20)
                state = inspect()['State']
                terminal = (state.get('Status') in {'exited', 'created'} and not state.get('Running')
                            and not state.get('Paused') and not state.get('Restarting'))
            except Exception:
                pass
        if terminal:
            result['state'] = 'failed'
        raise CIError('Local CI unavailable; retained evidence requires inspection') from None
    finally:
        result['terminal_verified'] = terminal
        (root / 'result.json').write_text(json.dumps(result, sort_keys=True) + '\n')
        if terminal:
            clear_own_intent(held, intent)
            clear_own_intent(engine_held, intent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('checkout', 'ref', 'image', 'engine', 'engine-id', 'output-root'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--profile', choices=('python', 'ui', 'all'), default='all')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(0 if result['state'] == 'passed' else 1)
    except Exception:
        parser.exit(2, 'Local CI unavailable; inspect retained result.json, no deployment occurred\n')


if __name__ == '__main__':
    main()
