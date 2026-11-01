"""Owned local test volume admission; never discovers or removes shared resources.

Caller supplies an independently enrolled local daemon ID and a task UUID. A
create with an uncertain outcome is held: this helper never retries or deletes.
Mountpoints are daemon metadata for local test use, not host custody proof.
A root-private caller-owned journal directory records the exact task/name/daemon
before creation; existing intent is never adopted or replayed after restart.
An exclusive trusted local operator must exclude same-name concurrent creators.
"""
import json
import re
import subprocess
import os
import selectors
import time
import stat
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

LABEL = 'engineering-hub.local-test-owner'
TMPFS_OPTIONS = (('device','tmpfs'),('o','size=536870912,mode=0700,nr_inodes=4096'),('type','tmpfs'))

class VolumeError(RuntimeError):
    pass

class VolumeOutcomeUnknown(VolumeError):
    pass

@dataclass(frozen=True)
class OwnedVolume:
    name: str
    owner: str
    daemon_id: str
    mountpoint: str
    options: tuple = ()


def _options_match(volume, actual):
    if type(volume) is not OwnedVolume or volume.options not in ((), TMPFS_OPTIONS): return False
    return actual in (None,{}) if not volume.options else actual == dict(volume.options)


def _call(args):
    """Bound both pipe output and elapsed time; do not consult Docker context."""
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'})
    selector = None
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + 15
    try:
        selector = selectors.DefaultSelector()
        for stream in buffers:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    buf = buffers[key.fileobj]
                    buf.extend(chunk)
                    if len(buf) > 65536:
                        raise ValueError('Docker output exceeds bound')
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise ValueError('Docker command failed')
        return bytes(buffers[process.stdout]).decode('utf-8', 'strict')
    finally:
        try:
            if selector is not None:
                selector.close()
        finally:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait()
            finally:
                process.stdout.close()
                process.stderr.close()


# Local test only: an initial-namespace root parent may attest root-owned
# ancestors by passing retained directory FDs. The parent, not these FDs alone,
# is responsible for checking host ownership before entering the user namespace.
_ANCESTOR_PINS = None


def _install_attested_ancestor_fds(fds):
    import fcntl
    global _ANCESTOR_PINS
    if (_ANCESTOR_PINS is not None or os.geteuid() != 0 or type(fds) is not tuple
            or not 0 < len(fds) <= 32 or any(type(fd) is not int or fd < 4 for fd in fds)
            or len(set(fds)) != len(fds)):
        raise ValueError('Invalid local ancestor attestation')
    pins = {}
    for fd in fds:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        key = (info.st_dev, info.st_ino)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if (flags & os.O_ACCMODE != os.O_RDONLY or flags & getattr(os, 'O_PATH', 0)
                or not stat.S_ISDIR(info.st_mode) or info.st_uid != 65534 or key in pins
                or (mode & 0o022 and not mode & stat.S_ISVTX)):
            raise ValueError('Invalid local ancestor attestation')
        pins[key] = (fd, mode)
    _ANCESTOR_PINS = pins


def _attested_ancestor(info):
    if info.st_uid != 65534 or _ANCESTOR_PINS is None:
        return False
    pin = _ANCESTOR_PINS.get((info.st_dev, info.st_ino))
    if pin is None:
        return False
    try:
        held = os.fstat(pin[0])
    except OSError:
        return False
    return (stat.S_ISDIR(held.st_mode) and held.st_uid == 65534
            and (held.st_dev, held.st_ino, stat.S_IMODE(held.st_mode))
            == (info.st_dev, info.st_ino, pin[1])
            and stat.S_IMODE(info.st_mode) == pin[1])


def _ancestor_owner_ok(info):
    return info.st_uid in (0, os.geteuid()) or _attested_ancestor(info)


def _ancestor_mode_ok(info):
    mode = stat.S_IMODE(info.st_mode)
    return not (mode & 0o022) or bool(mode & stat.S_ISVTX and
                                         (info.st_uid == 0 or _attested_ancestor(info)))


def _check_path(handles):
    for index, (fd, parent, name) in enumerate(handles):
        held = os.fstat(fd)
        mode = stat.S_IMODE(held.st_mode)
        if (not _ancestor_owner_ok(held)
                or not _ancestor_mode_ok(held)
                or (index == len(handles)-1 and (held.st_uid != os.geteuid() or mode != 0o700))):
            raise ValueError('Journal directory permissions changed')
        if parent is not None:
            held = os.fstat(fd)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode) or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                raise ValueError('Journal directory changed')


def _close_handles(handles):
    failed = False
    for fd, _, _ in reversed(handles):
        try:
            os.close(fd)
        except OSError:
            failed = True
    if failed:
        raise OSError('Journal descriptor cleanup failed')


def _reserve_intent(journal_dir, task_id, daemon_id, name, suffix, operation, *, storage=None):
    # Hold a no-follow descriptor for each ancestor through create.
    handles = []
    try:
        if (os.name != 'posix' or not os.path.isabs(journal_dir) or journal_dir.startswith('//')
                or '..' in PurePosixPath(journal_dir).parts or str(PurePosixPath(journal_dir)) != journal_dir):
            raise ValueError
        parts = PurePosixPath(journal_dir).parts[1:]
        rootfd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append((rootfd, None, None))
        for namepart in parts:
            parent = handles[-1][0]
            fd = os.open(namepart, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            handles.append((fd, parent, namepart))
        for index, (fd, _, _) in enumerate(handles):
            st = os.fstat(fd)
            mode = stat.S_IMODE(st.st_mode)
            if not _ancestor_owner_ok(st):
                raise ValueError
            if not _ancestor_mode_ok(st):
                raise ValueError
            if index == len(handles)-1 and (st.st_uid != os.geteuid() or mode != 0o700):
                raise ValueError
        _check_path(handles)
        dfd = handles[-1][0]
        value = {'daemon_id':daemon_id,'name':name,'task_id':task_id,'operation':operation}
        if storage is not None: value['storage'] = storage
        record = json.dumps(value,
            sort_keys=True,separators=(',',':')).encode('ascii') + b'\n'
        fd = os.open(task_id + suffix, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=dfd)
        try:
            if os.write(fd, record) != len(record):
                raise OSError('short intent write')
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(dfd)
        _check_path(handles)
    except Exception:
        try:
            _close_handles(handles)
        except OSError:
            pass
        raise VolumeError('Durable volume intent unavailable; do not retry') from None
    return handles


def create_volume(*, task_id, daemon_id, journal_dir, docker='/usr/bin/docker', endpoint='unix:///var/run/docker.sock',
                  storage='local-v1', run=_call):
    """Create one fresh task-named volume after checking the independently pinned daemon.

    On any create failure or malformed/ambiguous post-create inspection, ownership
    remains unknown and no cleanup is attempted. The caller owns later cleanup.
    """
    try:
        if str(uuid.UUID(task_id)) != task_id or not re.fullmatch(r'[A-Za-z0-9:._-]{12,128}', daemon_id):
            raise ValueError
        if docker != '/usr/bin/docker' or endpoint != 'unix:///var/run/docker.sock' or storage not in ('local-v1','tmpfs-512m-v1'):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise VolumeError('Invalid local test enrollment') from None
    prefix = [docker, '--host', endpoint]
    name = 'hub-local-test-' + task_id
    try:
        actual = run(prefix + ['info', '--format', '{{.ID}}']).strip()
    except Exception:
        raise VolumeError('Daemon identity unavailable') from None
    if actual != daemon_id:
        raise VolumeError('Daemon identity mismatch')
    # Never adopt an existing volume, even if its labels match.
    try:
        root = run(prefix + ['info', '--format', '{{.DockerRootDir}}']).strip()
        root_path = PurePosixPath(root)
        if (not root_path.is_absolute() or str(root_path) != root or root == '/'
                or any(ord(c) < 32 or ord(c) == 127 for c in root)
                or root.startswith('//') or '..' in root_path.parts):
            raise ValueError
        listed = run(prefix + ['volume', 'ls', '--format', '{{.Name}}']).splitlines()
    except Exception:
        raise VolumeError('Cannot establish fresh volume name') from None
    if not isinstance(listed, list) or any(type(x) is not str for x in listed):
        raise VolumeError('Invalid volume inventory')
    if name in listed:
        raise VolumeError('Volume name already exists')
    options = TMPFS_OPTIONS if storage == 'tmpfs-512m-v1' else ()
    handles = _reserve_intent(journal_dir, task_id, daemon_id, name, '.intent', 'create',
                              storage=storage if options else None)
    try:
        _check_path(handles)
        command = prefix + ['volume', 'create', '--driver', 'local', '--label', LABEL + '=' + task_id]
        for key,value in options: command += ['--opt',key+'='+value]
        result = run(command + [name]).strip()
        if result != name:
            raise ValueError
        data = json.loads(run(prefix + ['volume', 'inspect', name]))
        if not isinstance(data, list) or len(data) != 1 or type(data[0]) is not dict:
            raise ValueError
        item = data[0]; point = item.get('Mountpoint'); path = PurePosixPath(point)
        if (item.get('Name') != name or item.get('Driver') != 'local'
                or item.get('Labels') != {LABEL: task_id} or not _options_match(OwnedVolume(name,task_id,daemon_id,str(point),options),item.get('Options'))
                or item.get('Scope') != 'local'
                or type(point) is not str or not path.is_absolute()
                or str(path) != point or '..' in path.parts or point.startswith('//')
                or any(ord(c) < 32 or ord(c) == 127 for c in point)
                or point != root + '/volumes/' + name + '/_data'):
            raise ValueError
        if run(prefix + ['info', '--format', '{{.ID}}']).strip() != daemon_id:
            raise ValueError
    except Exception:
        raise VolumeOutcomeUnknown('Volume creation outcome unknown; retain for inspection') from None
    finally:
        try:
            _close_handles(handles)
        except OSError:
            raise VolumeOutcomeUnknown('Volume outcome unknown; journal cleanup failed') from None
    return OwnedVolume(name, task_id, daemon_id, point, options)


def remove_sealed_volume(*, volume, snapshot, server_thread, journal_dir, run=_call):
    """Remove one test volume after trusted caller sealed and joined its broker.

    Snapshot and thread are in-process trusted fixture facts, not credentials or
    host-wide quiescence proof. No uncertain deletion is retried or adopted.
    """
    import threading
    from scripts.hub_local_test_engine_broker import CleanedBrokerSnapshot
    if (type(volume) is not OwnedVolume or type(snapshot) is not CleanedBrokerSnapshot
            or type(server_thread) is not threading.Thread or server_thread.ident is None
            or server_thread.is_alive() or snapshot.owner != volume.owner
            or snapshot.daemon_id != volume.daemon_id or snapshot.volume_name != volume.name
            or snapshot.volume_mount != volume.mountpoint or type(snapshot.containers) is not tuple
            or not re.fullmatch(r'[a-f0-9]{64}', snapshot.enrollment_digest)):
        raise VolumeError('Cleaned broker and joined server required')
    try:
        if str(uuid.UUID(volume.owner)) != volume.owner or volume.name != 'hub-local-test-' + volume.owner:
            raise ValueError
        ids=[]
        for entry in snapshot.containers:
            if (type(entry) is not tuple or len(entry) != 2
                    or not re.fullmatch(r'[a-f0-9]{64}', entry[0])
                    or type(entry[1]) is not str):
                raise ValueError
            ids.append(entry[0])
        if len(ids) != len(set(ids)): raise ValueError
    except (ValueError,TypeError,AttributeError):
        raise VolumeError('Invalid sealed inventory') from None
    prefix = ['/usr/bin/docker','--host','unix:///var/run/docker.sock']
    def verify():
        if run(prefix + ['info','--format','{{.ID}}']).strip() != volume.daemon_id:
            raise ValueError
        root=run(prefix + ['info','--format','{{.DockerRootDir}}']).strip()
        if (not root.startswith('/') or root.startswith('//') or str(PurePosixPath(root)) != root
                or '..' in PurePosixPath(root).parts or any(ord(c)<32 or ord(c)==127 for c in root)
                or volume.mountpoint != root + '/volumes/' + volume.name + '/_data'):
            raise ValueError
        obj=json.loads(run(prefix+['volume','inspect',volume.name]))
        if type(obj) is not list or len(obj)!=1 or type(obj[0]) is not dict: raise ValueError
        item=obj[0]
        if (item.get('Name')!=volume.name or item.get('Mountpoint')!=volume.mountpoint
                or item.get('Driver')!='local' or item.get('Scope')!='local'
                or not _options_match(volume,item.get('Options')) or item.get('Labels')!={LABEL:volume.owner}):
            raise ValueError
        all_ids=run(prefix+['ps','--all','--quiet','--no-trunc']).splitlines()
        if any(not re.fullmatch(r'[a-f0-9]{64}', i) for i in all_ids) or any(i in all_ids for i in ids):
            raise ValueError
    try: verify()
    except Exception: raise VolumeError('Cannot establish sealed volume cleanup') from None
    handles=_reserve_intent(journal_dir,volume.owner,volume.daemon_id,volume.name,'.delete-intent','delete')
    try:
        _check_path(handles)
        verify()
        _check_path(handles)
        result=run(prefix+['volume','rm',volume.name]).strip()
        if result != volume.name: raise ValueError
    except Exception:
        raise VolumeOutcomeUnknown('Volume deletion outcome unknown; do not retry') from None
    finally:
        try: _close_handles(handles)
        except OSError: raise VolumeOutcomeUnknown('Volume deletion outcome unknown; journal cleanup failed') from None
