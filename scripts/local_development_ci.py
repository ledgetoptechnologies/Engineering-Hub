"""One-shot rootless local CI; no daemon setup, publication or deployment.

Only committed development refs and an explicitly prepared cached image are
accepted. The image bootstrap is trusted operator code; branch code executes
only after host-side kernel resource checks and never gets a Docker socket.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import ExitStack
import errno
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
MAX_TIMEOUT_SECONDS = 900
FILES = {'pyproject.toml': 'pyproject', 'uv.lock': 'uv-lock',
         'hub-ui/package.json': 'package', 'hub-ui/package-lock.json': 'npm-lock'}
IMAGE = re.compile(r'^sha256:[a-f0-9]{64}$')
CID = re.compile(r'^[a-f0-9]{64}$')
PROC_ROOT = Path('/proc')
CGROUP_ROOT = Path('/sys/fs/cgroup')
FD_ROOT = Path('/proc/self/fd')


class CIError(RuntimeError):
    pass


def validate_timeout(value):
    if type(value) is not int or not 1 <= value <= MAX_TIMEOUT_SECONDS:
        raise CIError('Bounded local CI wait required')
    return value


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


def _cgroup_read(fd, name):
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        raw = os.read(source, 4097)
        if len(raw) > 4096:
            raise CIError('Kernel cgroup metadata exceeds its bound')
        return raw.decode('ascii').strip()
    finally:
        os.close(source)


class KernelCgroup:
    """Pinned kernel scope, retained until terminal inspection/cleanup finishes."""
    def __init__(self, fd, evidence):
        self.fd, self.evidence = fd, evidence

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def verify_empty(self):
        if self.fd is None:
            raise CIError('Pinned CI cgroup unavailable')
        before = os.fstat(self.fd)
        if (before.st_dev, before.st_ino) != (self.evidence['device'], self.evidence['inode']):
            raise CIError('Pinned CI cgroup identity changed')
        try:
            events = _cgroup_read(self.fd, 'cgroup.events')
        except OSError as error:
            # kernfs/cgroupfs preserves st_nlink=2 after deletion, so link count
            # is not a removal test. Require the kernel's deleted-dentry marker
            # on this exact still-pinned FD, its original device/inode, an ENOENT
            # member lookup and the absence of the original pathname. A renamed
            # scope, temporary missing file or replacement scope is not proof.
            expected = self.evidence['path'] + ' (deleted)'
            current = os.fstat(self.fd)
            if (error.errno != errno.ENOENT
                    or (current.st_dev, current.st_ino) != (self.evidence['device'], self.evidence['inode'])
                    or os.path.lexists(self.evidence['path'])
                    or os.readlink(FD_ROOT / str(self.fd)) != expected
                    or os.readlink(FD_ROOT / str(self.fd)) != expected
                    or os.path.lexists(self.evidence['path'])):
                raise CIError('Pinned CI cgroup quiescence unavailable') from None
            return {'state': 'removed', 'descendants_empty': True,
                    'removal_proof': 'kernel_deleted_fd'}
        values = [line.split() for line in events.splitlines()]
        populated = [value[1] for value in values if len(value) == 2 and value[0] == 'populated']
        if populated != ['0']:
            raise CIError('CI cgroup still has processes; preserve ownership')
        return {'state': 'empty', 'descendants_empty': True}


def verify_kernel_limits(pid):
    if type(pid) is not int or pid <= 1:
        raise CIError('Owned container PID unavailable')
    process = PROC_ROOT / str(pid)
    start = (process / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    rows = (process / 'cgroup').read_text().splitlines()
    paths = [row[3:] for row in rows if row.startswith('0::')]
    if len(paths) != 1 or '..' in Path(paths[0]).parts:
        raise CIError('Container cgroup unavailable')
    root = CGROUP_ROOT / paths[0].lstrip('/')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        cpu = _cgroup_read(fd, 'cpu.max').split()
        memory = _cgroup_read(fd, 'memory.max')
        swap = _cgroup_read(fd, 'memory.swap.max')
        pids = _cgroup_read(fd, 'pids.max')
        if (len(cpu) != 2 or cpu[0] == 'max' or not 0 < int(cpu[0]) <= int(cpu[1])
                or memory == 'max' or not 0 < int(memory) <= MEMORY or swap != '0'
                or pids == 'max' or not 0 < int(pids) <= 256):
            raise CIError('Effective kernel CI limits unavailable')
        named = os.stat(root, follow_symlinks=False)
        if ((named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
                or (process / 'stat').read_text().rsplit(')', 1)[1].split()[19] != start):
            raise CIError('Container PID/cgroup changed during verification')
        return KernelCgroup(fd, {'path': str(root), 'device': before.st_dev, 'inode': before.st_ino,
            'cpu_max': ' '.join(cpu), 'memory_max': memory, 'memory_swap_max': swap, 'pids_max': pids})
    except BaseException:
        os.close(fd)
        raise


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


def run(args, *, expected_commit=None, admission_guard=None, terminal_observer=None):
    """Standalone by default; optional hooks belong only to trusted composition.

    A terminal observer receives detached observations while the original kernel
    FD and admission locks are still held. It is not a public receipt endpoint.
    """
    hooks=admission_guard is not None or terminal_observer is not None
    if (expected_commit is not None and
            (not isinstance(expected_commit,str) or not re.fullmatch('[a-f0-9]{40}',expected_commit))
        or hooks and (expected_commit is None or not callable(admission_guard) or not callable(terminal_observer))):
        raise CIError('Complete private integration hooks and exact source commit required')
    validate_timeout(args.timeout)
    if sys.platform != 'linux' or not IMAGE.fullmatch(args.image):
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
        if expected_commit is None and not hooks:
            return _run_admitted(args, checkout, output, engine_directory)
        return _run_admitted(args, checkout, output, engine_directory,
            expected_commit=expected_commit, admission_guard=admission_guard, terminal_observer=terminal_observer)


def _run_admitted(args, checkout, output, engine_directory, *, expected_commit=None,
                  admission_guard=None, terminal_observer=None):
    held = output / 'HELD.json'
    engine_held = engine_directory / 'HELD.json'
    container = None
    intent = None
    terminal = False
    started = False
    kernel = None
    profile_verified = False
    result = {'schema': 'engineering-hub.local-ci.v1', 'state': 'preflight',
              'advisory': True, 'execution_authorization': False, 'profile': args.profile,
              'phase': 'preflight'}
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
        result['phase'] = 'engine_verification'
        validate_engine(json.loads(docker('info', '--format', '{{json .}}')), args.engine_id)
        result['phase'] = 'source_verification'
        commit = checked_output(['/usr/bin/git', 'rev-parse', '--verify', args.ref + '^{commit}'],
                                cwd=checkout, environment=environment).decode().strip()
        if not re.fullmatch('[a-f0-9]{40}', commit):
            raise CIError('Exact committed source unavailable')
        if expected_commit is not None and commit != expected_commit:
            raise CIError('Candidate branch no longer selects the reviewed commit')
        result.update(source_commit=commit, source_ref=args.ref, image_id=args.image, engine_id=args.engine_id)
        result['phase'] = 'image_verification'
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
        result['phase'] = 'snapshot_creation'
        inputs = root / 'input'
        inputs.mkdir(mode=0o755)
        archive = inputs / 'source.tar'
        subprocess.run(['/usr/bin/git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                        'archive', '--format=tar', '--output', str(archive), commit],
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
        result['phase'] = 'intent_recording'
        write_intent(engine_held, marker)
        write_intent(held, marker)
        container = name
        result['phase'] = 'container_creation'
        if admission_guard is not None:
            admission_guard()
        cid = docker(*create_arguments(args.image, name, inputs, args.profile, intent)).decode().strip()
        if not CID.fullmatch(cid):
            raise CIError('Created container identity unavailable')
        container = cid
        result['phase'] = 'container_profile_verification'
        verify_created(inspect(), args.image, name, inputs, args.profile, intent)
        profile_verified = True
        result.update(container_id=cid, intent=intent, container_name=name)
        if admission_guard is not None:
            admission_guard()
        started = True
        result['phase'] = 'container_start'
        docker('container', 'start', cid)
        result['phase'] = 'kernel_limits_verification'
        kernel = verify_kernel_limits(inspect()['State']['Pid'])
        result['kernel_limits'] = kernel.evidence
        result['phase'] = 'source_admission'
        if admission_guard is not None:
            admission_guard()
        (inputs / 'admit.json').write_text(json.dumps({'intent': intent}))
        (inputs / 'admit.json').chmod(0o444)
        result['phase'] = 'source_execution'
        try:
            docker('container', 'wait', cid, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            result['timed_out'] = True
            docker('container', 'stop', '--time', '10', cid, timeout=20)
        result['phase'] = 'container_terminal_verification'
        state = inspect()['State']
        exited = state.get('Status') == 'exited' and not any(state.get(k) for k in ('Running', 'Paused', 'Restarting'))
        if not exited:
            raise CIError('CI quiescence unverified; preserve ownership')
        result['phase'] = 'kernel_quiescence_verification'
        result['cgroup_terminal'] = kernel.verify_empty()
        terminal = True
        result.update(state='passed' if state['ExitCode'] == 0 and not result.get('timed_out') else 'failed',
                      exit_code=state['ExitCode'], container_id=cid, terminal_verified=True)
        result['phase'] = 'log_retention'
        (root / 'test.log').write_bytes(docker('container', 'logs', '--tail', '5000', cid, merge_stderr=True))
        result['phase'] = 'complete'
        return result
    except Exception as error:
        result['failure_phase'] = result['phase']
        result['failure_code'] = ('cli_timeout' if isinstance(error, subprocess.TimeoutExpired)
            else 'filesystem_unavailable' if isinstance(error, OSError)
            else 'admission_or_verification_failed')
        result['state'] = 'held' if container else 'preflight_failed'
        if container and not terminal:
            try:
                docker('container', 'stop', '--time', '10', container, timeout=20)
                state = inspect()['State']
                exited = (state.get('Status') in {'exited', 'created'} and not state.get('Running')
                          and not state.get('Paused') and not state.get('Restarting'))
                if exited and not started:
                    result['cgroup_terminal'] = {'state': 'not_started', 'descendants_empty': True}
                    terminal = True
                elif exited and kernel is not None:
                    result['cgroup_terminal'] = kernel.verify_empty()
                    terminal = True
                if terminal and profile_verified and kernel is not None:
                    result['exit_code'] = state['ExitCode']
            except Exception:
                result['cleanup_failure_code'] = 'terminal_or_cgroup_unverified'
        if terminal:
            result['state'] = 'failed'
        raise CIError('Local CI unavailable; retained evidence requires inspection') from None
    finally:
        observer_failed = False
        try:
            if terminal and terminal_observer is not None and profile_verified and kernel is not None:
                try:
                    # The retained CID is immutable, but recheck its exact
                    # profile/state and the original pinned scope, not a later
                    # empty pathname or an arbitrary caller result.json.
                    row=inspect()
                    verify_created(row,args.image,name,inputs,args.profile,intent)
                    state=row['State']
                    if (row.get('Id')!=cid or state.get('Status')!='exited' or state.get('Pid')!=0
                        or type(state.get('ExitCode')) is not int or not 0<=state['ExitCode']<=255
                        or any(state.get(key) for key in ('Running','Paused','Restarting'))):
                        raise CIError('Terminal observation unavailable')
                    cgroup=kernel.verify_empty()
                    observation={key:result[key] for key in
                        ('source_commit','image_id','engine_id','container_id','intent','container_name','profile')}
                    observation.update(exit_code=state['ExitCode'],timed_out=result.get('timed_out',False),
                        failed=result['state']!='passed',input_directory=str(inputs),
                        terminal_verified=True,cgroup_terminal=cgroup,kernel_limits=kernel.evidence)
                    terminal_observer(copy.deepcopy(observation))
                except Exception:
                    # A failed observer may have lost its durable receipt. Keep
                    # both HELD markers even though local cleanup looked empty.
                    observer_failed=True
                    terminal=False
                    result.update(state='held',failure_phase='terminal_observation',
                                  failure_code='private_observer_failed')
            result['terminal_verified'] = terminal
            (root / 'result.json').write_text(json.dumps(result, sort_keys=True) + '\n')
            if terminal:
                clear_own_intent(held, intent)
                clear_own_intent(engine_held, intent)
        finally:
            if kernel is not None:
                kernel.close()
        if observer_failed:
            raise CIError('Private terminal observation unavailable; preserve ownership') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('checkout', 'ref', 'image', 'engine', 'engine-id', 'output-root'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--profile', choices=('python', 'ui', 'all'), default='all')
    parser.add_argument('--timeout', type=int, default=MAX_TIMEOUT_SECONDS)
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(0 if result['state'] == 'passed' else 1)
    except Exception:
        parser.exit(2, 'Local CI unavailable; inspect retained result.json, no deployment occurred\n')


if __name__ == '__main__':
    main()
