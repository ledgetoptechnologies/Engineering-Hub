"""Native backend gateway delivery: fake ASGI/RPC only, never a real model."""
import asyncio
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/hermes-mcp-wake/dashboard'))
from wake_backend import ASGIGateway, BackendDispatcher

@pytest.mark.asyncio
async def test_authenticated_asgi_rpc_uses_existing_gateway_and_survives_notifications():
    observed = []
    async def app(scope, receive, send):
        observed.append(scope)
        assert (await receive())['type'] == 'websocket.connect'
        # A running backend binds Host explicitly, unlike an unstarted fake app.
        assert dict(scope['headers']).get(b'host') == b'127.0.0.1'
        await send({'type': 'websocket.accept'})
        while True:
            frame = await receive()
            if frame['type'] == 'websocket.disconnect':
                return
            import json
            req = json.loads(frame['text'])
            await send({'type': 'websocket.send', 'text': json.dumps({'method': 'event', 'params': {}})})
            await send({'type': 'websocket.send', 'text': json.dumps({'id': req['id'], 'result': {'session_id': 'exact-runtime'}})})
    gateway = ASGIGateway(app, lambda: 'fake-token')
    try:
        assert await gateway.call('session.resume', {'session_id': 'selected-stored'}) == {'session_id': 'exact-runtime'}
        assert await gateway.call('session.resume', {'session_id': 'another-selected'}) == {'session_id': 'exact-runtime'}
        assert len(observed) == 1  # persistent gateway, no subprocess or network client
        assert observed[0]['path'] == '/api/ws'
        assert observed[0]['query_string'] == b'token=fake-token'
    finally:
        await gateway.close()

class Manager:
    def __init__(self):
        self.completed = []
        self.failed = []
    async def claim_dispatch(self, dispatch_id):
        return {'claim_token': 'test-claim', 'binding': {'stored_session_id': 'selected-stored', 'topic': 'alpha', 'owner': {'connection_id': 'local', 'profile': 'default', 'target_profile': 'default'}}, 'messages': [{'id': 'm-1', 'body': {'topic': 'alpha', 'text': 'untrusted'}}]}
    async def complete_dispatch(self, dispatch_id, token):
        self.completed.append(dispatch_id)
    def fail_dispatch(self, dispatch_id, token):
        self.failed.append(dispatch_id)

@pytest.mark.asyncio
async def test_backend_resumes_exact_binding_then_queues_without_renderer():
    calls = []
    class Gateway:
        async def call(self, method, params):
            calls.append((method, params))
            return {'session_id': 'resolved-runtime'} if method == 'session.resume' else {'status': 'queued'}
        async def close(self): pass
    manager = Manager()
    dispatch = BackendDispatcher(manager, Gateway())
    dispatch.enqueue('d-1')
    dispatch.enqueue('d-1')
    await dispatch.drain()
    assert [c[0] for c in calls] == ['session.resume', 'prompt.submit']
    assert calls[0][1]['session_id'] == 'selected-stored'
    assert calls[1][1]['session_id'] == 'resolved-runtime'
    assert calls[1][1]['queued'] is True
    # prompt.submit has a closed contract: source is valid on resume, NOT submit.
    assert 'source' not in calls[1][1]
    assert 'not owner authorization' in calls[1][1]['text']
    assert manager.completed == ['d-1']

@pytest.mark.asyncio
async def test_failed_or_invalid_resume_never_submits_or_marks_complete():
    calls = []
    class Gateway:
        async def call(self, method, params):
            calls.append(method)
            return {}  # missing exact runtime must fail closed
        async def close(self): pass
    manager = Manager()
    dispatch = BackendDispatcher(manager, Gateway())
    dispatch.enqueue('d-1')
    await dispatch.drain()
    assert calls == ['session.resume']
    assert manager.completed == []
    assert manager.failed == ['d-1']
