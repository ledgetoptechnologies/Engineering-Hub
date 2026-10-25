"""Allowlisted, cached-image-only Docker execution; never executes source on host."""
import json
import io
import hashlib
import os
from fractions import Fraction
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import tarfile

from .core import Rejected, ensure, tree_manifest, relative, make_job_readable


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
        self.executable = executable
        self.endpoint = endpoint
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
                    'DOCKER_HOST': endpoint, 'DOCKER_CONFIG': '/nonexistent/eh-staging-no-config'}

    def command(self, *parts):
        return [self.executable, '--host', self.endpoint, *parts]

    @staticmethod
    def _volume_specs(paths, name, profile, stage):
        specs = {'tests': {'name': name + '-tests', 'size': profile['output_bytes'] // 4,
                           'resource_id': paths['test_run'].name}}
        if stage == 'build':
            specs['output'] = {'name': name + '-output', 'size': profile['output_bytes'],
                               'resource_id': paths['build'].name}
        return specs

    def argv(self, paths, profile, command_id, stage):
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        name = 'eh-stage-' + scope[:12] + '-' + command_id + '-' + stage
        args = self.command('run', '--detach', '--pull=never', '--name', name) + [
                '--label', 'engineering-hub.staging.command=' + command_id,
                '--label', 'engineering-hub.staging.stage=' + stage,
                '--label', 'engineering-hub.staging.scope=' + scope,
                '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--cgroupns=private',
                '--read-only', '--user', str(os.getuid()) + ':' + str(os.getgid()),
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

    def _stage(self, paths, profile, command_id, stage):
        self._verify_daemon()
        name, _ = self.argv(paths, profile, command_id, stage)
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
            return self._stage_inner(paths, profile, command_id, stage)
        finally:
            for kind in specs:
                self._stop_owned(name + '-export-' + kind, command_id, stage, scope)
            self._stop_owned(name, command_id, stage, scope)
            for kind, spec in reversed(possible):
                self._remove_volume(spec, command_id, stage, scope, kind)

    def _stage_inner(self, paths, profile, command_id, stage):
        name, args = self.argv(paths, profile, command_id, stage)
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        specs = self._volume_specs(paths, name, profile, stage)
        log = bytearray()
        overflow = threading.Event()
        started = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=20, check=False, env=self.env)
        ensure(started.returncode == 0, 'container_start_failed')
        self._verify_kernel_limits(name, profile)
        self._verify_job_mounts(name, profile, stage)
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
            ensure(process.returncode == 0, 'stage_' + stage + '_failed')
            # Freeze all job processes before exporting tmpfs outputs, including
            # any descendant the build left behind after its foreground exit.
            subprocess.run(self.command('pause', name), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=True, env=self.env)
            paused = self._checked_output(('inspect', '--format', '{{json .State.Paused}}', name), limit=4096)
            ensure(paused.strip() == b'true', 'execution_uncertain')
            if stage == 'build':
                self._export(name + '-export-output', specs['output']['name'], paths['build'], profile['output_bytes'],
                             profile=profile, command_id=command_id, stage=stage, scope=scope)
            test_output = paths['test_run'] / stage
            test_output.mkdir()
            self._export(name + '-export-tests', specs['tests']['name'], test_output, profile['output_bytes'] // 4,
                         profile=profile, command_id=command_id, stage=stage, scope=scope)
            paused = self._checked_output(('inspect', '--format', '{{json .State.Paused}}', name), limit=4096)
            ensure(paused.strip() == b'true', 'execution_uncertain')
            return {'exit_code': process.returncode, 'log_bytes': len(log)}
        finally:
            self._stop_owned(name, command_id, stage, scope)

    def run(self, paths, profile, command_id):
        ensure(os.name == 'posix' and os.getuid() != 0, 'nonroot_linux_worker_required')
        return self._run_stages(paths, profile, command_id)

    def _run_stages(self, paths, profile, command_id):
        stages = {'build': self._stage(paths, profile, command_id, 'build')}
        ensure(bool(tree_manifest(paths['build'], profile['output_bytes'])[0]), 'empty_build_output')
        # _stage has frozen/exported the bounded output and removed the owned
        # build container. Only then make the validated immutable output tree
        # readable to the next rootless job UID; the test bind stays read-only.
        make_job_readable(paths['build'], profile['output_bytes'])
        stages['test'] = self._stage(paths, profile, command_id, 'test')
        tree_manifest(paths['build'], profile['output_bytes'])
        for directory, dirs, files in os.walk(paths['build'], followlinks=False):
            destination = paths['preview'] / Path(directory).relative_to(paths['build'])
            destination.mkdir(parents=True, exist_ok=True)
            for name in files:
                shutil.copyfile(Path(directory) / name, destination / name, follow_symlinks=False)
        return {'stages': stages, 'preview_ready': True}
