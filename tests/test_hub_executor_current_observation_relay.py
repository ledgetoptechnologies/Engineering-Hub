"""Synthetic local relay checks: no installed issuer, host or NAS acceptance."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time

import pytest
import httpx

if sys.platform != 'linux':
    pytest.skip('Linux UDS peer credentials', allow_module_level=True)

from hub_container_executor.asgi import CURRENT_OBSERVATION, create_app
from hub_container_executor.launch_transport import (
    CURRENT_REQUEST_PROTOCOL, UnixCurrentObservationTransport,
)
from hub_container_executor.runtime_identity import RuntimeIdentityError
from tests.test_hub_container_executor_service import owned, start, origin, TOKEN as SERVICE_TOKEN


TOKEN = 'synthetic-observation-only-token'
NONCE = 'a' * 64
RAW = b'{"payload":{},"signature":"synthetic-not-authority"}'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def read_exact(peer, count):
    raw = b''
    while len(raw) < count:
        part = peer.recv(count - len(raw))
        if not part:
            raise EOFError()
        raw += part
    return raw


@contextmanager
def issuer(handler, count=1):
    errors, requests = [], []
    with tempfile.TemporaryDirectory(prefix='eh-observe-', dir='/tmp') as directory:
        path = str(Path(directory) / 'issuer.sock')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(path)
            listener.listen(2)
            listener.settimeout(2)
            def run():
                try:
                    for _ in range(count):
                        peer, _ = listener.accept()
                        with peer:
                            peer.settimeout(1)
                            size = struct.unpack('!I', read_exact(peer, 4))[0]
                            assert 0 < size <= 256
                            raw = read_exact(peer, size)
                            value = json.loads(raw)
                            assert raw == canonical(value)
                            assert value == {'protocol': CURRENT_REQUEST_PROTOCOL, 'nonce': value['nonce']}
                            assert peer.recv(1) == b''
                            requests.append(value)
                            handler(peer)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except BaseException as error:
                    errors.append(error)
            worker = threading.Thread(target=run)
            worker.start()
            try:
                yield path, requests
            finally:
                worker.join(3)
                assert not worker.is_alive()
                assert not errors


def transport(path, **changes):
    args = dict(socket_path=path, expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    return UnixCurrentObservationTransport(**(args | changes))


def test_repeated_real_uds_exchanges_are_distinct_from_receipt_and_close():
    def reply(peer):
        peer.sendall(struct.pack('!I', len(RAW)) + RAW)
    with issuer(reply, count=2) as (path, requests):
        client = transport(path)
        assert not hasattr(client, 'issue_receipt')
        assert client.observe_current_launch(nonce=NONCE, timeout_ms=1000) == RAW
        assert client.observe_current_launch(nonce='b' * 64, timeout_ms=1000) == RAW
        client.close()
        with pytest.raises(RuntimeIdentityError):
            client.observe_current_launch(nonce='c' * 64, timeout_ms=1000)
    assert [r['nonce'] for r in requests] == [NONCE, 'b' * 64]


@pytest.mark.parametrize('reply', [b'', b'{}', b'{"payload":{},"payload":{},"signature":"x"}',
    b'{"payload": {},"signature":"x"}', b'{"payload":{},"signature":"x","extra":1}',
    b'{"payload":{"bad":1.1},"signature":"x"}', b'x' * 8193])
def test_invalid_envelope_denied_without_retry(reply):
    with issuer(lambda peer: peer.sendall(struct.pack('!I', len(reply)) + reply)) as (path, requests):
        with pytest.raises(RuntimeIdentityError):
            transport(path).observe_current_launch(nonce=NONCE, timeout_ms=1000)
    assert len(requests) == 1


def test_trailing_frame_and_slow_eof_denied():
    for suffix in (b'x', None):
        def reply(peer):
            peer.sendall(struct.pack('!I', len(RAW)) + RAW)
            if suffix is None:
                time.sleep(.15)
            else:
                peer.sendall(suffix)
        with issuer(reply) as (path, requests):
            with pytest.raises(RuntimeIdentityError):
                transport(path).observe_current_launch(nonce=NONCE, timeout_ms=100)
        assert len(requests) == 1


@pytest.mark.parametrize('nonce,budget', [('A' * 64, 1000), ('a', 1000), (NONCE, True),
    (NONCE, 0), (NONCE, 10001)])
def test_invalid_arguments_before_socket(nonce, budget):
    with pytest.raises(RuntimeIdentityError):
        transport('/tmp/nonexistent-synthetic-observer.sock').observe_current_launch(nonce=nonce, timeout_ms=budget)


class Observer:
    def __init__(self, raw=RAW):
        self.raw, self.calls = raw, []
        self.started, self.release = threading.Event(), threading.Event()
        self.block = False
    def observe_current_launch(self, *, nonce, timeout_ms):
        self.calls.append((nonce, timeout_ms, threading.get_ident()))
        self.started.set()
        if self.block and not self.release.wait(2):
            raise TimeoutError()
        if isinstance(self.raw, BaseException):
            raise self.raw
        return self.raw


async def invoke(app, raw=None, *, headers=None, query=b'', method='POST', path=CURRENT_OBSERVATION,
                 delay=0):
    raw = canonical({'nonce': NONCE}) if raw is None else raw
    if headers is None:
        headers = [(b'authorization', ('Bearer ' + TOKEN).encode()),
            (b'content-type', b'application/json'), (b'content-length', str(len(raw)).encode())]
    events = []
    async def receive():
        if delay:
            await asyncio.sleep(delay)
        return dict(type='http.request', body=raw, more_body=False)
    async def send(event):
        events.append(event)
    await app(dict(type='http', method=method, path=path, query_string=query, headers=headers), receive, send)
    return events


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.001)


async def test_authenticated_route_passes_nonce_only_and_preserves_canonical_bytes():
    observer = Observer()
    app = create_app(object(), bearer_token=TOKEN, current_observation_transport=observer)
    events = await invoke(app)
    assert events[0]['status'] == 200 and events[-1]['body'] == RAW
    assert dict(events[0]['headers'])[b'content-length'] == str(len(RAW)).encode()
    assert dict(events[0]['headers'])[b'cache-control'] == b'no-store'
    assert dict(events[0]['headers'])[b'connection'] == b'close'
    nonce, budget, worker = observer.calls[0]
    assert nonce == NONCE and 100 <= budget <= 2000 and worker != threading.get_ident()


@pytest.mark.parametrize('raw', [b'{}', b'{"nonce":"a"}', canonical({'nonce': NONCE, 'pid': 1}),
    ('{"nonce":"' + NONCE + '","nonce":"' + NONCE + '"}').encode(), b'x' * 257])
async def test_bad_request_never_calls_observer(raw):
    observer = Observer()
    events = await invoke(create_app(object(), bearer_token=TOKEN, current_observation_transport=observer), raw)
    assert events[0]['status'] in (400, 413)
    assert observer.calls == []


@pytest.mark.parametrize('changes,expected', [({'headers': []}, 401),
    ({'query': b'x=1'}, 404), ({'method': 'GET'}, 404), ({'path': CURRENT_OBSERVATION + '/'}, 404)])
async def test_auth_method_and_query_boundaries(changes, expected):
    observer = Observer()
    events = await invoke(create_app(object(), bearer_token=TOKEN, current_observation_transport=observer), **changes)
    assert events[0]['status'] == expected and not observer.calls


async def test_unconfigured_route_stays_unavailable():
    assert (await invoke(create_app(object(), bearer_token=TOKEN)))[0]['status'] == 404


@pytest.mark.parametrize('raw', [b'{}', b'{"payload": {},"signature":"x"}', b'x' * 8193,
    ValueError('sensitive fixture error')])
async def test_bad_callback_response_is_sanitized(raw):
    observer = Observer(raw)
    events = await invoke(create_app(object(), bearer_token=TOKEN, current_observation_transport=observer))
    assert events[0]['status'] == 503
    assert events[-1]['body'] == b'{"error":{"code":"observation_unavailable"}}'


async def test_body_deadline_not_renewed_and_command_gate_is_separate():
    observer = Observer()
    app = create_app(object(), bearer_token=TOKEN, current_observation_transport=observer,
        current_observation_timeout_ms=100)
    assert (await invoke(app, delay=.15))[0]['status'] == 408
    assert observer.calls == []
    assert app._operation.acquire(blocking=False)
    try:
        assert (await invoke(app))[0]['status'] == 200
        assert 1 <= observer.calls[-1][1] < 100
    finally:
        app._operation.release()


async def test_late_event_loop_resumption_cannot_report_success(monkeypatch):
    # Scheduling delay after a timely worker result must not renew the original
    # request window. A logical clock makes the regression deterministic without
    # sleeping or assuming the event loop's callback ordering across machines.
    import hub_container_executor.asgi as relay_module
    from types import SimpleNamespace
    now = [100.0]
    finish = relay_module.finish_owned
    async def late_resumption(future):
        result = await finish(future)  # Real retained worker must finish first.
        now[0] = 100.2  # Model loop scheduling delay after timely worker success.
        return result
    monkeypatch.setattr(relay_module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(relay_module, 'finish_owned', late_resumption)
    observer = Observer()
    app = create_app(object(), bearer_token=TOKEN, current_observation_transport=observer,
        current_observation_timeout_ms=100)
    events = await invoke(app)
    assert 1 <= observer.calls[0][1] <= 100  # Worker admitted within its original budget.
    assert events[0]['status'] == 503
    assert events[-1]['body'] == b'{"error":{"code":"observation_unavailable"}}'
    assert not app._observation_operation.locked()


async def test_cancellation_retains_observation_gate_until_thread_drain():
    observer = Observer()
    observer.block = True
    app = create_app(object(), bearer_token=TOKEN, current_observation_transport=observer)
    task = asyncio.create_task(invoke(app))
    try:
        await until(observer.started.is_set)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        assert (await invoke(app))[0]['status'] == 503
        assert len(observer.calls) == 1
        observer.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        observer.block = False
        assert (await invoke(app))[0]['status'] == 200
    finally:
        observer.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('change', ['duplicate-auth', 'duplicate-length', 'transfer', 'missing-length',
    'negative-length', 'incorrect-length', 'duplicate-type', 'wrong-type'])
async def test_relay_closed_header_and_framing_contract(change):
    observer = Observer()
    raw = canonical({'nonce': NONCE})
    headers = [(b'authorization', ('Bearer ' + TOKEN).encode()),
        (b'content-type', b'application/json'), (b'content-length', str(len(raw)).encode())]
    if change == 'duplicate-auth':
        headers.append(headers[0])
    elif change == 'duplicate-length':
        headers.append(headers[2])
    elif change == 'transfer':
        headers.append((b'transfer-encoding', b'chunked'))
    elif change == 'missing-length':
        headers.pop()
    elif change == 'negative-length':
        headers[-1] = (b'content-length', b'-1')
    elif change == 'incorrect-length':
        headers[-1] = (b'content-length', b'1')
    elif change == 'duplicate-type':
        headers.append(headers[1])
    else:
        headers[1] = (b'content-type', b'text/plain')
    events = await invoke(create_app(object(), bearer_token=TOKEN, current_observation_transport=observer),
        raw, headers=headers)
    assert events[0]['status'] in (400, 401)
    assert observer.calls == []


def test_wrong_uid_symlink_and_inherited_transport_denied_before_request(tmp_path, monkeypatch):
    path = str(tmp_path / 'issuer.sock')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen(1)
        client = transport(path, expected_issuer_uid=os.geteuid() + 1)
        with pytest.raises(RuntimeIdentityError):
            client.observe_current_launch(nonce=NONCE, timeout_ms=1000)
        peer, _ = listener.accept()
        with peer:
            assert peer.recv(1) == b''
        alias = tmp_path / 'alias.sock'
        alias.symlink_to(path)
        with pytest.raises(RuntimeIdentityError):
            transport(str(alias)).observe_current_launch(nonce=NONCE, timeout_ms=1000)
        inherited = transport(path)
        inherited._pid += 1
        with pytest.raises(RuntimeIdentityError):
            inherited.observe_current_launch(nonce=NONCE, timeout_ms=1000)


def test_no_queue_while_an_exchange_is_in_flight():
    started, release = threading.Event(), threading.Event()
    def reply(peer):
        started.set()
        assert release.wait(1)
        peer.sendall(struct.pack('!I', len(RAW)) + RAW)
    with issuer(reply) as (path, requests):
        client = transport(path)
        results = []
        worker = threading.Thread(target=lambda: results.append(client.observe_current_launch(nonce=NONCE, timeout_ms=1000)))
        worker.start()
        try:
            assert started.wait(1)
            with pytest.raises(RuntimeIdentityError):
                client.observe_current_launch(nonce='b' * 64, timeout_ms=1000)
            client.close()  # Accepted exchange still owns its socket until drain.
        finally:
            release.set()
            worker.join(2)
        assert not worker.is_alive() and results == [RAW] and len(requests) == 1


async def test_real_owned_service_exposes_only_explicit_relay_and_drains(owned):
    observer = Observer()
    observer.block = True
    stop, serving, address = await start(owned, current_observation_transport=observer)
    request = None
    try:
        async with httpx.AsyncClient(base_url=origin(address), trust_env=False) as client:
            assert (await client.post(CURRENT_OBSERVATION, json={'nonce': NONCE})).status_code == 401
            request = asyncio.create_task(client.post(CURRENT_OBSERVATION,
                headers={'Authorization': 'Bearer ' + SERVICE_TOKEN}, json={'nonce': NONCE}))
            await until(observer.started.is_set)
            serving.cancel()
            await asyncio.sleep(.03)
            assert not serving.done() and owned.provider_closes == 0
            observer.release.set()
            response = await asyncio.wait_for(request, 2)
            assert response.status_code == 200 and response.content == RAW
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(serving, 8)
        assert owned.provider_closes == 1 and owned.adapter_closes == 1
        assert not owned.backend.events
    finally:
        observer.release.set()
        stop.set()
        await asyncio.gather(serving, *([request] if request is not None else []), return_exceptions=True)


@pytest.mark.parametrize('options', [{'current_observation_timeout_ms': True},
    {'current_observation_timeout_ms': 99}, {'current_observation_transport': object()}])
async def test_invalid_observation_composition_denied_before_listener_or_providers(owned, monkeypatch, options):
    monkeypatch.setattr(owned.module, '_bind', lambda *a: pytest.fail('No listener construction'))
    with pytest.raises(ValueError):
        await owned.module.serve_owned_host_executor(owned.factory, bearer_token=SERVICE_TOKEN,
            supervisor_stop_grace_seconds=2000, port=0, install_signals=False, **options)
    assert owned.entered == 0


async def test_actual_nginx_tls_to_uds_nonce_route_auth_and_no_adapter_effects():
    from tests.test_hub_executor_nginx_wire import proxy_fixture, raw_request
    observer = Observer()
    body = canonical({'nonce': NONCE})
    async with proxy_fixture(current_observation_transport=observer) as rig:
        status, response = await raw_request(rig, 'POST', CURRENT_OBSERVATION, body=body)
        assert status == 200 and response.endswith(RAW)
        assert b'Content-Length: ' + str(len(RAW)).encode() in response or (
            b'content-length: ' + str(len(RAW)).encode() in response)
        assert len(observer.calls) == 1 and not rig['adapter'].calls
        for method, path, auth, expected in [
                ('POST', CURRENT_OBSERVATION, (), 401),
                ('GET', CURRENT_OBSERVATION, None, 404),
                ('POST', CURRENT_OBSERVATION + '?x=1', None, 404),
                ('POST', CURRENT_OBSERVATION + '/', None, 404)]:
            args = {} if auth is None else {'auth': auth}
            assert (await raw_request(rig, method, path, body=body, **args))[0] == expected
        assert len(observer.calls) == 1 and not rig['adapter'].calls
