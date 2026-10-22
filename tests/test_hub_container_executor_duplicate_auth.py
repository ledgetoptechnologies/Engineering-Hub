"""Executor auth rejects ambiguity before body reads or adapter access.

Raw ASGI headers avoid relying on proxy/HTTP client header normalization.
No executor, database, daemon or credential store is opened by these tests.
"""
import json

import pytest

from hub_container_executor.asgi import create_app


@pytest.mark.parametrize('method,path', [
    ('POST', '/v1/container-pilot/commands'),
    ('GET', '/v1/container-pilot/commands/00000000-0000-4000-8000-000000000000'),
    ('POST', '/v1/container-pilot/commands/00000000-0000-4000-8000-000000000000/reconcile'),
])
@pytest.mark.parametrize('headers', [
    [(b'authorization', b'Bearer test-only'), (b'authorization', b'Bearer test-only')],
    [(b'authorization', b'Bearer wrong-only'), (b'authorization', b'Bearer test-only')],
    [(b'authorization', b'Bearer test-only'), (b'authorization', b'Bearer wrong-only')],
    [(b'Authorization', b'Bearer test-only'), (b'aUtHoRiZaTiOn', b'Bearer test-only')],
])
@pytest.mark.asyncio
async def test_duplicate_authorization_is_rejected_before_adapter_or_body(method, path, headers):
    class UnavailableAdapter:
        def __getattr__(self, name):
            raise AssertionError('Authentication must reject before adapter access')

    app = create_app(UnavailableAdapter(), bearer_token='test-only')
    sent = []

    async def receive():
        raise AssertionError('Authentication must reject before body reads')

    async def send(message):
        sent.append(message)

    await app(dict(type='http', method=method, path=path,
                   query_string=b'', headers=headers), receive, send)
    assert sent[0]['status'] == 401
    assert json.loads(sent[1]['body']) == {'error': {'code': 'unauthorized'}}
    assert b'test-only' not in sent[1]['body'] and b'wrong-only' not in sent[1]['body']
