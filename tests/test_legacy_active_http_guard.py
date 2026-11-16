"""Default legacy HTTP status must not report idle after journal loss."""
import http.client
import ipaddress
import json
import threading
from uuid import uuid4

from hub_staging_service.__main__ import BoundedServer, handler
from hub_staging_service.core import Service
from tests.test_staging_service_core import Backend, profile


def test_legacy_status_active_journal_loss_is_unavailable(tmp_path):
    service = Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    token = 'synthetic-only-legacy-token'
    server = BoundedServer(('127.0.0.1', 0), handler(
        service, token, {ipaddress.ip_address('127.0.0.1')}))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()

    def status():
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request('GET', '/v1/status', headers={'Authorization': 'Bearer ' + token})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    command, job = str(uuid4()), str(uuid4())
    try:
        code, body = status()
        assert code == 200 and body['active_command_id'] is None

        service.active = command
        code, body = status()
        assert code == 503 and body == {'error': 'worker_status_unavailable'}

        service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
                           (command, 'identity', None, 'running', '{}', '{}'))
        service.db.commit()
        code, body = status()
        assert code == 503 and body == {'error': 'worker_status_unavailable'}

        service.db.execute('UPDATE operations SET job_id=? WHERE command_id=?', (job, command))
        service.db.commit()
        code, body = status()
        assert code == 200 and body['active_command_id'] == command

        # A surviving scoped marker must not look idle if scope tables or
        # configuration were lost, and must not disclose the command ID.
        service.db.execute('UPDATE operations SET payload=? WHERE command_id=?',
                           (json.dumps({'_receiver_principal': 'synthetic'}), command))
        service.db.commit()
        code, body = status()
        assert code == 503 and body == {'error': 'worker_status_unavailable'}
        assert command not in json.dumps(body)

        for invalid_payload in ('{', '[]', 'null', '"text"', '3'):
            service.db.execute('UPDATE operations SET payload=? WHERE command_id=?', (invalid_payload, command))
            service.db.commit()
            code, body = status()
            assert code == 503 and body == {'error': 'worker_status_unavailable'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        service.active = None
        service.close()
