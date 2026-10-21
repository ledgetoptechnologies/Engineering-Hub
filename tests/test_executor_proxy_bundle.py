"""Pure generated-file contracts, not actual NGINX/systemd/NAS acceptance."""
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from pathlib import Path
import importlib.util

REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('reviewed_executor_proxy_renderer',
    REPOSITORY / 'scripts/render_executor_proxy_bundle.py')
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)
render_proxy_bundle = RENDERER.render_proxy_bundle
HTTP_TEMPLATE = RENDERER.HTTP_TEMPLATE
HISTORICAL_GENERATIONS = RENDERER.HISTORICAL_GENERATIONS


GENERATION = '1d2a891d-0ea8-4ec0-8f88-4984b397fc8a'  # Proposed fixture data, not an enrolled runtime.


def arguments(**changes):
    values = dict(generation_id=GENERATION,
        api_parent='/var/lib/eh-pilot-volume-test-v1/campaigns/hub-pilot-cli-' + GENERATION + '/executor-state/api',
        socket_leaf='executor.sock', staging_bind_ip='192.168.50.90',
        approved_nas_ip='192.168.50.80', worker_uid=210000)
    values.update(changes)
    return values


class Rendering(unittest.TestCase):
    def test_deterministic_complete_bundle_no_host_reads(self):
        with patch('builtins.open', side_effect=AssertionError('filesystem access')):
            first = render_proxy_bundle(**arguments())
            second = render_proxy_bundle(**arguments())
        self.assertEqual(first, second)
        self.assertEqual(len(first), 6)
        manifest = json.loads(first['bundle-manifest.json'])
        self.assertFalse(manifest['activation'])
        self.assertFalse(manifest['host_state_read'])
        self.assertFalse(manifest['nas_route_verified'])
        for name, proof in manifest['files'].items():
            self.assertEqual(proof['size_bytes'], len(first[name]))
            self.assertEqual(proof['sha256'], 'sha256:' + hashlib.sha256(first[name]).hexdigest())

    def test_exact_current_repository_template_retained(self):
        from pathlib import Path
        original = (REPOSITORY / 'docker/nginx-executor-https.conf.template').read_text()
        self.assertEqual(HTTP_TEMPLATE, original.rstrip() + '\n')
        files = render_proxy_bundle(**arguments())
        config = files['nginx.conf'].decode()
        self.assertIn('events { worker_connections 32; }', config)
        self.assertEqual(config.count('http {\n'), 1)
        self.assertEqual(config.count('proxy_pass http://eh_private_executor;'), 1)
        self.assertIn('map "$request_method:$request_uri" $eh_executor_route', config)
        self.assertIn('listen 192.168.50.90:9443 ssl;', config)
        self.assertIn('allow 192.168.50.80;\n', config)
        self.assertIn('deny all;', config)
        self.assertIn('proxy_next_upstream off;', config)
        self.assertIn('proxy_next_upstream_tries 1;', config)
        self.assertNotIn('proxy_set_header Authorization', config)
        self.assertNotIn('Bearer ', config)
        directives = '\n'.join(row for row in config.splitlines() if not row.lstrip().startswith('#'))
        self.assertNotIn('include ', directives)
        self.assertNotIn('__', directives)

    def test_manual_unit_has_only_required_proxy_mounts_and_long_owned_stop(self):
        files = render_proxy_bundle(**arguments())
        unit_name = next(name for name in files if name.endswith('.service'))
        unit = files[unit_name].decode()
        self.assertIn('PrivateNetwork=no\n', unit)
        self.assertIn('KillMode=mixed\nKillSignal=SIGQUIT\nSendSIGKILL=no\n', unit)
        self.assertIn('TimeoutStopSec=1200\n', unit)
        self.assertIn('CapabilityBoundingSet=CAP_CHOWN CAP_SETUID CAP_SETGID\n', unit)
        self.assertIn('NoNewPrivileges=yes\n', unit)
        self.assertEqual(sum(row.startswith('LoadCredential=') for row in unit.splitlines()), 2)
        self.assertIn('LoadCredential=server.pem:', unit)
        self.assertIn('LoadCredential=server.key:', unit)
        binds = [row for row in unit.splitlines() if row.startswith('BindReadOnlyPaths=')]
        self.assertEqual(len(binds), 5)
        self.assertTrue(any(row.startswith('BindReadOnlyPaths=' + arguments()['api_parent'] + ':')
                            and row.endswith('/uds') for row in binds))
        self.assertFalse(any('/issuer/' in row or '/secrets/' in row or 'docker.sock' in row for row in binds))
        for row in unit.splitlines():
            self.assertFalse(row.startswith(('Restart=', 'RuntimeMaxSec=', 'ExecStop=', 'ExecReload=',
                                             'ReadWritePaths=', 'Environment=')))
        self.assertNotIn('[Install]', '\n'.join(row for row in unit.splitlines() if not row.startswith('#')))
        self.assertNotIn('ca.key', unit)
        self.assertNotIn('server.key', files['nginx.conf'].decode().split('http {', 1)[0])

    def test_private_nss_mapping_is_exact_not_host_account_setup(self):
        files = render_proxy_bundle(**arguments())
        self.assertIn(b'ehproxy:!:210000:210000:', files['passwd'])
        self.assertIn(b'ehproxy:!:210000:', files['group'])
        self.assertEqual(files['passwd'].count(b'\n'), 2)
        self.assertEqual(files['group'].count(b'\n'), 2)

    def test_injected_or_broadened_inputs_denied(self):
        changes = [
            dict(generation_id='not-a-uuid'), dict(generation_id=GENERATION.upper()),
            dict(generation_id=GENERATION + '\nUser=root'),
            dict(api_parent=arguments()['api_parent'] + '/..'),
            dict(api_parent=arguments()['api_parent'] + '\nBindPaths=/'),
            dict(api_parent='/var/run/docker.sock'),
            dict(socket_leaf='../executor.sock'), dict(socket_leaf='other.sock'),
            dict(staging_bind_ip='0.0.0.0'), dict(staging_bind_ip='127.0.0.1'),
            dict(approved_nas_ip='192.168.50.0/24'), dict(approved_nas_ip='all'),
            dict(worker_uid=0), dict(worker_uid=True), dict(worker_uid='210000'),
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                render_proxy_bundle(**arguments(**change))

    def test_stopped_or_held_historical_generations_never_reused(self):
        for generation in HISTORICAL_GENERATIONS:
            values = arguments(generation_id=generation,
                api_parent='/var/lib/eh-pilot-volume-test-v1/campaigns/hub-pilot-cli-' + generation + '/executor-state/api')
            with self.subTest(generation=generation), self.assertRaises(ValueError):
                render_proxy_bundle(**values)

    def test_bound_generation_paths_cannot_cross(self):
        with self.assertRaises(ValueError):
            render_proxy_bundle(**arguments(api_parent=arguments()['api_parent'].replace(GENERATION,
                '2d2a891d-0ea8-4ec0-8f88-4984b397fc8a')))

    def test_cli_returns_only_inert_public_file_text(self):
        output = io.StringIO()
        with redirect_stdout(output), patch('builtins.open', side_effect=AssertionError('host read')):
            self.assertEqual(RENDERER.main(['--generation-id', GENERATION,
                '--api-parent', arguments()['api_parent']]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'rendered_not_activated')
        self.assertEqual(result['files'], {name: raw.decode()
            for name, raw in render_proxy_bundle(**arguments()).items()})

    def test_cli_errors_do_not_reflect_input(self):
        for argv in (['--unexpected', 'PRIVATE-SENTINEL'],
                     ['--generation-id', 'PRIVATE-SENTINEL', '--api-parent', '/invalid']):
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                try:
                    code = RENDERER.main(argv)
                except SystemExit as stopped:
                    code = stopped.code
            self.assertEqual(code, 2)
            self.assertEqual(output.getvalue(), '')
            self.assertNotIn('PRIVATE-SENTINEL', error.getvalue())


if __name__ == '__main__':
    unittest.main()
