import http.client
import ipaddress
import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from hub_staging_service.__main__ import BoundedServer, handler, main
from hub_staging_service.core import Rejected


class Stub:
    active = None
    projects = {'demo': {'static': {}}}
    def put_source(self, payload):
        return {'source_id': payload['source_id'], 'source_digest': 'a' * 64, 'size_bytes': 4}
    def operation(self, command_id):
        return {'command_id': command_id, 'job_id': None, 'state': 'held',
                'result': {'error': 'cleanup_uncertain'}}


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.token = 'test-only-fake-credential'
        self.server = BoundedServer(('127.0.0.1', 0), handler(Stub(), self.token, {ipaddress.ip_address('127.0.0.1')}))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, method='GET', path='/v1/status', body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, json.loads(response.read()), dict(response.getheaders()))
        connection.close()
        return result

    def test_human_cookie_and_forwarded_ip_never_authenticate(self):
        code, body, _ = self.request(headers={'Cookie': 'CF_Authorization=already-signed-in', 'X-Forwarded-For': '192.168.50.80'})
        self.assertEqual(code, 401)
        self.assertEqual(body, {'error': 'unauthorized'})

    def test_status_with_dedicated_bearer_has_profiles_no_secret(self):
        code, body, headers = self.request(headers={'Authorization': 'Bearer ' + self.token})
        self.assertEqual(code, 200)
        self.assertEqual(body['projects'], {'demo': ['static']})
        self.assertNotIn(self.token, json.dumps(body))
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_operation_get_uses_same_private_bearer_gate_and_original_command(self):
        command = '59e01102-54a0-4c26-8d8c-4d68be82badb'
        path = '/v1/operations/' + command
        assert self.request(path=path)[0] == 401
        code, body, _ = self.request(path=path, headers={'Authorization': 'Bearer ' + self.token})
        assert code == 200
        assert body == {'command_id': command, 'job_id': None, 'state': 'held',
                        'result': {'error': 'cleanup_uncertain'}}

    def test_duplicate_json_and_chunked_upload_rejected(self):
        headers = {'Authorization': 'Bearer ' + self.token}
        self.assertEqual(self.request('POST', '/v1/sources', '{"x":1,"x":2}', headers)[0], 400)
        headers['Transfer-Encoding'] = 'chunked'
        self.assertEqual(self.request('POST', '/v1/sources', '{}', headers)[0], 400)

    def test_socket_peer_gate_cannot_be_overridden(self):
        other = BoundedServer(('127.0.0.1', 0), handler(Stub(), self.token, {ipaddress.ip_address('192.168.50.80')}))
        thread = threading.Thread(target=other.serve_forever); thread.start()
        try:
            connection = http.client.HTTPConnection(*other.server_address, timeout=3)
            connection.request('GET', '/v1/status', headers={'Authorization': 'Bearer ' + self.token, 'X-Forwarded-For': '192.168.50.80'})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read(); connection.close()
        finally:
            other.shutdown(); other.server_close(); thread.join()


class StorageConfigTests(unittest.TestCase):
    def configuration(self, root):
        return {'token_file': '/unused/test-token', 'worker_root': str(root / 'unopened'),
                'docker_endpoint': 'unix:///run/user/10001/docker.sock',
                'allowed_peer_ips': ['127.0.0.1'], 'projects': {'demo': {
                    'image_id': 'sha256:' + 'a' * 64, 'build_argv': ['true'], 'test_argv': ['true'],
                    'timeout_seconds': 30, 'memory_mb': 128, 'cpus': 1, 'pids': 32,
                    'output_bytes': 1048576, 'log_bytes': 65536}}}

    def test_check_config_validates_storage_limits_without_opening_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.configuration(root)
            config.update(capacity_bytes=64 * 1024**2, free_floor_bytes=4 * 1024**3)
            path = root / 'config.json'
            path.write_text(json.dumps(config))
            with patch('sys.argv', ['worker', '--config', str(path), '--check-config']), \
                    patch('hub_staging_service.__main__.credentials', return_value='synthetic-test-token'), \
                    patch('hub_staging_service.__main__.Service') as service, patch('sys.stdout', new_callable=io.StringIO) as output:
                main()
                assert json.loads(output.getvalue())['valid'] is True
                service.assert_not_called()
            assert not (root / 'unopened').exists()
            config['free_floor_bytes'] = 1024
            path.write_text(json.dumps(config))
            with patch('sys.argv', ['worker', '--config', str(path), '--check-config']), \
                    patch('hub_staging_service.__main__.credentials', return_value='synthetic-test-token'), \
                    self.assertRaises(Rejected):
                main()

    def test_runtime_passes_explicit_limits_to_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.configuration(root)
            config.update(capacity_bytes=64 * 1024**2, free_floor_bytes=4 * 1024**3)
            path = root / 'config.json'
            path.write_text(json.dumps(config))
            with patch('sys.argv', ['worker', '--config', str(path)]), \
                    patch('hub_staging_service.__main__.credentials', return_value='synthetic-test-token'), \
                    patch('hub_staging_service.__main__.Service') as service, \
                    patch('hub_staging_service.__main__.BoundedServer') as server:
                main()
                assert service.call_args.kwargs == {'capacity_bytes': 64 * 1024**2,
                                                    'free_floor_bytes': 4 * 1024**3}
                service.return_value.close.assert_called_once()
                server.return_value.server_close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
