"""Allowlisted, cached-image-only Docker execution; never executes source on host."""
import json
import io
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import tarfile

from .core import Rejected, ensure, tree_manifest, relative


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

    def argv(self, paths, profile, command_id, stage):
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        name = 'eh-stage-' + scope[:12] + '-' + command_id + '-' + stage
        args = self.command('run', '--detach', '--pull=never', '--name', name) + [
                '--label', 'engineering-hub.staging.command=' + command_id,
                '--label', 'engineering-hub.staging.stage=' + stage,
                '--label', 'engineering-hub.staging.scope=' + scope,
                '--network=none', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                '--read-only', '--user', str(os.getuid()) + ':' + str(os.getgid()),
                '--pids-limit', str(profile['pids']), '--memory', str(profile['memory_mb']) + 'm',
                '--memory-swap', str(profile['memory_mb']) + 'm', '--cpus', str(profile['cpus']),
                '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777',
                '--workdir=/workspace', '--env', 'HOME=/tmp', '--env', 'CI=true']
        args += ['--mount', 'type=bind,src=' + str(paths['workspace']) + ',dst=/source,readonly',
                 '--tmpfs', '/workspace:rw,nosuid,nodev,mode=1777,size=' + str(profile['workspace_bytes']),
                 '--tmpfs', '/test-output:rw,noexec,nosuid,nodev,mode=1777,size=' + str(profile['output_bytes'] // 4)]
        if stage == 'build':
            args += ['--tmpfs', '/output:rw,noexec,nosuid,nodev,mode=1777,size=' + str(profile['output_bytes'])]
        else:
            args += ['--mount', 'type=bind,src=' + str(paths['build']) + ',dst=/output,readonly']
        args += ['--entrypoint=/bin/sh', profile['image_id'], '-c', 'while :; do sleep 3600; done']
        return name, args

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

    def _export(self, name, source, destination, limit):
        """Read a bounded Docker tar stream; never use tarfile.extract/extractall."""
        process = subprocess.Popen(self.command('cp', name + ':' + source + '/.', '-'),
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
        name, _ = self.argv(paths, profile, command_id, stage)
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        try:
            return self._stage_inner(paths, profile, command_id, stage)
        finally:
            self._stop_owned(name, command_id, stage, scope)

    def _stage_inner(self, paths, profile, command_id, stage):
        name, args = self.argv(paths, profile, command_id, stage)
        scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
        log = bytearray()
        overflow = threading.Event()
        started = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=20, check=False, env=self.env)
        ensure(started.returncode == 0, 'container_start_failed')
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
            if stage == 'build':
                self._export(name, '/output', paths['build'], profile['output_bytes'])
            test_output = paths['test_run'] / stage
            test_output.mkdir()
            self._export(name, '/test-output', test_output, profile['output_bytes'] // 4)
            return {'exit_code': process.returncode, 'log_bytes': len(log)}
        finally:
            self._stop_owned(name, command_id, stage, scope)

    def run(self, paths, profile, command_id):
        ensure(os.name == 'posix' and os.getuid() != 0, 'nonroot_linux_worker_required')
        stages = {stage: self._stage(paths, profile, command_id, stage) for stage in ('build', 'test')}
        tree_manifest(paths['build'], profile['output_bytes'])
        for directory, dirs, files in os.walk(paths['build'], followlinks=False):
            destination = paths['preview'] / Path(directory).relative_to(paths['build'])
            destination.mkdir(parents=True, exist_ok=True)
            for name in files:
                shutil.copyfile(Path(directory) / name, destination / name, follow_symlinks=False)
        return {'stages': stages, 'preview_ready': True}
