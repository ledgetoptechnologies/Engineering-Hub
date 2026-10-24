from pathlib import Path
import hashlib
import io
import json
import subprocess
import unittest
import uuid
from unittest.mock import patch

import pytest

from hub_staging_service.docker import DockerBackend
from hub_staging_service.core import Rejected, recipe


def uid():
    return str(uuid.uuid4())


def profile():
    return {'image_id': 'sha256:' + 'a' * 64, 'build_argv': ['npm', 'run', 'build'],
            'test_argv': ['npm', 'test'], 'timeout_seconds': 30, 'memory_mb': 128,
            'cpus': 1, 'pids': 32, 'output_bytes': 1048576, 'log_bytes': 65536}


class DockerTests(unittest.TestCase):
    def test_fixed_image_rootless_readonly_network_and_hard_tmpfs_limits(self):
        backend = DockerBackend('unix:///run/user/1000/docker.sock')
        paths = {kind: Path('/private') / kind for kind in ('workspace', 'build', 'test_run', 'preview')}
        with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True):
            name, argv = backend.argv(paths, recipe(profile()), uid(), 'build')
        self.assertIn('--pull=never', argv)
        self.assertIn('--network=none', argv)
        self.assertIn('--cgroupns=private', argv)
        self.assertIn('--cap-drop=ALL', argv)
        self.assertIn('--read-only', argv)
        self.assertEqual(argv[argv.index('--user') + 1], '1000:1000')
        self.assertIn(profile()['image_id'], argv)
        self.assertFalse(any('privileged' in part or '--device' in part for part in argv))
        mounts = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == '--mount']
        self.assertFalse(any('docker.sock' in part for part in mounts))
        self.assertEqual(len(mounts), 1)
        self.assertTrue(mounts[0].endswith(',dst=/source,readonly'))
        self.assertTrue(any(part.startswith('/workspace:rw,nosuid,nodev,mode=1777,size=') for part in argv))
        self.assertFalse(any(part.startswith('/workspace:') and 'noexec' in part for part in argv))
        self.assertTrue(any(part.startswith('/output:') and 'size=1048576' in part for part in argv))
        self.assertTrue(any(part.startswith('/test-output:') and 'size=262144' in part for part in argv))

    def test_unknown_container_labels_never_removed(self):
        import subprocess
        backend = DockerBackend('unix:///run/user/1000/docker.sock')
        with patch('hub_staging_service.docker.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, b'{"engineering-hub.staging.command":"foreign"}', b'')
            with self.assertRaises(Exception):
                backend._stop_owned('eh-stage-owned-build', 'owned', 'build', 'scope')
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][1:4], ['--host', backend.endpoint, 'inspect'])
            self.assertEqual(run.call_args.kwargs['env'], backend.env)

    def test_host_executable_cannot_be_selected(self):
        with self.assertRaises(Exception):
            DockerBackend('unix:///run/user/1000/docker.sock', executable='/bin/sh')

    def test_local_endpoint_is_explicit_and_environment_does_not_follow_context(self):
        for endpoint in ('tcp://127.0.0.1:2375', 'ssh://host', 'unix://relative', 'unix:///run/../docker.sock'):
            with self.subTest(endpoint=endpoint), self.assertRaises(Exception):
                DockerBackend(endpoint)
        with patch.dict('os.environ', {'DOCKER_CONTEXT': 'production', 'DOCKER_HOST': 'tcp://foreign:2375'}):
            backend = DockerBackend('unix:///run/user/1000/docker.sock')
            self.assertNotIn('DOCKER_CONTEXT', backend.env)
            self.assertEqual(backend.env['DOCKER_HOST'], 'unix:///run/user/1000/docker.sock')
            self.assertEqual(backend.command('info')[:3], ['/usr/bin/docker', '--host', backend.endpoint])


def daemon_info(**changes):
    return json.dumps(dict(SecurityOptions=['name=seccomp,profile=builtin', 'name=rootless', 'name=cgroupns'],
                           CgroupVersion='2', CgroupDriver='systemd') | changes).encode()


@pytest.mark.parametrize('changes', [
    {'SecurityOptions': []}, {'SecurityOptions': ['name=rootless-foreign']},
    {'SecurityOptions': ['name=seccomp,profile=builtin']}, {'SecurityOptions': [True]},
    {'CgroupVersion': '1'}, {'CgroupVersion': 2}, {'CgroupDriver': 'cgroupfs'},
    {'CgroupDriver': None},
])
def test_daemon_must_be_rootless_v2_systemd_before_any_container_operation(changes):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch.object(backend, '_checked_output', return_value=daemon_info(**changes)) as output, \
         patch('hub_staging_service.docker.subprocess.run') as run:
        with pytest.raises(Rejected, match='rootless_cgroup_policy_unverified'):
            backend._stage({}, recipe(profile()), uid(), 'build')
        run.assert_not_called()
        assert output.call_args.args[0] == ('info', '--format', '{{json .}}')
        assert output.call_args.kwargs['limit'] == 65536


@pytest.mark.parametrize('raw', [b'not-json', b'[]', b'{}',
    b'{"SecurityOptions":["name=rootless"],"SecurityOptions":[],"CgroupVersion":"2","CgroupDriver":"systemd"}'])
def test_missing_or_ambiguous_daemon_info_denied(raw):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch.object(backend, '_checked_output', return_value=raw):
        with pytest.raises(Rejected, match='rootless_cgroup_policy_unverified'):
            backend._verify_daemon()


@pytest.mark.parametrize('raw', [
    b'max 100000\n134217728\n0\n32\n',  # Unenforced CPU.
    b'200000 100000\n134217728\n0\n32\n',
    b'100000 0\n134217728\n0\n32\n',
    b'0 100000\n134217728\n0\n32\n',
    b'100000 100000\nmax\n0\n32\n',
    b'100000 100000\n134217729\n0\n32\n',
    b'100000 100000\n0\n0\n32\n',
    b'100000 100000\n134217728\nmax\n32\n',
    b'100000 100000\n134217728\n1\n32\n',
    b'100000 100000\n134217728\n0\nmax\n',
    b'100000 100000\n134217728\n0\n33\n',
    b'100000 100000\n134217728\n0\n0\n',
    b'100000\t100000\n134217728\n0\n32\n',
    b'100000 100000\n134217728\n0\n32\nextra\n',
    b'100000 100000\n134217728\n0\n',
    b'100000 100000\n134217728\n0\ntrue\n',
], ids=['cpu-max', 'cpu-too-large', 'period-zero', 'quota-zero', 'memory-max', 'memory-too-large',
        'memory-zero', 'swap-max', 'swap-nonzero', 'pids-max', 'pids-too-large', 'pids-zero',
        'malformed-cpu', 'extra-line', 'missing-line', 'not-integer'])
def test_kernel_resource_limits_must_be_positive_finite_and_at_most_recipe(raw):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch.object(backend, '_checked_output', return_value=raw) as output:
        with pytest.raises(Rejected, match='resource_limits_unverified'):
            backend._verify_kernel_limits('exact-owned-container', recipe(profile()))
        parts = output.call_args.args[0]
        assert parts[:4] == ('exec', 'exact-owned-container', '/bin/sh', '-c')
        assert parts[4] == ('exec /bin/cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max '
                            '/sys/fs/cgroup/memory.swap.max /sys/fs/cgroup/pids.max')
        assert output.call_args.kwargs['limit'] == 4096


@pytest.mark.parametrize('cpus,quota,period', [(1, 100000, 100000), (0.5, 50000, 100000),
                                            (0.3, 30000, 100000), (0.5, 25000, 100000)])
def test_verified_kernel_limits_accept_exact_or_stricter_cpu_ram_and_pids(cpus, quota, period):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    selected = recipe(profile() | {'cpus': cpus})
    raw = f'{quota} {period}\n67108864\n0\n16\n'.encode()
    with patch.object(backend, '_checked_output', return_value=raw):
        backend._verify_kernel_limits('exact-owned-container', selected)


def test_fractional_cpu_quota_comparison_does_not_round_up():
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch.object(backend, '_checked_output', return_value=b'30001 100000\n67108864\n0\n16\n'):
        with pytest.raises(Rejected, match='resource_limits_unverified'):
            backend._verify_kernel_limits('exact-owned-container', recipe(profile() | {'cpus': 0.3}))


def stage_paths(tmp_path):
    paths = {kind: tmp_path / kind for kind in ('workspace', 'build', 'test_run', 'preview')}
    for path in paths.values():
        path.mkdir()
    return paths


@pytest.mark.parametrize('known_labels', [True, False])
def test_bad_kernel_proof_prevents_source_copy_and_cleanup_is_exact_label_guarded(tmp_path, known_labels):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    paths, command_id = stage_paths(tmp_path), uid()
    scope = hashlib.sha256(str(paths['workspace'].parent.absolute()).encode()).hexdigest()
    labels = {'engineering-hub.staging.command': command_id,
              'engineering-hub.staging.stage': 'build',
              'engineering-hub.staging.scope': scope if known_labels else 'foreign'}
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if 'inspect' in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(labels).encode(), b'')
        return subprocess.CompletedProcess(args, 0, b'', b'')
    with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True), \
         patch.object(backend, '_verify_daemon'), \
         patch.object(backend, '_verify_kernel_limits', side_effect=Rejected('resource_limits_unverified')), \
         patch('hub_staging_service.docker.subprocess.run', side_effect=run), \
         patch('hub_staging_service.docker.subprocess.Popen') as untrusted:
        with pytest.raises(Rejected, match='resource_limits_unverified' if known_labels else 'execution_uncertain'):
            backend._stage(paths, recipe(profile()), command_id, 'build')
        untrusted.assert_not_called()
    assert all(args[:3] == ['/usr/bin/docker', '--host', backend.endpoint] for args in calls)
    assert any('inspect' in args for args in calls)
    assert any('rm' in args for args in calls) is known_labels


def test_verified_daemon_and_kernel_checks_precede_source_copy_and_recipe_execution(tmp_path):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    paths, events = stage_paths(tmp_path), []
    class Process:
        stdout = io.BytesIO(b'fixture build log')
        returncode = 0
        def poll(self):
            return 0
        def wait(self, **kwargs):
            return 0
    def output(parts, **kwargs):
        events.append('daemon' if parts[0] == 'info' else 'kernel')
        return daemon_info() if parts[0] == 'info' else b'100000 100000\n134217728\n0\n32\n'
    def run(args, **kwargs):
        events.append('start' if 'run' in args else 'pause')
        return subprocess.CompletedProcess(args, 0)
    def popen(args, **kwargs):
        events.append('untrusted-source-copy-and-recipe')
        assert 'cp -R /source/. /workspace/' in args[args.index('-c') + 1]
        return Process()
    with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True), \
         patch.object(backend, '_checked_output', side_effect=output), \
         patch('hub_staging_service.docker.subprocess.run', side_effect=run), \
         patch('hub_staging_service.docker.subprocess.Popen', side_effect=popen), \
         patch.object(backend, '_export'), patch.object(backend, '_stop_owned'):
        assert backend._stage(paths, recipe(profile()), uid(), 'build')['exit_code'] == 0
    assert events[:4] == ['daemon', 'start', 'kernel', 'untrusted-source-copy-and-recipe']


@pytest.mark.parametrize('raw,limit,accepted', [(b'bounded', 16, True), (b'x' * 17, 16, False)])
def test_trusted_cli_output_is_bounded_and_closed_before_use(raw, limit, accepted):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    class Process:
        def __init__(self):
            self.stdout = io.BytesIO(raw)
            self.returncode = 0
        def poll(self):
            return self.returncode
        def wait(self, **kwargs):
            return self.returncode
        def kill(self):
            self.returncode = -9
    process = Process()
    with patch('hub_staging_service.docker.subprocess.Popen', return_value=process) as popen:
        if accepted:
            assert backend._checked_output(('info', '--format', '{{json .}}'), limit=limit) == raw
        else:
            with pytest.raises(Rejected, match='resource_limits_unverified'):
                backend._checked_output(('info', '--format', '{{json .}}'), limit=limit)
        assert popen.call_args.args[0][:3] == ['/usr/bin/docker', '--host', backend.endpoint]
    assert process.stdout.closed


def test_trusted_cli_timeout_kills_only_the_retained_cli_child():
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    class Process:
        stdout = io.BytesIO(b'')
        returncode = None
        killed = False
        def poll(self):
            return self.returncode
        def wait(self, **kwargs):
            return self.returncode
        def kill(self):
            self.killed = True
            self.returncode = -9
    process = Process()
    with patch('hub_staging_service.docker.subprocess.Popen', return_value=process):
        with pytest.raises(Rejected, match='resource_limits_unverified'):
            backend._checked_output(('info', '--format', '{{json .}}'), limit=64, timeout=0.001)
    assert process.killed and process.stdout.closed


if __name__ == '__main__':
    unittest.main()
