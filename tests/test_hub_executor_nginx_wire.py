"""Optional actual nginx TLS->UDS smoke, with synthetic certs and no host mounts.

Requires a separately supplied trusted nginx binary. The validation image uses
the official Docker Library binary+private libraries; no application proxy is
installed by these tests. This is not installed-host/NAS acceptance.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
from uuid import uuid4

import pytest
import uvicorn

if sys.platform != 'linux':
    pytest.skip('Real nginx and Unix socket fixture requires Linux', allow_module_level=True)

from engineering_hub.container_pilot_client import ContainerPilotRequest, ContainerPilotResponse
from hub_container_executor.asgi import PilotASGI
from tests.test_hub_unified_pilot_startup import certificate
from tests.test_hub_container_pilot import pilot_manifest


TEMPLATE = Path(__file__).resolve().parents[1] / 'docker/nginx-executor-https.conf.template'
TOKEN = 'synthetic-private-executor-test-token'
ROOT = '/v1/container-pilot/commands'


def nginx_command():
    isolated = Path('/opt/nginx-runtime')
    if (isolated / 'nginx').is_file():
        return [str(isolated / 'ld-linux-x86-64.so.2'), '--library-path', str(isolated / 'lib'), str(isolated / 'nginx')]
    executable = shutil.which('nginx')
    if executable is None:
        pytest.skip('Optional actual nginx binary not supplied')
    return [executable]


def command():
    manifest = pilot_manifest()
    return ContainerPilotRequest(schema_version='container_pilot_v1', command_id=str(uuid4()),
        run_id=str(uuid4()), target_id='engineering-hub:staging', action='deploy_container',
        manifest_digest=manifest.digest(), candidate_commit=manifest.candidate.source_commit,
        policy_revision=manifest.policy_revision, fencing_generation=1)


class FixtureAdapter:
    def __init__(self):
        self.calls = []
        self.receipts = {}

    def submit(self, request):
        self.calls.append(('submit', request.command_id))
        result = ContainerPilotResponse(**request.model_dump(), state='held', reason='daemon_unavailable',
            evidence=dict(container_id=None, image_config_id=None, health='unknown', evidence_verified=False),
            replayed=False)
        self.receipts[request.command_id] = result
        return result

    def get(self, identity):
        self.calls.append(('get', identity))
        return self.receipts.get(identity)

    def reconcile(self, identity):
        self.calls.append(('reconcile', identity))
        return self.receipts.get(identity)


@asynccontextmanager
async def proxy_fixture():
    nginx = nginx_command()
    with tempfile.TemporaryDirectory(prefix='eh-nx-', dir='/tmp') as directory:
        root = Path(directory)
        ca, key = certificate()
        cert_path, key_path = root / 'server.pem', root / 'server.key'
        cert_path.write_bytes(ca); key_path.write_bytes(key); key_path.chmod(0o600)
        socket_path = root / 'executor.sock'
        adapter = FixtureAdapter()
        application = PilotASGI(adapter, TOKEN)
        state = dict(seen=[], fail=False)
        async def observed(scope, receive, send):
            state['seen'].append((scope['method'], scope['path'], scope['headers']))
            if state['fail']:
                await send({'type': 'http.response.start', 'status': 503, 'headers': [(b'content-length', b'0')]})
                await send({'type': 'http.response.body', 'body': b''})
            else:
                await application(scope, receive, send)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path)); listener.setblocking(False)
        socket_path.chmod(0o600)
        server = uvicorn.Server(uvicorn.Config(observed, lifespan='off', interface='asgi3',
            loop='asyncio', http='h11', ws='none', access_log=False, proxy_headers=False,
            log_config=None, timeout_graceful_shutdown=None))
        backend = asyncio.create_task(server.serve(sockets=[listener]))
        process = None
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if backend.done():
                        await backend
                        pytest.fail('Synthetic UDS server stopped')
                    await asyncio.sleep(0.001)
            with socket.socket() as selected:
                selected.bind(('127.0.0.1', 0))
                port = selected.getsockname()[1]
            rendered = TEMPLATE.read_text()
            for placeholder, value in {
                '__EXECUTOR_UDS_PATH__': str(socket_path), '__STAGING_BIND_IP__': '127.0.0.1',
                '__EXECUTOR_DNS_NAME__': 'localhost', '__CERTIFICATE_CHAIN_PATH__': str(cert_path),
                '__CERTIFICATE_KEY_PATH__': str(key_path), '__APPROVED_NAS_IP__': '127.0.0.1',
            }.items():
                rendered = rendered.replace(placeholder, value)
            rendered = rendered.replace(':9443 ssl', ':' + str(port) + ' ssl')
            config = root / 'nginx.conf'
            # Test-only writable paths; no production files, host sockets or ports.
            config.write_text('worker_processes 1;\nerror_log stderr crit;\npid ' + str(root / 'nginx.pid')
                + ';\nevents { worker_connections 32; }\nhttp {\n'
                + '\n'.join(name + ' ' + str(root / name) + ';' for name in (
                    'client_body_temp_path', 'proxy_temp_path', 'fastcgi_temp_path', 'uwsgi_temp_path', 'scgi_temp_path'))
                + '\n' + rendered + '\n}\n')
            syntax = subprocess.run([*nginx, '-e', 'stderr', '-p', str(root) + '/', '-c', str(config), '-t'],
                                    capture_output=True, text=True, timeout=10)
            assert syntax.returncode == 0, syntax.stderr
            process = await asyncio.create_subprocess_exec(*nginx, '-e', 'stderr', '-p', str(root) + '/',
                '-c', str(config), '-g', 'daemon off;', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=ca.decode())
            async with asyncio.timeout(5):
                while True:
                    if process.returncode is not None:
                        pytest.fail('Synthetic nginx exited before readiness')
                    try:
                        reader, writer = await asyncio.open_connection('127.0.0.1', port,
                            ssl=context, server_hostname='localhost')
                        writer.close(); await writer.wait_closed()
                        break
                    except ConnectionRefusedError:
                        await asyncio.sleep(0.001)
            yield dict(port=port, context=context, adapter=adapter, state=state, ca=ca)
        finally:
            if process is not None and process.returncode is None:
                process.send_signal(signal.SIGQUIT)
                await asyncio.wait_for(process.communicate(), 8)
            server.should_exit = True
            await asyncio.wait_for(backend, 8)
            listener.close()


async def raw_request(rig, method, path, *, auth=(TOKEN,), body=b'', extra_headers=(), source='127.0.0.1'):
    reader, writer = await asyncio.open_connection('127.0.0.1', rig['port'], ssl=rig['context'],
        server_hostname='localhost', local_addr=(source, 0))
    try:
        headers = ['Host: localhost', 'Connection: close', 'Content-Type: application/json',
                   'Content-Length: ' + str(len(body))]
        headers.extend('Authorization: Bearer ' + value for value in auth)
        headers.extend(extra_headers)
        wire = method + ' ' + path + ' HTTP/1.1\r\n' + '\r\n'.join(headers) + '\r\n\r\n'
        writer.write(wire.encode('ascii') + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(65536), 5)
        while not reader.at_eof():
            part = await asyncio.wait_for(reader.read(65536), 5)
            if not part: break
            response += part
            assert len(response) <= 128 * 1024
        return int(response.split(b' ', 2)[1]), response
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_actual_nginx_syntax_tls_uds_exact_routes_auth_and_source():
    async with proxy_fixture() as rig:
        # Trust failures must stop at this actual proxy, before any UDS request.
        wrong_ca, _ = certificate()
        wrong_trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        wrong_trust.load_verify_locations(cadata=wrong_ca.decode())
        for context, hostname in [(wrong_trust, 'localhost'), (rig['context'], 'wrong.invalid')]:
            with pytest.raises(ssl.SSLCertVerificationError):
                await asyncio.open_connection('127.0.0.1', rig['port'], ssl=context,
                    server_hostname=hostname)
            assert not rig['state']['seen'] and not rig['adapter'].calls
        request = command()
        payload = request.model_dump_json().encode()
        status, response = await raw_request(rig, 'POST', ROOT, body=payload)
        assert status == 200 and request.command_id.encode() in response
        status, _ = await raw_request(rig, 'GET', ROOT + '/' + request.command_id)
        assert status == 200
        status, _ = await raw_request(rig, 'POST', ROOT + '/' + request.command_id + '/reconcile')
        assert status == 200
        assert rig['adapter'].calls == [('submit', request.command_id), ('get', request.command_id), ('reconcile', request.command_id)]
        for _, _, headers in rig['state']['seen']:
            assert [value for name, value in headers if name == b'authorization'] == [('Bearer ' + TOKEN).encode()]
        before = len(rig['adapter'].calls)
        for auth in [(), ('wrong',), (TOKEN, TOKEN), (TOKEN, 'wrong')]:
            status, _ = await raw_request(rig, 'POST', ROOT, body=payload, auth=auth)
            assert status in {400, 401}
        assert len(rig['adapter'].calls) == before
        upstream_before = len(rig['state']['seen'])
        status, _ = await raw_request(rig, 'POST', ROOT, body=payload, source='127.0.0.2',
            extra_headers=['X-Forwarded-For: 127.0.0.1', 'CF-Connecting-IP: 127.0.0.1'])
        assert status == 403 and len(rig['state']['seen']) == upstream_before


@pytest.mark.asyncio
async def test_actual_nginx_variants_body_framing_and_no_upstream_retry():
    async with proxy_fixture() as rig:
        request = command()
        payload = request.model_dump_json().encode()
        path = ROOT + '/' + request.command_id
        for method, target in [('GET', ROOT), ('HEAD', path), ('OPTIONS', ROOT), ('PUT', ROOT),
            ('post', ROOT), ('POST', ROOT + '/'), ('POST', ROOT + '?x=1'),
            ('GET', path + '?'), ('POST', '/v1/%63ontainer-pilot/commands'),
            ('POST', '/v1//container-pilot/commands'), ('POST', '/v1/other/../container-pilot/commands'),
            ('GET', ROOT + '/' + request.command_id.upper()), ('GET', '/health'), ('POST', '/mcp')]:
            before = len(rig['state']['seen'])
            status, _ = await raw_request(rig, method, target)
            assert status in {400, 404, 405}, (method, target, status)
            assert len(rig['state']['seen']) == before
        status, _ = await raw_request(rig, 'POST', ROOT, body=b'x' * 8193)
        assert status == 413
        status, _ = await raw_request(rig, 'POST', ROOT, body=payload,
            extra_headers=['Transfer-Encoding: chunked'])
        assert status == 400
        assert not rig['adapter'].calls
        for method, target in [('GET', path), ('POST', path + '/reconcile')]:
            status, _ = await raw_request(rig, method, target, body=b'{}')
            assert status == 400
        assert not rig['adapter'].calls
        rig['state']['fail'] = True
        before = len(rig['state']['seen'])
        status, _ = await raw_request(rig, 'POST', ROOT, body=payload)
        assert status == 503
        await asyncio.sleep(0.02)  # Bounded observation of unwanted retry only.
        assert len(rig['state']['seen']) == before + 1
