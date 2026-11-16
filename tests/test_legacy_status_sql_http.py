import ipaddress
import sqlite3
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from hub_staging_service.__main__ import handler
from hub_staging_service.core import Service
from tests.test_staging_service_core import Backend, profile


@pytest.mark.parametrize('active', [False, True])
def test_legacy_status_closed_journal_is_http_503_and_auth_still_applies(tmp_path, active):
    service = Service(tmp_path, {'demo': profile()}, Backend())
    if active:
        service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
            ('command-a', 'identity', 'job-a', 'running', '{"project":"demo"}', '{}'))
        service.db.commit()
        service.active = 'command-a'
    token = 'synthetic-legacy-token'
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(service, token, {ipaddress.ip_address('127.0.0.1')}))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/v1/status'
        with httpx.Client(trust_env=False, timeout=5) as client:
            control = client.get(url, headers={'Authorization': f'Bearer {token}'})
            assert control.status_code == 200
            assert control.json()['active_command_id'] == ('command-a' if active else None)
            service.db.close()
            denied = client.get(url, headers={'Authorization': 'Bearer invalid'})
            assert denied.status_code == 401
            response = client.get(url, headers={'Authorization': f'Bearer {token}'})
            assert response.status_code == 503
            assert response.json() == {'error': 'worker_status_unavailable'}
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
        service.close()
        assert not thread.is_alive()


def test_active_status_operation_query_sql_error_is_http_503(tmp_path):
    service = Service(tmp_path, {'demo': profile()}, Backend())
    service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
        ('command-a', 'identity', 'job-a', 'running', '{"project":"demo"}', '{}'))
    service.db.commit()
    service.active = 'command-a'
    original = service.db
    class FaultConnection:
        def execute(self, sql, *args):
            if sql.startswith('SELECT job_id,payload FROM operations WHERE command_id='):
                raise sqlite3.OperationalError('synthetic journal error')
            return original.execute(sql, *args)
        def __getattr__(self, name):
            return getattr(original, name)
    token = 'synthetic-legacy-token'
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(service, token, {ipaddress.ip_address('127.0.0.1')}))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/v1/status'
        with httpx.Client(trust_env=False, timeout=5, headers={'Authorization': f'Bearer {token}'}) as client:
            assert client.get(url).json()['active_command_id'] == 'command-a'
            service.db = FaultConnection()
            response = client.get(url)
            assert response.status_code == 503
            assert response.json() == {'error': 'worker_status_unavailable'}
    finally:
        service.db = original
        server.shutdown()
        thread.join(5)
        server.server_close()
        service.close()
        assert not thread.is_alive()
