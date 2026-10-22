"""Explicit bounded development-only rootless engine, never a boot service.

No installs, global Docker context, old pilot adoption, pruning, credentials or
application execution. NET=none on the helper is not container isolation: every
consumer must independently require network=none and a closed resource profile.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import pwd
import fcntl
import re
import shutil
import stat
import subprocess
import sys
import time


UNIT = 'eh-development-ci-engine.service'
UNIT_OBJECT = '/org/freedesktop/systemd1/unit/eh_2ddevelopment_2dci_2dengine_2eservice'
MIB = 1024**2
MEMORY = 2 * 1024**3
ERROR = 'Development CI engine unavailable; preserve state for inspection'
_DEADLINE = None
PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'InvocationID', 'MainPID',
    'ControlGroup', 'CPUQuotaPerSecUSec', 'MemoryMax', 'MemorySwapMax', 'TasksMax',
    'Delegate', 'Restart', 'SendSIGKILL', 'TimeoutStopUSec', 'Transient', 'Type')


class EngineError(RuntimeError):
    def __init__(self):
        super().__init__(ERROR)


@dataclass(frozen=True)
class Layout:
    uid: int
    home: Path
    runtime: Path
    private: Path

    @property
    def socket(self): return self.runtime / 'docker.sock'

    @property
    def data(self): return self.private / 'engine'

    @property
    def config(self): return self.private / 'daemon.json'

    @property
    def cli_config(self): return self.private / 'docker-cli'

    @property
    def bus(self): return f'unix:path=/run/user/{self.uid}/bus'


def layout():
    if sys.platform != 'linux' or os.getuid() != 1000 or os.geteuid() != 1000:
        raise EngineError()
    user = pwd.getpwuid(os.getuid())
    if user.pw_name != 'bkoltz':
        raise EngineError()
    home = Path(user.pw_dir)
    if not home.is_absolute() or str(home) != os.path.normpath(str(home)):
        raise EngineError()
    return Layout(os.getuid(), home, Path(f'/run/user/{os.getuid()}/engineering-hub-ci'),
                  home / 'engineering-hub-ci')


def _directory(path, uid, *, private=False, create=False, private_modes=(0o700,)):
    """Walk trusted root/current-owned ancestors without following symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            if part in {'', '.', '..'}:
                raise EngineError()
            try:
                following = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=fd)
                following = os.open(part, flags, dir_fd=fd)
            value = os.fstat(following)
            named = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if (not stat.S_ISDIR(value.st_mode) or value.st_uid not in {0, uid}
                    or value.st_mode & 0o022
                    or (named.st_dev, named.st_ino) != (value.st_dev, value.st_ino)):
                os.close(following)
                raise EngineError()
            os.close(fd)
            fd = following
        value = os.fstat(fd)
        if private and (value.st_uid != uid or stat.S_IMODE(value.st_mode) not in private_modes):
            raise EngineError()
        return value.st_dev, value.st_ino
    finally:
        os.close(fd)


def environment(value):
    # Never propagate SSH sockets, proxies, Docker context/flags or user env.
    return {'HOME': str(value.home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
            'XDG_RUNTIME_DIR': f'/run/user/{value.uid}',
            'DBUS_SESSION_BUS_ADDRESS': value.bus, 'LC_ALL': 'C'}


def run(value, argv, *, timeout=10, returncodes=(0,)):
    if _DEADLINE is not None:
        timeout = min(timeout, _DEADLINE - time.monotonic())
        if timeout <= 0:
            raise EngineError()
    result = subprocess.run(argv, env=environment(value), capture_output=True,
                            text=True, timeout=timeout, check=False)
    if result.returncode not in returncodes or len(result.stdout.encode()) > 2 * MIB:
        raise EngineError()
    return result.stdout


def _json(value, argv):
    try:
        return json.loads(run(value, argv))
    except (ValueError, TypeError):
        raise EngineError() from None


def command(value):
    return ['/usr/bin/env', '-i', f'HOME={value.home}', 'PATH=/usr/bin:/bin:/usr/sbin:/sbin',
        f'XDG_RUNTIME_DIR={value.runtime}', f'DBUS_SESSION_BUS_ADDRESS={value.bus}',
        'DOCKERD_ROOTLESS_ROOTLESSKIT_NET=none',
        'DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=true',
        f'DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR={value.runtime / "rootlesskit"}',
        '/usr/bin/dockerd-rootless.sh', f'--host=unix://{value.socket}',
        f'--data-root={value.data}', f'--exec-root={value.runtime / "exec"}',
        f'--pidfile={value.runtime / "engine.pid"}', f'--config-file={value.config}',
        '--bridge=none', '--iptables=false', '--ip6tables=false',
        '--ip-forward=false', '--ip-masq=false']


def unit_state(value):
    output = run(value, ['/usr/bin/systemctl', '--user', 'show', UNIT,
        '--property=' + ','.join(PROPERTIES)], returncodes=(0, 1))
    result = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if set(result) != set(PROPERTIES):
        raise EngineError()
    return result


def _config(value, *, create=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(value.config, flags)
    except FileNotFoundError:
        if not create:
            raise
        writer = os.open(value.config, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(writer, b'{}\n')
            os.fsync(writer)
        finally:
            os.close(writer)
        fd = os.open(value.config, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != value.uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > 32 or os.read(fd, 33) not in {b'{}', b'{}\n'}):
            raise EngineError()
    finally:
        os.close(fd)


def directories(value, *, create=False):
    _directory(value.home, value.uid)
    _directory(Path(f'/run/user/{value.uid}'), value.uid, private=True)
    _directory(value.private, value.uid, private=True, create=create)
    _directory(value.runtime, value.uid, private=True, create=create)
    # dockerd may set its own data child0710. The enclosing private root remains
    # same-UID0700 and no writable ancestor or foreign owner is accepted.
    _directory(value.data, value.uid, private=True, create=create, private_modes=(0o700, 0o710))
    _directory(value.cli_config, value.uid, private=True, create=create)
    _config(value, create=create)


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


@contextmanager
def admission_lock(value):
    """Shared with the one-shot controller, not a production machine lease."""
    _directory(value.runtime, value.uid, private=True)
    parent = os.open(value.runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = None
    try:
        fd = os.open('admission.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != value.uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise EngineError()
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = os.stat('admission.lock', dir_fd=parent, follow_symlinks=False)
        current = value.runtime.lstat()
        pinned = os.fstat(parent)
        if ((named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)
                or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)):
            raise EngineError()
        yield parent
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _execution(value):
    data = _json(value, ['/usr/bin/busctl', '--user', '--json=short', 'get-property',
        'org.freedesktop.systemd1', UNIT_OBJECT, 'org.freedesktop.systemd1.Service', 'ExecStart'])
    try:
        rows = data['data']
        if len(rows) != 1 or rows[0][0] != '/usr/bin/env' or rows[0][1] != command(value):
            raise EngineError()
    except (TypeError, KeyError, IndexError):
        raise EngineError() from None


def _cgroup(value, state):
    group = state['ControlGroup']
    expected = f'/user.slice/user-{value.uid}.slice/user@{value.uid}.service/'
    if (not group.startswith(expected) or not group.endswith('/' + UNIT)
            or any(part in {'', '.', '..'} for part in group.split('/')[1:])):
        raise EngineError()
    root = Path('/sys/fs/cgroup')
    selected = root / group.lstrip('/')
    if selected.resolve() != selected:
        raise EngineError()
    if (selected.joinpath('memory.max').read_text().strip() != str(MEMORY)
            or selected.joinpath('memory.swap.max').read_text().strip() != '0'
            or selected.joinpath('pids.max').read_text().strip() != '384'):
        raise EngineError()
    quota = selected.joinpath('cpu.max').read_text().split()
    if len(quota) != 2 or not all(part.isdigit() for part in quota) or int(quota[0]) != int(quota[1]):
        raise EngineError()


def verified_unit(value, state):
    expected = {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running',
        'MemoryMax': str(MEMORY), 'MemorySwapMax': '0', 'TasksMax': '384',
        'Delegate': 'yes', 'Restart': 'no', 'SendSIGKILL': 'no',
        'TimeoutStopUSec': '20min', 'Transient': 'yes', 'Type': 'exec',
        'CPUQuotaPerSecUSec': '1s'}
    if any(state.get(key) != expected_value for key, expected_value in expected.items()):
        raise EngineError()
    if (not re.fullmatch('[a-f0-9]{32}', state['InvocationID'])
            or not state['MainPID'].isdigit() or int(state['MainPID']) <= 1):
        raise EngineError()
    _execution(value)
    _cgroup(value, state)


def docker(value, *arguments):
    return ['/usr/bin/docker', '--config', str(value.cli_config),
            '--host', f'unix://{value.socket}', *arguments]


def inspect(value):
    directories(value)
    state = unit_state(value)
    verified_unit(value, state)
    socket = value.socket.lstat()
    if (not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != value.uid
            or socket.st_mode & 0o007):
        raise EngineError()
    info = _json(value, docker(value, 'info', '--format', '{{json .}}'))
    if (type(info) is not dict or info.get('DockerRootDir') != str(value.data)
            or info.get('CgroupDriver') != 'systemd' or info.get('CgroupVersion') != '2'
            or not any('rootless' in option for option in info.get('SecurityOptions', [])
                       if type(option) is str)
            or type(info.get('ID')) is not str or not re.fullmatch('[A-Za-z0-9:_-]{8,128}', info['ID'])):
        raise EngineError()
    # Recheck after daemon query; never conflate a replaced service generation.
    fresh = unit_state(value)
    if any(fresh.get(key) != state.get(key) for key in PROPERTIES):
        raise EngineError()
    verified_unit(value, fresh)
    return {'schema': 'engineering-hub.development-ci-engine.v1', 'state': 'ready',
        'unit': UNIT, 'invocation_id': state['InvocationID'], 'engine_id': info['ID'],
        'socket': str(value.socket), 'data_root': str(value.data),
        'rootless': True, 'cgroup_driver': 'systemd', 'cgroup_version': '2',
        'cpu_limit': 1, 'memory_bytes': MEMORY, 'swap_bytes': 0, 'tasks_max': 384,
        'automatic_restart': False, 'boot_autostart': False}


def _start_locked(value):
    global _DEADLINE
    _directory(value.home, value.uid)
    _directory(Path(f'/run/user/{value.uid}'), value.uid, private=True)
    state = unit_state(value)
    if state['LoadState'] != 'not-found' or state['ActiveState'] != 'inactive':
        raise EngineError()  # no adoption, restart or reset-failed
    if shutil.disk_usage(value.home).free < 8 * 1024**3:
        raise EngineError()
    available = next((line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                      if line.startswith('MemAvailable:')), '0')
    if not available.isdigit() or int(available) * 1024 < 3 * 1024**3:
        raise EngineError()
    controllers = Path(f'/sys/fs/cgroup/user.slice/user-{value.uid}.slice/'
                       f'user@{value.uid}.service/cgroup.controllers').read_text().split()
    if not {'cpu', 'memory', 'pids'} <= set(controllers):
        raise EngineError()
    if (_present(value.runtime / 'STOPPING.json') or _present(value.runtime / 'HELD.json') or _present(value.socket)
            or _present(value.runtime / 'engine.pid')):
        raise EngineError()
    run(value, ['/usr/bin/systemd-run', '--user', '--unit=' + UNIT, '--service-type=exec',
        '--property=Delegate=yes', '--property=Restart=no', '--property=CPUQuota=100%',
        '--property=MemoryMax=2G', '--property=MemorySwapMax=0', '--property=TasksMax=384',
        '--property=TimeoutStartSec=30', '--property=TimeoutStopSec=1200',
        '--property=SendSIGKILL=no', '--property=UMask=0077', '--', *command(value)])
    deadline = time.monotonic() + 30
    previous = _DEADLINE
    _DEADLINE = deadline
    try:
        while time.monotonic() < deadline:
            try:
                return inspect(value)
            except (EngineError, OSError, subprocess.SubprocessError):
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    finally:
        _DEADLINE = previous
    raise EngineError()  # retain unknown state; no force-kill or second daemon


def start(value):
    # Basic disk/identity checks precede provisioning; repeat the complete
    # complete check under the same admission lock as all CI consumers.
    _directory(value.home, value.uid)
    _directory(Path(f'/run/user/{value.uid}'), value.uid, private=True)
    state = unit_state(value)
    if state['LoadState'] != 'not-found' or state['ActiveState'] != 'inactive':
        raise EngineError()
    if shutil.disk_usage(value.home).free < 8 * 1024**3:
        raise EngineError()
    directories(value, create=True)
    with admission_lock(value):
        return _start_locked(value)


def _pid_start(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
    except FileNotFoundError:
        return None
    if len(text) > 65536:
        raise EngineError()
    fields = text.rsplit(')', 1)[1].split()
    if len(fields) < 20 or not fields[19].isdigit():
        raise EngineError()
    return fields[19]


def _terminal(value, pid, started, invocation_id):
    state = unit_state(value)
    return (state['LoadState'] in {'not-found', 'loaded'} and state['ActiveState'] == 'inactive'
        and state['MainPID'] in {'', '0'}
        and state['InvocationID'] in {'', invocation_id}
        and _pid_start(pid) != started and not _present(value.socket))


def _stop_locked(value, parent, *, engine_id, invocation_id):
    global _DEADLINE
    if _present(value.runtime / 'STOPPING.json') or _present(value.runtime / 'HELD.json'):
        raise EngineError()
    before = inspect(value)
    if before['engine_id'] != engine_id or before['invocation_id'] != invocation_id:
        raise EngineError()
    running = run(value, docker(value, 'ps', '--quiet', '--no-trunc')).strip()
    if running:
        raise EngineError()
    if inspect(value) != before:
        raise EngineError()
    selected = unit_state(value)
    if selected['InvocationID'] != invocation_id:
        raise EngineError()
    pid = int(selected['MainPID'])
    started = _pid_start(pid)
    if started is None:
        raise EngineError()
    marker = os.open('STOPPING.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
    terminal = False
    previous = _DEADLINE
    try:
        os.write(marker, json.dumps({'unit': UNIT, 'engine_id': engine_id,
            'invocation_id': invocation_id, 'pid': pid, 'pid_start': started}).encode())
        os.fsync(marker)
        os.fsync(parent)
        run(value, ['/usr/bin/systemctl', '--user', 'stop', '--no-block', UNIT])
        deadline = time.monotonic() + 30
        _DEADLINE = deadline
        while time.monotonic() < deadline:
            try:
                if _terminal(value, pid, started, invocation_id):
                    original = os.fstat(marker)
                    named = os.stat('STOPPING.json', dir_fd=parent, follow_symlinks=False)
                    current, pinned = value.runtime.lstat(), os.fstat(parent)
                    if ((named.st_dev, named.st_ino) != (original.st_dev, original.st_ino)
                            or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)):
                        raise EngineError()
                    os.unlink('STOPPING.json', dir_fd=parent)
                    os.fsync(parent)
                    terminal = True
                    break
            except (EngineError, OSError, subprocess.SubprocessError):
                pass
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    finally:
        _DEADLINE = previous
        os.close(marker)
    return {'schema': before['schema'], 'state': 'stopped' if terminal else 'stop_requested', 'unit': UNIT,
        'engine_id': engine_id, 'invocation_id': invocation_id,
        'terminal_verified': terminal, 'retained_data_root': str(value.data)}


def stop(value, *, engine_id, invocation_id):
    directories(value)
    with admission_lock(value) as parent:
        return _stop_locked(value, parent, engine_id=engine_id, invocation_id=invocation_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('operation', choices=('start', 'inspect', 'stop'))
    parser.add_argument('--engine-id')
    parser.add_argument('--invocation-id')
    args = parser.parse_args(argv)
    try:
        value = layout()
        if args.operation == 'stop':
            if not args.engine_id or not args.invocation_id:
                raise EngineError()
            result = stop(value, engine_id=args.engine_id, invocation_id=args.invocation_id)
        elif args.engine_id is not None or args.invocation_id is not None:
            raise EngineError()
        else:
            result = start(value) if args.operation == 'start' else inspect(value)
        print(json.dumps(result, sort_keys=True))
    except (Exception, KeyboardInterrupt):
        print(json.dumps({'state': 'unavailable', 'error': ERROR}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
