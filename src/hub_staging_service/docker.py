"""Allowlisted, cached-image-only Docker execution; never executes source on host."""
import json
import io
import hashlib
import os
from fractions import Fraction
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import threading
import time
import tarfile

from .core import Rejected, ensure, tree_manifest, relative, make_job_readable, identifier


# Trusted immutable-image Python only, before source copy/recipe execution.
# Scratch-enabled images must provide this fixed interpreter and stdlib.
_SCRATCH_PROBE = """import json,os,stat
p='/ci-scratch'
s=os.stat(p,follow_symlinks=False);v=os.statvfs(p)
with open('/proc/self/mountinfo','rb') as f:m=f.read(65537)
with open('/proc/self/uid_map','rb') as f:u=f.read(4097)
with open('/proc/self/gid_map','rb') as f:g=f.read(4097)
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'owner_uid':s.st_uid,'owner_gid':s.st_gid,
'mode':stat.S_IMODE(s.st_mode),'device':s.st_dev,'inode':s.st_ino,
'block_size':v.f_frsize,'blocks':v.f_blocks,'capacity_inodes':v.f_files,
'free_bytes':v.f_bavail*v.f_frsize,'free_inodes':v.f_favail,'filesystem_id':v.f_fsid,
'mountinfo':m.decode('utf-8'),'uid_map':u.decode('ascii'),'gid_map':g.decode('ascii')},separators=(',',':')))
"""


# Owned cleanup, not source execution: fixed isolated-image Python, same mapped
# nonroot identity, only the exact scratch bind. Never chmod the worker-owned top.
_SCRATCH_CLEANUP = """import json,os,stat,sys,time
expected_device,expected_inode,uid,gid,maximum=map(int,sys.argv[1:])
assert uid>0 and gid>0 and os.getuid()==uid and os.getgid()==gid and 0<maximum<=100000
root=os.open('/ci-scratch',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
identity=os.fstat(root);assert identity.st_dev==expected_device and identity.st_ino==expected_inode
removed=0;deadline=time.monotonic()+45

def sweep(parent,depth):
 global removed
 assert depth<=64
 with os.scandir(parent) as entries:
  for entry in entries:
   assert time.monotonic()<deadline and removed<maximum
   info=entry.stat(follow_symlinks=False)
   assert info.st_dev==expected_device
   if stat.S_ISDIR(info.st_mode):
    anchor=os.open(entry.name,os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
    try:
     actual=os.fstat(anchor)
     assert (actual.st_dev,actual.st_ino)==(info.st_dev,info.st_ino) and actual.st_uid==uid
     if stat.S_IMODE(actual.st_mode)&0o700 != 0o700:
      os.chmod('/proc/self/fd/'+str(anchor),stat.S_IMODE(actual.st_mode)|0o700)
     child=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
     try:
      opened=os.fstat(child);assert (opened.st_dev,opened.st_ino)==(actual.st_dev,actual.st_ino)
      sweep(child,depth+1)
     finally:os.close(child)
     check=os.stat(entry.name,dir_fd=parent,follow_symlinks=False)
     assert (check.st_dev,check.st_ino)==(actual.st_dev,actual.st_ino)
     os.rmdir(entry.name,dir_fd=parent)
    finally:os.close(anchor)
   else:
    os.unlink(entry.name,dir_fd=parent)
   removed+=1
try:
 sweep(root,0)
 with os.scandir(root) as remaining:assert next(remaining,None) is None
 final=os.fstat(root);assert (final.st_dev,final.st_ino)==(expected_device,expected_inode)
 os.fsync(root)
 print(json.dumps({'empty':True,'removed':removed,'device':expected_device,'inode':expected_inode}))
finally:os.close(root)
"""


_CGROUP_PROBE = ('exec /bin/cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max '
                 '/sys/fs/cgroup/memory.swap.max /sys/fs/cgroup/pids.max')


def _positive(value):
    ensure(type(value) is bytes and re.fullmatch(rb'[1-9][0-9]{0,19}', value) is not None,
           'resource_limits_unverified')
    return int(value)


class DockerBackend:
    def __init__(self, endpoint, executable='/usr/bin/docker'):
        ensure(executable == '/usr/bin/docker', 'fixed_docker_executable')
        ensure(isinstance(endpoint, str) and endpoint.startswith('unix:///') and
               '\x00' not in endpoint and '\\' not in endpoint and
               all(part not in ('', '.', '..') for part in endpoint[8:].split('/')),
               'local_unix_docker_endpoint_required')
        self._scratch_engine = None
        self.executable = executable
        self.endpoint = endpoint
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
                    'DOCKER_HOST': endpoint, 'DOCKER_CONFIG': '/nonexistent/eh-staging-no-config'}

    def command(self, *parts):
        # Every controlled Engine interaction is rechecked while a scratch run
        # is bound, including finally-path cleanup and trusted probes.
        if self._scratch_engine is not None and parts[:1] != ('info',):
            ensure(self._verify_daemon() == self._scratch_engine[1], 'execution_uncertain')
        return [self.executable, '--host', self.endpoint, *parts]

    @staticmethod
    def _volume_specs(paths, name, profile, stage):
        specs = {'tests': {'name': name + '-tests', 'size': profile['output_bytes'] // 4,
                           'resource_id': paths['test_run'].name}}
        if stage == 'build':
            specs['output'] = {'name': name + '-output', 'size': profile['output_bytes'],
                               'resource_id': paths['build'].name}
        return specs

    @staticmethod
    def _scratch_lease(lease, paths, command_id, *, admission=True):
        from .scratch import ScratchLease, ScratchSlot
        ensure(type(lease) is ScratchLease and type(lease.slot) is ScratchSlot,
               'scratch_lease_required')
        identifier(command_id)
        identifier(lease.job_id)
        ensure(lease.command_id == command_id and
               lease.path == lease.slot.path / ('job-' + command_id)
               and ',' not in str(lease.path)
               and lease.resource_ids == {kind: paths[kind].name for kind in
                                                    ('workspace', 'build', 'test_run', 'preview')},
               'scratch_command_drift')
        return lease.verify(admission=admission)

    def argv(self, paths, profile, command_id, stage, *, scratch_lease=None):
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        name = 'eh-stage-' + scope[:12] + '-' + command_id + '-' + stage
        if scratch_lease is not None:
            self._scratch_lease(scratch_lease, paths, command_id)
        uid, gid = ((scratch_lease.container_uid, scratch_lease.container_gid)
                    if scratch_lease is not None else (os.getuid(), os.getgid()))
        args = self.command('run', '--detach', '--pull=never', '--name', name) + [
                '--label', 'engineering-hub.staging.command=' + command_id,
                '--label', 'engineering-hub.staging.stage=' + stage,
                '--label', 'engineering-hub.staging.scope=' + scope,
                '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--cgroupns=private',
                '--read-only', '--user', str(uid) + ':' + str(gid),
                '--pids-limit', str(profile['pids']), '--memory', str(profile['memory_mb']) + 'm',
                '--memory-swap', str(profile['memory_mb']) + 'm', '--cpus', str(profile['cpus']),
                '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777',
                '--workdir=/workspace', '--env', 'HOME=/tmp', '--env', 'CI=true']
        specs = self._volume_specs(paths, name, profile, stage)
        args += ['--mount', 'type=bind,src=' + str(paths['workspace']) + ',dst=/source,readonly',
                 '--tmpfs', '/workspace:rw,exec,nosuid,nodev,mode=1777,size=' + str(profile['workspace_bytes']),
                 '--mount', 'type=volume,source=' + specs['tests']['name'] + ',target=/test-output,volume-nocopy']
        if stage == 'build':
            args += ['--mount', 'type=volume,source=' + specs['output']['name'] + ',target=/output,volume-nocopy']
        else:
            args += ['--mount', 'type=bind,src=' + str(paths['build']) + ',dst=/output,readonly']
        if scratch_lease is not None:
            args += ['--env', 'EH_CI_SCRATCH=1']
            args += ['--mount', 'type=bind,src=' + str(scratch_lease.path) +
                     ',dst=/ci-scratch,bind-propagation=rprivate,bind-recursive=disabled']
        args += ['--entrypoint=/bin/sh', profile['image_id'], '-c', 'while :; do sleep 3600; done']
        return name, args

    def _container_absent(self, name):
        raw = self._checked_output(('ps', '--all', '--quiet', '--filter', 'name=^/' + name + '$'), limit=4096)
        ensure(not raw.strip(), 'execution_uncertain')

    def _volume_absent(self, name):
        raw = self._checked_output(('volume', 'ls', '--quiet', '--filter', 'name=^' + name + '$'), limit=4096)
        ensure(not raw.strip(), 'execution_uncertain')

    @staticmethod
    def _volume_options(size):
        return {'type': 'tmpfs', 'device': 'tmpfs', 'o': 'size=' + str(size) + ',mode=1777,nosuid,nodev,noexec'}

    @staticmethod
    def _volume_labels(spec, command_id, stage, scope, kind):
        return {'engineering-hub.staging.command': command_id,
                'engineering-hub.staging.stage': stage, 'engineering-hub.staging.scope': scope,
                'engineering-hub.staging.volume': kind, 'engineering-hub.staging.resource': spec['resource_id']}

    def _verify_volume(self, spec, command_id, stage, scope, kind):
        try:
            raw = self._checked_output(('volume', 'inspect', spec['name']), limit=8192)
            values = json.loads(raw)
            ensure(type(values) is list and len(values) == 1, 'execution_uncertain')
            info = values[0]
            wanted = self._volume_labels(spec, command_id, stage, scope, kind)
            ensure(info.get('Name') == spec['name'] and info.get('Driver') == 'local' and
                   info.get('Options') == self._volume_options(spec['size']) and
                   type(info.get('Labels')) is dict and
                   all(info['Labels'].get(key) == value for key, value in wanted.items()), 'execution_uncertain')
        except Exception:
            raise Rejected('execution_uncertain') from None

    def _create_volume(self, spec, command_id, stage, scope, kind):
        parts = ['volume', 'create', '--driver=local']
        for key, value in self._volume_labels(spec, command_id, stage, scope, kind).items():
            parts += ['--label', key + '=' + value]
        for key, value in self._volume_options(spec['size']).items():
            parts += ['--opt', key + '=' + value]
        self._checked_output(tuple(parts + [spec['name']]), limit=4096)
        self._verify_volume(spec, command_id, stage, scope, kind)

    def _remove_volume(self, spec, command_id, stage, scope, kind):
        try:
            existing = self._checked_output(('volume', 'ls', '--quiet', '--filter', 'name=^' + spec['name'] + '$'), limit=4096)
            if not existing.strip():
                return
            self._verify_volume(spec, command_id, stage, scope, kind)
            refs = self._checked_output(('ps', '--all', '--quiet', '--filter', 'volume=' + spec['name']), limit=4096)
            ensure(not refs.strip(), 'execution_uncertain')
            self._checked_output(('volume', 'rm', spec['name']), limit=4096)
            self._volume_absent(spec['name'])
        except Exception:
            raise Rejected('execution_uncertain') from None

    def _checked_output(self, parts, *, limit, timeout=10):
        """Bound trusted CLI output while reading; never capture an unlimited pipe."""
        process = subprocess.Popen(self.command(*parts), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env)
        output = bytearray()
        overflow, read_failed = threading.Event(), threading.Event()
        def drain():
            try:
                while chunk := process.stdout.read(4096):
                    available = limit - len(output)
                    output.extend(chunk[:max(0, available)])
                    if len(chunk) > available:
                        overflow.set()
            except Exception:
                read_failed.set()
            finally:
                process.stdout.close()
        reader = threading.Thread(target=drain)
        started = False
        expired = False
        try:
            reader.start()
            started = True
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if overflow.is_set() or read_failed.is_set() or time.monotonic() >= deadline:
                    expired = True
                    process.kill()  # Only this retained CLI child, never a discovered process.
                    break
                time.sleep(0.01)
            process.wait(timeout=5)
        except Exception:
            raise Rejected('resource_limits_unverified') from None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if started:
                reader.join(timeout=5)
            else:
                process.stdout.close()
        ensure(process.returncode == 0 and not expired and not overflow.is_set()
               and not read_failed.is_set() and not reader.is_alive(), 'resource_limits_unverified')
        return bytes(output)

    def _verify_daemon(self):
        raw = self._checked_output(('info', '--format', '{{json .}}'), limit=65536)
        def unique(pairs):
            result = {}
            for key, value in pairs:
                ensure(key not in result, 'rootless_cgroup_policy_unverified')
                result[key] = value
            return result
        try:
            info = json.loads(raw, object_pairs_hook=unique)
        except (ValueError, TypeError, RecursionError):
            raise Rejected('rootless_cgroup_policy_unverified') from None
        ensure(type(info) is dict and type(info.get('SecurityOptions')) is list
               and all(type(value) is str for value in info['SecurityOptions'])
               and 'name=rootless' in info['SecurityOptions']
               and info.get('CgroupVersion') == '2' and info.get('CgroupDriver') == 'systemd',
               'rootless_cgroup_policy_unverified')
        engine_id = info.get('ID')
        if self._scratch_engine is not None:
            ensure(type(engine_id) is str and engine_id == self._scratch_engine[1],
                   'execution_uncertain')
        return engine_id

    def _verify_scratch_engine(self, lease):
        ensure(self._scratch_engine is not None and self._scratch_engine[0] is lease,
               'execution_uncertain')
        ensure(self._verify_daemon() == self._scratch_engine[1], 'execution_uncertain')

    def _verify_kernel_limits(self, name, profile):
        raw = self._checked_output(('exec', name, '/bin/sh', '-c', _CGROUP_PROBE), limit=4096)
        lines = raw.splitlines()
        ensure(len(lines) == 4, 'resource_limits_unverified')
        cpu = lines[0].split(b' ')
        ensure(len(cpu) == 2, 'resource_limits_unverified')
        quota, period = (_positive(value) for value in cpu)
        requested = Fraction(str(profile['cpus']))
        ensure(quota * requested.denominator <= period * requested.numerator
               and _positive(lines[1]) <= profile['memory_mb'] * 1024**2
               and lines[2] == b'0' and _positive(lines[3]) <= profile['pids'],
               'resource_limits_unverified')

    def _stop_owned(self, name, command_id, stage, scope):
        try:
            return self._stop_owned_checked(name, command_id, stage, scope)
        except Exception:
            raise Rejected('execution_uncertain') from None

    def _stop_owned_checked(self, name, command_id, stage, scope):
        check = subprocess.run(self.command('inspect', '--format', '{{json .Config.Labels}}', name),
                               capture_output=True, timeout=10, check=False, env=self.env)
        if check.returncode != 0:
            absent = subprocess.run(self.command('ps', '--all', '--quiet', '--filter', 'name=^/' + name + '$'),
                                    capture_output=True, timeout=10, check=False, env=self.env)
            ensure(absent.returncode == 0 and not absent.stdout.strip() and not absent.stderr.strip(), 'container_absence_unverified')
            return
        try:
            labels = json.loads(check.stdout)
        except (ValueError, TypeError):
            raise Rejected('container_identity_unknown') from None
        ensure(labels.get('engineering-hub.staging.command') == command_id and
               labels.get('engineering-hub.staging.stage') == stage and
               labels.get('engineering-hub.staging.scope') == scope, 'container_identity_drift')
        subprocess.run(self.command('rm', '--force', name), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True, env=self.env)
        self._container_absent(name)

    def exporter_argv(self, name, volume, profile, command_id, stage, scope):
        policy = dict(profile, cpus=min(profile['cpus'], 0.25), memory_mb=min(profile['memory_mb'], 128),
                      pids=min(profile['pids'], 32))
        args = self.command('run', '--detach', '--pull=never', '--name', name) + [
            '--label', 'engineering-hub.staging.command=' + command_id,
            '--label', 'engineering-hub.staging.stage=' + stage,
            '--label', 'engineering-hub.staging.scope=' + scope,
            '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--cgroupns=private',
            '--read-only', '--user', str(os.getuid()) + ':' + str(os.getgid()),
            '--pids-limit', str(policy['pids']), '--memory', str(policy['memory_mb']) + 'm',
            '--memory-swap', str(policy['memory_mb']) + 'm', '--cpus', str(policy['cpus']),
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777',
            '--mount', 'type=volume,source=' + volume + ',target=/artifacts,readonly,volume-nocopy',
            '--entrypoint=/bin/sh', profile['image_id'], '-c', 'while :; do sleep 3600; done']
        return args, policy

    def _export(self, name, volume, destination, limit, *, profile, command_id, stage, scope):
        """Writers stay paused; only a trusted readonly-volume exporter reads them."""
        args, policy = self.exporter_argv(name, volume, profile, command_id, stage, scope)
        try:
            subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, check=True, env=self.env)
            self._verify_kernel_limits(name, policy)
            self._verify_export_mount(name, limit)
            self._export_stream(name, destination, limit)
        finally:
            self._stop_owned(name, command_id, stage, scope)

    def _verify_export_mount(self, name, limit):
        self._verify_tmpfs_mount(name, '/artifacts', limit, readonly=True)

    def _verify_job_mounts(self, name, profile, stage):
        self._verify_tmpfs_mount(name, '/tmp', 64 * 1024**2, readonly=False)
        self._verify_tmpfs_mount(name, '/workspace', profile['workspace_bytes'], readonly=False, executable=True)
        self._verify_tmpfs_mount(name, '/test-output', profile['output_bytes'] // 4, readonly=False)
        if stage == 'build':
            self._verify_tmpfs_mount(name, '/output', profile['output_bytes'], readonly=False)

    def _verify_tmpfs_mount(self, name, destination, limit, *, readonly, executable=False):
        raw = self._checked_output(('exec', name, '/bin/cat', '/proc/self/mountinfo'), limit=65536)
        matches = []
        for line in raw.decode('ascii').splitlines():
            fields = line.split()
            if len(fields) > 8 and fields[4] == destination:
                split = fields.index('-')
                matches.append((set(fields[5].split(',')), fields[split + 1], fields[split + 3].split(',')))
        ensure(len(matches) == 1, 'artifact_mount_unverified')
        options, filesystem, super_options = matches[0]
        access, forbidden = ('ro', 'rw') if readonly else ('rw', 'ro')
        required = {access, 'nosuid', 'nodev'} | (set() if executable else {'noexec'})
        ensure(filesystem == 'tmpfs' and required <= options and forbidden not in options
               and (not executable or 'noexec' not in options),
               'artifact_mount_unverified')
        sizes = [value[5:] for value in super_options if value.startswith('size=')]
        ensure(len(sizes) == 1 and re.fullmatch('[1-9][0-9]*[kmg]?', sizes[0]), 'artifact_mount_unverified')
        suffix = sizes[0][-1]
        actual = int(sizes[0][:-1]) * {'k': 1024, 'm': 1024**2, 'g': 1024**3}[suffix] if suffix in 'kmg' else int(sizes[0])
        # tmpfs rounds its byte budget to a kernel page; exported file bytes
        # still obey the exact recipe limit in the validated tar reader.
        ensure(limit <= actual <= ((limit + 4095) // 4096) * 4096, 'artifact_mount_unverified')

    @staticmethod
    def _scratch_json(raw):
        def unique(entries):
            value = {}
            for key, item in entries:
                ensure(key not in value, 'scratch_reference_unverified')
                value[key] = item
            return value
        try:
            return json.loads(raw, object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError, RecursionError):
            raise Rejected('scratch_reference_unverified') from None

    @staticmethod
    def _scratch_mount_rows(raw):
        ensure(type(raw) is str and len(raw.encode('utf-8')) <= 128 * 1024, 'scratch_mount_unverified')
        rows = []
        def unescape(value):
            return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)
        for line in raw.splitlines():
            fields = line.split()
            ensure(len(fields) >= 10 and '-' in fields, 'scratch_mount_unverified')
            split = fields.index('-')
            ensure(split >= 6 and len(fields) >= split + 4, 'scratch_mount_unverified')
            rows.append({'id': fields[0], 'device': fields[2], 'root': unescape(fields[3]),
                         'destination': unescape(fields[4]), 'filesystem': fields[split + 1],
                         'options': set(fields[5].split(',')) | set(fields[split + 3].split(',')),
                         'propagation': fields[6:split]})
        return rows

    @staticmethod
    def _scratch_id_map(raw, value, *, inverse=False):
        ensure(type(raw) is str and len(raw) <= 4096, 'scratch_uid_mapping_unverified')
        ranges, found = [], []
        for line in raw.splitlines():
            fields = line.split()
            ensure(len(fields) == 3 and all(re.fullmatch('[0-9]{1,10}', f) for f in fields),
                   'scratch_uid_mapping_unverified')
            inside, outside, count = map(int, fields)
            ensure(count > 0 and inside + count <= 2**32 and outside + count <= 2**32
                   and len(ranges) < 16, 'scratch_uid_mapping_unverified')
            for old_inside, old_outside, old_count in ranges:
                ensure(inside + count <= old_inside or old_inside + old_count <= inside,
                       'scratch_uid_mapping_unverified')
                ensure(outside + count <= old_outside or old_outside + old_count <= outside,
                       'scratch_uid_mapping_unverified')
            ranges.append((inside, outside, count))
            start, target = (outside, inside) if inverse else (inside, outside)
            if start <= value < start + count:
                found.append(target + value - start)
        ensure(len(found) == 1, 'scratch_uid_mapping_unverified')
        return found[0]

    def _verify_scratch_mount(self, name, lease, paths, command_id, *, admission=True):
        facts = self._scratch_lease(lease, paths, command_id, admission=admission)
        raw = self._checked_output(('inspect', '--format', '{{json .Mounts}}', name), limit=65536)
        mounts = self._scratch_json(raw)
        ensure(type(mounts) is list and len(mounts) <= 64, 'scratch_mount_unverified')
        selected = [item for item in mounts if type(item) is dict and
                    (item.get('Destination') == '/ci-scratch' or
                     str(item.get('Destination', '')).startswith('/ci-scratch/'))]
        ensure(len(selected) == 1 and selected[0].get('Type') == 'bind'
               and selected[0].get('Source') == str(lease.path)
               and selected[0].get('Destination') == '/ci-scratch'
               and selected[0].get('RW') is True and selected[0].get('Propagation') == 'rprivate',
               'scratch_mount_unverified')
        raw = self._checked_output(('exec', name, '/usr/local/bin/python', '-I', '-B', '-c', _SCRATCH_PROBE),
                                   limit=256 * 1024)
        def unique(entries):
            value = {}
            for key, item in entries:
                ensure(key not in value, 'scratch_mount_unverified')
                value[key] = item
            return value
        observation = json.loads(raw, object_pairs_hook=unique)
        expected = {'uid', 'gid', 'owner_uid', 'owner_gid', 'mode', 'device', 'inode', 'block_size',
                    'blocks', 'capacity_inodes', 'free_bytes', 'free_inodes', 'filesystem_id',
                    'mountinfo', 'uid_map', 'gid_map'}
        ensure(type(observation) is dict and set(observation) == expected, 'scratch_mount_unverified')
        ensure(all(type(observation[key]) is int and observation[key] >= 0 for key in expected -
                   {'mountinfo', 'uid_map', 'gid_map'}), 'scratch_mount_unverified')
        ensure(observation['uid'] == lease.container_uid and observation['gid'] == lease.container_gid
               and observation['device'] == lease.device and observation['inode'] == lease.inode
               and observation['mode'] == 0o777
               and all(observation[key] == facts[key] for key in
                       ('block_size', 'blocks', 'capacity_inodes', 'filesystem_id'))
               and (lease.free_floor_bytes if admission else 0) <= observation['free_bytes'] <= facts['capacity_bytes']
               and (lease.free_floor_inodes if admission else 0) <= observation['free_inodes'] <= facts['capacity_inodes'],
               'scratch_mount_unverified')
        ensure(self._scratch_id_map(observation['uid_map'], lease.container_uid) == lease.mapped_uid
               and self._scratch_id_map(observation['gid_map'], lease.container_gid) == lease.mapped_gid
               and self._scratch_id_map(observation['uid_map'], os.getuid(), inverse=True) == observation['owner_uid']
               and self._scratch_id_map(observation['gid_map'], os.getgid(), inverse=True) == observation['owner_gid'],
               'scratch_uid_mapping_unverified')
        mountinfo = observation['mountinfo']
        ensure(type(mountinfo) is str and len(mountinfo.encode('utf-8')) <= 65536, 'scratch_mount_unverified')
        rows = [row for row in self._scratch_mount_rows(mountinfo)
                if row['destination'] == '/ci-scratch' or row['destination'].startswith('/ci-scratch/')]
        ensure(len(rows) == 1 and rows[0]['root'] == '/' + lease.path.name
               and rows[0]['device'] == facts['mount_device'] and rows[0]['filesystem'] == facts['filesystem']
               and {'rw', 'nosuid', 'nodev'} <= rows[0]['options']
               and not {'ro', 'noexec'} & rows[0]['options'] and not rows[0]['propagation'],
               'scratch_mount_unverified')
        self._scratch_lease(lease, paths, command_id, admission=admission)

    @staticmethod
    def _scratch_paths_overlap(left, right):
        def parse(value):
            ensure(type(value) is str and value.startswith('/') and '\\' not in value
                   and '\x00' not in value and (value == '/' or all(p not in ('', '.', '..') for p in value[1:].split('/'))),
                   'scratch_reference_unverified')
            return PurePosixPath(value)
        a, b = parse(left), parse(right)
        return a == b or a in b.parents or b in a.parents

    def _scratch_reference_disjoint(self, source, lease):
        ensure(not self._scratch_paths_overlap(source, str(lease.path)), 'scratch_reference_present')
        # A symlink, missing path or inaccessible namespace is unknown, not an
        # absence receipt. A different pathname on the dedicated device is an
        # alias too; never infer disjointness solely from string prefixes.
        try:
            value = os.stat(source, follow_symlinks=False)
        except OSError:
            raise Rejected('scratch_reference_unverified') from None
        ensure(not stat.S_ISLNK(value.st_mode), 'scratch_reference_unverified')
        ensure(value.st_dev != lease.device
               and not (stat.S_ISBLK(value.st_mode) and value.st_rdev == lease.device),
               'scratch_reference_present')

    def _scratch_volumes_absent(self, lease):
        # Detached local bind volumes can retain the path too; container Mounts
        # alone is not enough. Read selected metadata, never full driver options.
        def names():
            raw = self._checked_output(('volume', 'ls', '--quiet'), limit=256 * 257 + 1)
            result = raw.decode('ascii').splitlines()
            ensure(len(result) <= 256 and len(set(result)) == len(result)
                   and all(re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,255}', value) for value in result),
                   'scratch_reference_unverified')
            return tuple(sorted(result))
        before = names()
        if before:
            template = '[{{json .Name}},{{json .Driver}},{{json .Mountpoint}},{{json .Options.device}},{{json .Options.type}}]'
            raw = self._checked_output(('volume', 'inspect', '--format', template, *before),
                                       limit=128 * 1024, timeout=15)
            rows = raw.decode('utf-8').splitlines()
            ensure(len(rows) == len(before), 'scratch_reference_unverified')
            for name, row in zip(before, rows):
                value = self._scratch_json(row)
                ensure(type(value) is list and len(value) == 5 and value[0] == name
                       and value[1] == 'local' and type(value[2]) is str,
                       'scratch_reference_unverified')
                _, _, mountpoint, device, filesystem = value
                self._scratch_reference_disjoint(mountpoint, lease)
                if filesystem == 'tmpfs':
                    ensure(device == 'tmpfs', 'scratch_reference_unverified')
                else:
                    ensure(filesystem in (None, '', 'none'), 'scratch_reference_unverified')
                    if device is not None:
                        ensure(type(device) is str, 'scratch_reference_unverified')
                        self._scratch_reference_disjoint(device, lease)
        ensure(names() == before, 'scratch_reference_unverified')

    def _scratch_references_absent(self, paths, profile, command_id, lease):
        self._verify_scratch_engine(lease)
        self._scratch_lease(lease, paths, command_id, admission=False)
        for stage in ('build', 'test'):
            name, _ = self.argv(paths, profile, command_id, stage)
            for suffix in ('', '-export-tests', '-export-output'):
                self._container_absent(name + suffix)
        self._container_absent(self._scratch_cleanup_name(paths, command_id))
        def identities():
            raw = self._checked_output(('ps', '--all', '--quiet', '--no-trunc'), limit=256 * 65 + 1)
            ids = raw.decode('ascii').splitlines()
            ensure(len(ids) <= 256 and len(set(ids)) == len(ids)
                   and all(re.fullmatch('[a-f0-9]{64}', value) for value in ids),
                   'scratch_reference_unverified')
            return tuple(sorted(ids))
        before = identities()
        if before:
            raw = self._checked_output(('inspect', '--type', 'container', '--format', '{{json .Mounts}}', *before),
                                       limit=128 * 1024, timeout=15)
            lines = raw.decode('utf-8').splitlines()
            ensure(len(lines) == len(before), 'scratch_reference_unverified')
            for line in lines:
                mounts = self._scratch_json(line)
                ensure(type(mounts) is list and len(mounts) <= 64, 'scratch_reference_unverified')
                for mount in mounts:
                    ensure(type(mount) is dict and mount.get('Type') in {'bind', 'volume', 'tmpfs'}, 'scratch_reference_unverified')
                    if mount['Type'] == 'bind':
                        self._scratch_reference_disjoint(mount.get('Source'), lease)
        ensure(identities() == before, 'scratch_reference_unverified')
        self._scratch_volumes_absent(lease)
        with open('/proc/self/mountinfo', 'rb') as stream:
            raw = stream.read(128 * 1024 + 1)
        ensure(len(raw) <= 128 * 1024, 'scratch_reference_unverified')
        for row in self._scratch_mount_rows(raw.decode('utf-8')):
            if row['device'] == lease.mount_device and row['destination'] != str(lease.slot.path):
                ensure(not self._scratch_paths_overlap(row['root'], '/' + lease.path.name),
                       'scratch_reference_present')
            ensure(not (row['destination'] == str(lease.path) or
                        row['destination'].startswith(str(lease.path) + '/')), 'scratch_reference_present')
        self._scratch_lease(lease, paths, command_id, admission=False)
        self._verify_scratch_engine(lease)

    @staticmethod
    def _scratch_cleanup_name(paths, command_id):
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        return 'eh-stage-' + scope[:12] + '-' + command_id + '-scratch-cleanup'

    def _cleanup_scratch(self, paths, profile, command_id, lease):
        self._verify_scratch_engine(lease)
        self._scratch_lease(lease, paths, command_id, admission=False)
        name = self._scratch_cleanup_name(paths, command_id)
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        stage = 'scratch-cleanup'
        self._container_absent(name)
        args, policy = self.exporter_argv(name, 'unused', profile, command_id, stage, scope)
        args[args.index('--user') + 1] = str(lease.container_uid) + ':' + str(lease.container_gid)
        index = args.index('--mount') + 1
        args[index] = ('type=bind,src=' + str(lease.path) +
                       ',dst=/ci-scratch,bind-propagation=rprivate,bind-recursive=disabled')
        try:
            self._verify_scratch_engine(lease)
            started = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=20, check=False, env=self.env)
            ensure(started.returncode == 0, 'execution_uncertain')
            self._verify_kernel_limits(name, policy)
            self._verify_tmpfs_mount(name, '/tmp', 16 * 1024**2, readonly=False)
            self._verify_scratch_mount(name, lease, paths, command_id, admission=False)
            raw = self._checked_output(('exec', name, '/usr/local/bin/python', '-I', '-B', '-c',
                _SCRATCH_CLEANUP, str(lease.device), str(lease.inode), str(lease.container_uid),
                str(lease.container_gid), str(min(lease.ceiling_inodes, 100000))), limit=4096, timeout=60)
            result = self._scratch_json(raw)
            ensure(type(result) is dict and set(result) == {'empty', 'removed', 'device', 'inode'}
                   and result['empty'] is True and result['device'] == lease.device
                   and result['inode'] == lease.inode and type(result['removed']) is int
                   and 0 <= result['removed'] <= min(lease.ceiling_inodes, 100000), 'execution_uncertain')
        finally:
            self._stop_owned(name, command_id, stage, scope)
        self._container_absent(name)
        self._verify_scratch_engine(lease)

    def _confirm_scratch_absent(self, paths, profile, command_id, lease):
        # Contents cleanup runs before the core terminal receipt; it never runs
        # source code and never releases the durable lease or machine hold.
        self._scratch_references_absent(paths, profile, command_id, lease)
        self._cleanup_scratch(paths, profile, command_id, lease)
        self._scratch_references_absent(paths, profile, command_id, lease)
        self._scratch_lease(lease, paths, command_id, admission=False)
        self._verify_scratch_engine(lease)
        lease.confirm_backend_absent(command_id)

    def _export_stream(self, name, destination, limit):
        """Read bounded raw tar; never use docker cp or tarfile.extract/all."""
        process = subprocess.Popen(self.command('exec', name, '/bin/tar', '-C', '/artifacts', '-cf', '-', '.'),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env)
        data = bytearray()
        overflow = threading.Event()
        ceiling = limit + 6 * 1024 * 1024
        def drain():
            try:
                while chunk := process.stdout.read(65536):
                    available = ceiling - len(data)
                    data.extend(chunk[:max(0, available)])
                    if len(chunk) > available:
                        overflow.set()
            finally:
                process.stdout.close()
        reader = threading.Thread(target=drain)
        try:
            reader.start()
        except Exception:
            process.kill(); process.wait(timeout=10); process.stdout.close()
            raise
        deadline = time.monotonic() + 30
        while process.poll() is None:
            if overflow.is_set() or time.monotonic() > deadline:
                process.kill()
                break
            time.sleep(0.05)
        process.wait(timeout=10)
        reader.join(timeout=10)
        ensure(process.returncode == 0 and not overflow.is_set() and not reader.is_alive(), 'artifact_export_failed')
        names, total = set(), 0
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as bundle:
            for item in bundle:
                name = item.name.removeprefix('./').rstrip('/')
                if name in ('', '.') and item.isdir():
                    continue
                path = relative(name)
                ensure(name.casefold() not in names and len(names) < 5000 and
                       (item.isdir() or item.isreg()), 'unsafe_output_archive')
                names.add(name.casefold())
                total += item.size
                ensure(total <= limit, 'output_size_limit')
                target = destination.joinpath(*path.parts)
                if item.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(item) as src, target.open('xb') as dst:
                        shutil.copyfileobj(src, dst, 65536)

    def _stage(self, paths, profile, command_id, stage, *, scratch_lease=None):
        if scratch_lease is None:
            self._verify_daemon()
        else:
            self._verify_scratch_engine(scratch_lease)
        options = {'scratch_lease': scratch_lease} if scratch_lease is not None else {}
        name, _ = self.argv(paths, profile, command_id, stage, **options)
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        specs = self._volume_specs(paths, name, profile, stage)
        # No adoption: any deterministic name left by another/earlier attempt
        # refuses before this attempt creates or removes anything.
        self._container_absent(name)
        for kind, spec in specs.items():
            self._container_absent(name + '-export-' + kind)
            self._volume_absent(spec['name'])
        possible = []
        try:
            for kind, spec in specs.items():
                possible.append((kind, spec))  # Includes a lost creation acknowledgement.
                self._create_volume(spec, command_id, stage, scope, kind)
            return self._stage_inner(paths, profile, command_id, stage, **options)
        finally:
            for kind in specs:
                self._stop_owned(name + '-export-' + kind, command_id, stage, scope)
            self._stop_owned(name, command_id, stage, scope)
            for kind, spec in reversed(possible):
                self._remove_volume(spec, command_id, stage, scope, kind)

    def _stage_inner(self, paths, profile, command_id, stage, *, scratch_lease=None):
        options = {'scratch_lease': scratch_lease} if scratch_lease is not None else {}
        name, args = self.argv(paths, profile, command_id, stage, **options)
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        specs = self._volume_specs(paths, name, profile, stage)
        log = bytearray()
        overflow = threading.Event()
        if scratch_lease is not None:
            self._verify_scratch_engine(scratch_lease)
        started = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=20, check=False, env=self.env)
        ensure(started.returncode == 0, 'container_start_failed')
        self._verify_kernel_limits(name, profile)
        self._verify_job_mounts(name, profile, stage)
        if scratch_lease is not None:
            self._verify_scratch_engine(scratch_lease)
            self._verify_scratch_mount(name, scratch_lease, paths, command_id)
            self._verify_scratch_engine(scratch_lease)
        process = subprocess.Popen(self.command('exec', name, '/bin/sh', '-c',
                                   'cp -R /source/. /workspace/ && exec "$@"',
                                   'eh-staging-recipe') + profile[stage + '_argv'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=self.env)
        def drain():
            try:
                while chunk := process.stdout.read(65536):
                    remaining = profile['log_bytes'] - len(log)
                    log.extend(chunk[:max(0, remaining)])
                    if len(chunk) > remaining:
                        overflow.set()
            finally:
                process.stdout.close()
        reader = threading.Thread(target=drain)
        try:
            reader.start()
        except Exception:
            self._stop_owned(name, command_id, stage, scope)
            process.wait(timeout=20); process.stdout.close()
            raise
        deadline = time.monotonic() + profile['timeout_seconds']
        failure = None
        try:
            while process.poll() is None:
                if overflow.is_set() or time.monotonic() > deadline:
                    failure = 'stage_log_limit' if overflow.is_set() else 'stage_timeout'
                    self._stop_owned(name, command_id, stage, scope)
                    break
                time.sleep(0.1)
            process.wait(timeout=20)
        finally:
            if process.poll() is None:
                self._stop_owned(name, command_id, stage, scope)
                process.wait(timeout=20)
            reader.join(timeout=20)
        ensure(not reader.is_alive(), 'stage_log_reader_stalled')
        try:
            (paths['test_run'] / (stage + '.log')).write_bytes(log)
            ensure(failure is None and not overflow.is_set(), failure or 'stage_log_limit')
            ensure(type(process.returncode) is int and process.returncode >= 0, 'execution_uncertain')
            # Freeze all job processes before exporting tmpfs outputs, including
            # any descendant the build left behind after its foreground exit.
            subprocess.run(self.command('pause', name), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True, env=self.env)
            paused = self._checked_output(('inspect', '--format', '{{json .State.Paused}}', name), limit=4096)
            ensure(paused.strip() == b'true', 'execution_uncertain')
            # A nonnegative Docker CLI exit permits only a frozen diagnostic
            # observation; it does not prove the recipe exited normally. Keep
            # the existing failure classification and never publish its build.
            # Timeout/overflow/signal or failed freeze checks cannot reach here.
            if stage == 'build' and process.returncode == 0:
                self._export(name + '-export-output', specs['output']['name'], paths['build'], profile['output_bytes'],
                             profile=profile, command_id=command_id, stage=stage, scope=scope)
            test_output = paths['test_run'] / stage
            test_output.mkdir()
            self._export(name + '-export-tests', specs['tests']['name'], test_output, profile['output_bytes'] // 4,
                         profile=profile, command_id=command_id, stage=stage, scope=scope)
            paused = self._checked_output(('inspect', '--format', '{{json .State.Paused}}', name), limit=4096)
            ensure(paused.strip() == b'true', 'execution_uncertain')
            ensure(process.returncode == 0, 'stage_' + stage + '_failed')
            return {'exit_code': process.returncode, 'log_bytes': len(log)}
        finally:
            self._stop_owned(name, command_id, stage, scope)

    def run(self, paths, profile, command_id, *, scratch_lease=None):
        ensure(os.name == 'posix' and os.getuid() != 0, 'nonroot_linux_worker_required')
        if scratch_lease is None:
            return self._run_stages(paths, profile, command_id)
        self._scratch_lease(scratch_lease, paths, command_id)
        ensure(self._scratch_engine is None, 'execution_uncertain')
        engine_id = self._verify_daemon()
        ensure(type(engine_id) is str and re.fullmatch('[A-Za-z0-9:_-]{1,128}', engine_id),
               'rootless_cgroup_policy_unverified')
        self._scratch_engine = (scratch_lease, engine_id)
        try:
            return self._run_stages(paths, profile, command_id, scratch_lease=scratch_lease)
        finally:
            try:
                self._confirm_scratch_absent(paths, profile, command_id, scratch_lease)
            finally:
                self._scratch_engine = None

    def _run_stages(self, paths, profile, command_id, *, scratch_lease=None):
        options = {'scratch_lease': scratch_lease} if scratch_lease is not None else {}
        stages = {'build': self._stage(paths, profile, command_id, 'build', **options)}
        ensure(bool(tree_manifest(paths['build'], profile['output_bytes'])[0]), 'empty_build_output')
        # _stage has frozen/exported the bounded output and removed the owned
        # build container. Only then make the validated immutable output tree
        # readable to the next rootless job UID; the test bind stays read-only.
        make_job_readable(paths['build'], profile['output_bytes'])
        stages['test'] = self._stage(paths, profile, command_id, 'test', **options)
        tree_manifest(paths['build'], profile['output_bytes'])
        for directory, dirs, files in os.walk(paths['build'], followlinks=False):
            destination = paths['preview'] / Path(directory).relative_to(paths['build'])
            destination.mkdir(parents=True, exist_ok=True)
            for name in files:
                shutil.copyfile(Path(directory) / name, destination / name, follow_symlinks=False)
        return {'stages': stages, 'preview_ready': True}
