from pathlib import Path
import hashlib
import io
import json
import subprocess
import tarfile
import stat
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
        self.assertEqual(len(mounts), 3)
        self.assertTrue(mounts[0].endswith(',dst=/source,readonly'))
        self.assertTrue(any(part.startswith('/workspace:rw,nosuid,nodev,mode=1777,size=') for part in argv))
        self.assertFalse(any(part.startswith('/workspace:') and 'noexec' in part for part in argv))
        self.assertTrue(any(part.startswith('type=volume,source=') and 'target=/output,volume-nocopy' in part for part in argv))
        self.assertTrue(any(part.startswith('type=volume,source=') and 'target=/test-output,volume-nocopy' in part for part in argv))
        specs = backend._volume_specs(paths, name, recipe(profile()), 'build')
        self.assertEqual(specs['output']['size'], 1048576)
        self.assertEqual(specs['tests']['size'], 262144)

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


def test_build_tree_is_validated_and_made_readable_before_readonly_test_bind(tmp_path):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    paths = stage_paths(tmp_path)
    events = []
    def stage(paths, selected, command_id, stage):
        events.append(stage)
        if stage == 'build':
            (paths['build'] / 'nested').mkdir(mode=0o700)
            (paths['build'] / 'nested/index.html').write_text('synthetic output')
        return {'exit_code': 0, 'log_bytes': 0}
    from hub_staging_service.core import make_job_readable as real_readable
    def readable(path, limit):
        events.append('validate-and-read-permissions')
        assert path == paths['build'] and limit == profile()['output_bytes']
        real_readable(path, limit)
    with patch.object(backend, '_stage', side_effect=stage), \
            patch('hub_staging_service.docker.make_job_readable', side_effect=readable):
        assert backend._run_stages(paths, recipe(profile()), uid())['preview_ready']
    assert events == ['build', 'validate-and-read-permissions', 'test']
    assert (paths['preview'] / 'nested/index.html').read_text() == 'synthetic output'
    with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True):
        _, argv = backend.argv(paths, recipe(profile()), uid(), 'test')
    assert any(value.endswith(',dst=/output,readonly') for value in argv)


@pytest.mark.skipif(__import__('os').name != 'posix', reason='Actual POSIX permission bits')
def test_readable_tree_permissions_change_only_after_validation_and_leave_ancestors_private(tmp_path):
    from hub_staging_service.core import make_job_readable
    private = tmp_path / 'worker'
    private.mkdir(mode=0o700)
    build = private / 'build'
    build.mkdir(mode=0o700)
    nested = build / 'nested'
    nested.mkdir(mode=0o700)
    output = nested / 'index.html'
    output.write_bytes(b'output')
    output.chmod(0o600)
    make_job_readable(build, 100)
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(build.stat().st_mode) == stat.S_IMODE(nested.stat().st_mode) == 0o755
    assert stat.S_IMODE(output.stat().st_mode) == 0o644


def test_oversized_export_is_rejected_before_test_or_permission_change(tmp_path):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    paths = stage_paths(tmp_path)
    selected = recipe(profile())
    (paths['build'] / 'too-big').write_bytes(b'x' * (selected['output_bytes'] + 1))
    with patch.object(backend, '_stage') as stage, patch('hub_staging_service.core.Path.chmod') as chmod:
        with pytest.raises(Rejected, match='output_size_limit'):
            backend._run_stages(paths, selected, uid())
        assert stage.call_count == 1 and stage.call_args.args[-1] == 'build'
        chmod.assert_not_called()


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
         patch.object(backend, '_container_absent'), patch.object(backend, '_volume_absent'), \
         patch.object(backend, '_create_volume'), patch.object(backend, '_remove_volume'), \
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
        if parts[0] == 'inspect':
            return b'true'
        if parts[:3] == ('exec', parts[1], '/bin/cat'):
            events.append('job-hard-mounts')
            return (b'23 1 0:50 / /test-output rw,nosuid,nodev,noexec - tmpfs tmpfs rw,size=256k\n'
                    b'24 1 0:51 / /output rw,nosuid,nodev,noexec - tmpfs tmpfs rw,size=1024k\n')
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
         patch.object(backend, '_container_absent'), patch.object(backend, '_volume_absent'), \
         patch.object(backend, '_create_volume'), patch.object(backend, '_remove_volume'), \
         patch('hub_staging_service.docker.subprocess.run', side_effect=run), \
         patch('hub_staging_service.docker.subprocess.Popen', side_effect=popen), \
         patch.object(backend, '_export'), patch.object(backend, '_stop_owned'):
        assert backend._stage(paths, recipe(profile()), uid(), 'build')['exit_code'] == 0
    assert events[:6] == ['daemon', 'start', 'kernel', 'job-hard-mounts', 'job-hard-mounts', 'untrusted-source-copy-and-recipe']


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


def test_exporter_has_one_readonly_volume_no_source_and_independent_hard_policy():
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True):
        args, policy = backend.exporter_argv('owned-export', 'owned-volume', recipe(profile()), uid(), 'build', 'scope')
    mounts = [args[i + 1] for i, value in enumerate(args[:-1]) if value == '--mount']
    assert mounts == ['type=volume,source=owned-volume,target=/artifacts,readonly,volume-nocopy']
    assert not any('/source' in value or '/workspace' in value or 'docker.sock' in value for value in mounts)
    assert all(value in args for value in ('--read-only', '--network=none', '--cap-drop=ALL', '--cgroupns=private', '--pull=never'))
    assert policy['cpus'] == 0.25 and policy['memory_mb'] == 128 and policy['pids'] == 32
    assert args[args.index('--memory-swap') + 1] == '128m'


def test_exporter_bad_kernel_never_reads_volume_and_stops_exact_owned_container(tmp_path):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True), \
         patch('hub_staging_service.docker.subprocess.run'), \
         patch.object(backend, '_verify_kernel_limits', side_effect=Rejected('resource_limits_unverified')), \
         patch.object(backend, '_export_stream') as stream, patch.object(backend, '_stop_owned') as stop:
        with pytest.raises(Rejected, match='resource_limits_unverified'):
            backend._export('export-owned', 'volume-owned', tmp_path, 1024, profile=recipe(profile()),
                            command_id='command', stage='build', scope='scope')
    stream.assert_not_called()
    stop.assert_called_once_with('export-owned', 'command', 'build', 'scope')


def test_existing_stage_volume_is_never_adopted_or_deleted(tmp_path):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True), \
         patch.object(backend, '_verify_daemon'), patch.object(backend, '_container_absent'), \
         patch.object(backend, '_volume_absent', side_effect=Rejected('execution_uncertain')), \
         patch.object(backend, '_create_volume') as create, patch.object(backend, '_stop_owned') as stop, \
         patch.object(backend, '_remove_volume') as remove:
        with pytest.raises(Rejected, match='execution_uncertain'):
            backend._stage(stage_paths(tmp_path), recipe(profile()), uid(), 'build')
    create.assert_not_called(); stop.assert_not_called(); remove.assert_not_called()


@pytest.mark.parametrize('mutation', ['label', 'size', 'driver'])
def test_volume_identity_and_hard_mount_options_cannot_drift(mutation):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    spec = {'name': 'volume-owned', 'size': 65536, 'resource_id': uid()}
    info = {'Name': spec['name'], 'Driver': 'local', 'Options': backend._volume_options(spec['size']),
            'Labels': backend._volume_labels(spec, 'cmd', 'build', 'scope', 'output')}
    if mutation == 'label':
        info['Labels']['engineering-hub.staging.scope'] = 'foreign'
    elif mutation == 'size':
        info['Options']['o'] = 'size=999999999,mode=1777'
    else:
        info['Driver'] = 'foreign'
    with patch.object(backend, '_checked_output', return_value=json.dumps([info]).encode()):
        with pytest.raises(Rejected, match='execution_uncertain'):
            backend._verify_volume(spec, 'cmd', 'build', 'scope', 'output')


def test_volumes_removed_only_after_all_owned_containers_are_confirmed_absent(tmp_path):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    paths, command_id, events = stage_paths(tmp_path), uid(), []
    with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True):
        name, _ = backend.argv(paths, recipe(profile()), command_id, 'build')
        with patch.object(backend, '_verify_daemon'), patch.object(backend, '_container_absent'), \
             patch.object(backend, '_volume_absent'), patch.object(backend, '_create_volume'), \
             patch.object(backend, '_stage_inner', return_value={'exit_code': 0}), \
             patch.object(backend, '_stop_owned', side_effect=lambda name,*rest: events.append(('absent', name))), \
             patch.object(backend, '_remove_volume', side_effect=lambda spec,*rest: events.append(('volume-remove', spec['name']))):
            backend._stage(paths, recipe(profile()), command_id, 'build')
    assert events[:3] == [('absent', name + '-export-tests'), ('absent', name + '-export-output'), ('absent', name)]
    assert all(event[0] == 'volume-remove' for event in events[3:])


def test_unknown_container_cleanup_prevents_any_volume_removal(tmp_path):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True), \
         patch.object(backend, '_verify_daemon'), patch.object(backend, '_container_absent'), \
         patch.object(backend, '_volume_absent'), patch.object(backend, '_create_volume'), \
         patch.object(backend, '_stage_inner', return_value={'exit_code': 0}), \
         patch.object(backend, '_stop_owned', side_effect=Rejected('execution_uncertain')), \
         patch.object(backend, '_remove_volume') as remove:
        with pytest.raises(Rejected, match='execution_uncertain'):
            backend._stage(stage_paths(tmp_path), recipe(profile()), uid(), 'build')
    remove.assert_not_called()


def test_empty_build_export_is_never_published_as_preview(tmp_path):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch.object(backend, '_stage', return_value={'exit_code': 0}) as stage:
        with pytest.raises(Rejected, match='empty_build_output'):
            backend._run_stages(stage_paths(tmp_path), recipe(profile()), uid())
    assert stage.call_count == 1


@pytest.mark.parametrize('entry_kind', ['file', 'symlink', 'traversal'])
def test_export_stream_uses_fixed_tar_and_validates_entries(tmp_path, entry_kind):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as bundle:
        entry = tarfile.TarInfo('../escape' if entry_kind == 'traversal' else 'index.html')
        if entry_kind == 'symlink':
            entry.type, entry.linkname = tarfile.SYMTYPE, '/foreign'
            bundle.addfile(entry)
        else:
            entry.size = 4; bundle.addfile(entry, io.BytesIO(b'demo'))
    class Process:
        stdout = io.BytesIO(buffer.getvalue())
        returncode = 0
        def poll(self): return 0
        def wait(self, **kwargs): return 0
    with patch('hub_staging_service.docker.subprocess.Popen', return_value=Process()) as child:
        if entry_kind == 'file':
            backend._export_stream('owned-exporter', tmp_path, 1024)
            assert (tmp_path / 'index.html').read_bytes() == b'demo'
        else:
            with pytest.raises(Rejected):
                backend._export_stream('owned-exporter', tmp_path, 1024)
        assert child.call_args.args[0][3:] == ['exec', 'owned-exporter', '/bin/tar', '-C', '/artifacts', '-cf', '-', '.']


@pytest.mark.parametrize('flags,filesystem,size,valid', [
    ('ro,nosuid,nodev,noexec', 'tmpfs', '1024k', True),
    ('rw,nosuid,nodev,noexec', 'tmpfs', '1024k', False),
    ('ro,nosuid,nodev', 'tmpfs', '1024k', False),
    ('ro,nosuid,nodev,noexec', 'ext4', '1024k', False),
    ('ro,nosuid,nodev,noexec', 'tmpfs', '2048k', False),
    ('ro,nosuid,nodev,noexec', 'tmpfs', '512k', False),
    ('ro,nosuid,nodev,noexec', 'tmpfs', 'max', False),
])
def test_export_mount_actual_readonly_tmpfs_budget_is_proven(flags, filesystem, size, valid):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    line = f'23 1 0:50 / /artifacts {flags} - {filesystem} tmpfs rw,size={size}\n'.encode()
    with patch.object(backend, '_checked_output', return_value=line):
        if valid:
            backend._verify_export_mount('owned-exporter', 1048576)
        else:
            with pytest.raises(Rejected, match='artifact_mount_unverified'):
                backend._verify_export_mount('owned-exporter', 1048576)


@pytest.mark.parametrize('flags,filesystem,size', [
    ('rw,nosuid,nodev,noexec', 'tmpfs', '2048k'),
    ('rw,nosuid,nodev,noexec', 'ext4', '1024k'),
    ('ro,nosuid,nodev,noexec', 'tmpfs', '1024k'),
    ('rw,nosuid,nodev', 'tmpfs', '1024k'),
])
def test_bad_job_output_mount_blocks_before_untrusted_process(tmp_path, flags, filesystem, size):
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    raw = (f'23 1 0:50 / /test-output rw,nosuid,nodev,noexec - tmpfs tmpfs rw,size=256k\n'
           f'24 1 0:51 / /output {flags} - {filesystem} tmpfs rw,size={size}\n').encode()
    with patch('os.getuid', return_value=1000, create=True), patch('os.getgid', return_value=1000, create=True), \
         patch('hub_staging_service.docker.subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
         patch.object(backend, '_verify_kernel_limits') as kernel, \
         patch.object(backend, '_checked_output', return_value=raw), \
         patch('hub_staging_service.docker.subprocess.Popen') as untrusted:
        with pytest.raises(Rejected, match='artifact_mount_unverified'):
            backend._stage_inner(stage_paths(tmp_path), recipe(profile()), uid(), 'build')
    kernel.assert_called_once()
    untrusted.assert_not_called()


def test_test_stage_proves_test_output_only_and_does_not_require_tmpfs_build_bind():
    backend = DockerBackend('unix:///run/user/1000/docker.sock')
    with patch.object(backend, '_verify_tmpfs_mount') as mount:
        backend._verify_job_mounts('test-owned', recipe(profile()), 'test')
    mount.assert_called_once_with('test-owned', '/test-output', 262144, readonly=False)


if __name__ == '__main__':
    unittest.main()
