from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from hub_staging_service.docker import DockerBackend
from hub_staging_service.core import recipe


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


if __name__ == '__main__':
    unittest.main()
