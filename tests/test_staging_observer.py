import http.client
import ipaddress
import json
import threading
import sys

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from engineering_hub.staging_runtime import validate_runtime_snapshot
from hub_staging_service.__main__ import BoundedServer
from hub_staging_service import observer
from hub_staging_service.runtime_inventory import _empty, _source, _timestamp


def test_snapshot_validates_freshness_and_never_collects():
    now = datetime.now(timezone.utc)
    host = _empty(now)
    host['sources'] = [_source('host', 'observed', _timestamp(now))]
    with patch.object(observer, '_read_host', return_value=host):
        result = observer.snapshot('/unused')
    validate_runtime_snapshot(result)
    assert result['sources'][0]['state'] == 'not_configured'
    assert result['sources'][1]['state'] == 'observed'
    host['observed_at'] = _timestamp(now - timedelta(seconds=100))
    host['sources'][0]['observed_at'] = host['observed_at']
    with patch.object(observer, '_read_host', return_value=host):
        assert observer.snapshot('/unused')['sources'][1]['state'] == 'stale'
    with patch.object(observer, '_read_host', side_effect=ValueError()):
        assert observer.snapshot('/unused')['sources'][1]['state'] == 'unavailable'


def test_http_auth_exact_route_and_no_mutations():
    token = 'fake-observer-token-only'
    server = BoundedServer(('127.0.0.1', 0), observer.handler('/unused', token, {ipaddress.ip_address('127.0.0.1')}))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    def request(method, path, headers=None):
        conn = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()
    try:
        with patch.object(observer, 'snapshot', return_value={'safe': True}) as collect:
            assert request('GET', '/v1/runtime')[0] == 401
            collect.assert_not_called()
            headers = {'Authorization': 'Bearer ' + token}
            assert request('GET', '/v1/runtime', headers) == (200, b'{"safe":true}')
            assert request('GET', '/v1/runtime?x=1', headers)[0] == 404
            assert request('POST', '/v1/jobs', headers)[0] == 501
            assert collect.call_count == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_main_rejects_unrestricted_identity_before_access(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['observer', '--snapshot', str(Path.cwd() / 'snapshot'),
                                    '--token-file', str(Path.cwd() / 'token'), '--port', '8781'])
    monkeypatch.setattr(observer, 'restricted_identity', lambda: False)
    with patch.object(observer, 'read_observer_credential') as credentials, \
            patch.object(observer, 'BoundedServer') as server:
        with pytest.raises(SystemExit) as exc:
            observer.main()
        assert exc.value.code == 2
        credentials.assert_not_called()
        server.assert_not_called()


def test_main_accepts_nonroot_identity_and_closes_server(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['observer', '--snapshot', str(Path.cwd() / 'snapshot'),
                                    '--token-file', str(Path.cwd() / 'token'), '--port', '8781'])
    monkeypatch.setattr(observer.os, 'geteuid', lambda: 12345, raising=False)
    monkeypatch.setattr(observer, 'restricted_identity', lambda: True)
    with patch.object(observer, 'read_observer_credential', return_value='fake-observer-token-only') as credentials, \
            patch.object(observer, 'BoundedServer') as server:
        observer.main()
        credentials.assert_called_once()
        server.return_value.serve_forever.assert_called_once_with()
        server.return_value.server_close.assert_called_once_with()
