import asyncio
import hashlib
import json
from pathlib import Path
import ssl
import time

import httpx
import pytest


from engineering_hub import container_pilot_client as client_module
from engineering_hub import executor_connection_check as cli

TOKEN = 'isolated-test-value-not-live'
UUID = '01812ed4-729b-4740-8772-793c921f38c9'


def run_probe(handler, timeout=10):
    async def operation():
        client = client_module.ContainerPilotClient('http://127.0.0.1', TOKEN,
            transport=httpx.MockTransport(handler), timeout_seconds=timeout)
        try:
            return await client.probe_connection(UUID)
        finally:
            await client.aclose()
    return asyncio.run(operation())


def test_exact_two_gets_preserve_only_second_bearer():
    seen = []
    def handler(request):
        seen.append(request)
        code, error = (401, 'unauthorized') if len(seen) == 1 else (404, 'command_not_found')
        return httpx.Response(code, json={'error': {'code': error}})
    result = run_probe(handler)
    assert len(seen) == 2 and all(r.method == 'GET' for r in seen)
    assert seen[0].url == seen[1].url
    assert not seen[0].content and not seen[1].content
    assert 'authorization' not in seen[0].headers
    assert seen[1].headers['authorization'] == 'Bearer ' + TOKEN
    assert result['state'] == 'transport_auth_verified'
    assert result['agent_token_reuse_checked'] is False
    assert result['workflow_verified'] is False


@pytest.mark.parametrize('status,body', [
    (302, b'{}'), (200, b'{}'), (401, b'{"error":{"code":"unauthorized","code":"unauthorized"}}'),
    (401, b'{"error":{"code":"unauthorized"},"extra":1}'),
    (401, b'x' * 16385), (401, ('{"error":{"code":"' + TOKEN + '"}}').encode()),
    (401, b'{"error":{"code":"unauthorized"},"n":NaN}'),
])
def test_failures_do_not_retry_or_expose_input(status, body):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, content=body, headers={'location': '/elsewhere'})
    with pytest.raises(client_module.ContainerPilotClientError) as error:
        run_probe(handler)
    assert str(error.value) == 'Executor connection check failed'
    assert len(seen) == 1


def test_second_wrong_auth_response_fails():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(401, json={'error': {'code': 'unauthorized'}})
    with pytest.raises(client_module.ContainerPilotClientError):
        run_probe(handler)
    assert len(seen) == 2


def test_one_deadline_spans_both_body_reads():
    seen = []
    class Slow(httpx.AsyncByteStream):
        def __init__(self, raw): self.raw = raw
        async def __aiter__(self):
            await asyncio.sleep(0.04)
            yield self.raw
    def handler(request):
        seen.append(request)
        status, code = (401, 'unauthorized') if len(seen) == 1 else (404, 'command_not_found')
        return httpx.Response(status, stream=Slow(json.dumps({'error': {'code': code}}).encode()))
    with pytest.raises(client_module.ContainerPilotClientError):
        run_probe(handler, timeout=0.06)
    assert len(seen) == 2


def test_invalid_uuid_causes_no_request():
    seen = []
    async def operation():
        client = client_module.ContainerPilotClient('http://127.0.0.1', TOKEN,
            transport=httpx.MockTransport(lambda request: seen.append(request)))
        try:
            with pytest.raises(ValueError): await client.probe_connection('not-a-uuid')
        finally: await client.aclose()
    asyncio.run(operation())
    assert seen == []


def args_for(token=TOKEN.encode(), ca=b'public test CA'):
    return ['--origin', 'https://192.168.50.90:9443', '--ca-file', '/private/ca.pem',
        '--ca-sha256', 'sha256:' + hashlib.sha256(ca).hexdigest(),
        '--credential-file', '/private/executor-token', '--credential-sha256',
        'sha256:' + hashlib.sha256(token).hexdigest(), '--unknown-command-id', UUID]


def test_cli_uses_protected_reader_and_existing_https_factory(monkeypatch):
    reads, calls = [], []
    def reader(path, **kwargs):
        reads.append((str(path), kwargs))
        return TOKEN.encode() if str(path).endswith('executor-token') else b'public test CA'
    class Fake:
        @classmethod
        def for_verified_https(cls, origin, token, ca, **kwargs):
            calls.append((origin, token, ca, kwargs)); return cls()
        async def probe_connection(self, command):
            assert command == UUID; return {'state': 'transport_auth_verified'}
        async def aclose(self): calls.append('closed')
    monkeypatch.setattr(cli, 'read_protected_file', reader)
    monkeypatch.setattr(cli, 'ContainerPilotClient', Fake)
    assert asyncio.run(cli.check(cli.parse(args_for()))) == {'state': 'transport_auth_verified'}
    assert reads[0][1] == {'maximum_bytes': 4096}
    assert reads[1][1] == {'maximum_bytes': 1048576, 'private': False}
    assert calls[0][-1] == {'timeout_seconds': 10.0}
    assert calls[-1] == 'closed'


@pytest.mark.parametrize('extra', [['--token', TOKEN], ['--unknown', 'value'], ['--origin-other', 'x']])
def test_closed_cli_sanitizes_errors(extra, capsys):
    assert cli.main(args_for() + extra) == 2
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert '/private/' not in output.out + output.err
    assert json.loads(output.out)['state'] == 'connection_check_failed'


def test_digest_mismatch_precedes_transport(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'read_protected_file', lambda *a, **k: b'wrong')
    class Forbidden:
        @classmethod
        def for_verified_https(cls, *a, **k): raise AssertionError('must not allocate')
    monkeypatch.setattr(cli, 'ContainerPilotClient', Forbidden)
    assert cli.main(args_for()) == 2
    assert 'wrong' not in capsys.readouterr().out


def test_response_cleanup_bounded_after_exchange_timeout():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.1)
            yield b'{}'
        async def aclose(self): await asyncio.sleep(0.15)
    started = time.monotonic()
    with pytest.raises(client_module.ContainerPilotClientError):
        run_probe(lambda r: httpx.Response(401, stream=Slow()), timeout=0.01)
    assert time.monotonic() - started < 0.12


def test_response_cleanup_failure_cannot_report_success():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self): yield b'{"error":{"code":"unauthorized"}}'
        async def aclose(self): await asyncio.sleep(0.15)
    with pytest.raises(client_module.ContainerPilotClientError):
        run_probe(lambda r: httpx.Response(401, stream=Slow()), timeout=0.01)


def test_probe_preserves_caller_cancellation_during_bounded_cleanup():
    async def operation():
        entered = asyncio.Event()
        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.sleep(10)
                yield b'{}'
            async def aclose(self): await asyncio.sleep(0.15)
        client = client_module.ContainerPilotClient('http://127.0.0.1', TOKEN,
            transport=httpx.MockTransport(lambda r: httpx.Response(401, stream=Slow())), timeout_seconds=0.02)
        try:
            task = asyncio.create_task(client.probe_connection(UUID))
            await entered.wait(); task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        finally: await client.aclose()
    asyncio.run(operation())


def test_cli_client_close_bounded_and_failure_not_success(monkeypatch):
    monkeypatch.setattr(cli, 'CLOSE_TIMEOUT_SECONDS', 0.01)
    monkeypatch.setattr(cli, 'read_protected_file', lambda path, **k:
        TOKEN.encode() if str(path).endswith('executor-token') else b'public test CA')
    class Fake:
        @classmethod
        def for_verified_https(cls, *a, **k): return cls()
        async def probe_connection(self, command): return {'state': 'transport_auth_verified'}
        async def aclose(self): await asyncio.sleep(0.15)
    monkeypatch.setattr(cli, 'ContainerPilotClient', Fake)
    started = time.monotonic()
    with pytest.raises(TimeoutError): asyncio.run(cli.check(cli.parse(args_for())))
    assert time.monotonic() - started < 0.12


def test_cli_preserves_caller_cancellation_even_when_close_times_out(monkeypatch):
    monkeypatch.setattr(cli, 'CLOSE_TIMEOUT_SECONDS', 0.01)
    monkeypatch.setattr(cli, 'read_protected_file', lambda path, **k:
        TOKEN.encode() if str(path).endswith('executor-token') else b'public test CA')
    async def operation():
        entered = asyncio.Event()
        class Fake:
            @classmethod
            def for_verified_https(cls, *a, **k): return cls()
            async def probe_connection(self, command):
                entered.set(); await asyncio.sleep(10)
            async def aclose(self): await asyncio.sleep(0.15)
        monkeypatch.setattr(cli, 'ContainerPilotClient', Fake)
        task = asyncio.create_task(cli.check(cli.parse(args_for())))
        await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(operation())


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['match', 'wrong-ca', 'wrong-host'])
async def test_probe_over_real_loopback_tls(case, tmp_path):
    # Real TLS and the shared transport, but synthetic protocol responses only.
    # This does not establish a NAS route or an enrolled executor identity.
    from tests.test_hub_unified_pilot_startup import certificate
    ca, key = certificate()
    certificate_file, key_file = tmp_path / 'probe-cert.pem', tmp_path / 'probe-key.pem'
    certificate_file.write_bytes(ca)
    key_file.write_bytes(key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_file, key_file)
    seen, handlers, failures = [], set(), []

    async def handler(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            async with asyncio.timeout(2):
                raw = await reader.readuntil(b'\r\n\r\n')
                lines = raw.decode('ascii').split('\r\n')
                assert lines[0] == 'GET /v1/container-pilot/commands/' + UUID + ' HTTP/1.1'
                headers = dict(line.split(': ', 1) for line in lines[1:] if line)
                authorization = {name.lower(): value for name, value in headers.items()}.get('authorization')
                seen.append(authorization)
                if len(seen) == 1:
                    assert authorization is None
                    status, reason, code = 401, 'Unauthorized', 'unauthorized'
                else:
                    assert len(seen) == 2 and authorization == 'Bearer ' + TOKEN
                    status, reason, code = 404, 'Not Found', 'command_not_found'
                body = json.dumps({'error': {'code': code}}).encode()
                writer.write(('HTTP/1.1 ' + str(status) + ' ' + reason + '\r\n'
                    'Content-Type: application/json\r\nContent-Length: ' + str(len(body)) +
                    '\r\nConnection: close\r\n\r\n').encode() + body)
                await writer.drain()
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            finally:
                handlers.discard(task)

    listener = await asyncio.start_server(handler, '127.0.0.1', 0, ssl=context)
    port = listener.sockets[0].getsockname()[1]
    host = '127.0.0.1' if case == 'wrong-host' else 'localhost'
    trusted = certificate('other-probe-ca')[0] if case == 'wrong-ca' else ca
    client = client_module.ContainerPilotClient.for_verified_https(
        f'https://{host}:{port}', TOKEN, trusted, timeout_seconds=2)
    try:
        if case == 'match':
            result = await client.probe_connection(UUID)
            assert result['state'] == 'transport_auth_verified'
            assert result['api_get_count'] == 2
            assert seen == [None, 'Bearer ' + TOKEN]
        else:
            with pytest.raises(client_module.ContainerPilotClientError):
                await client.probe_connection(UUID)
            assert seen == []
    finally:
        await client.aclose()
        listener.close()
        await listener.wait_closed()
        if handlers:
            async with asyncio.timeout(2):
                await asyncio.gather(*tuple(handlers))
    assert failures == []
