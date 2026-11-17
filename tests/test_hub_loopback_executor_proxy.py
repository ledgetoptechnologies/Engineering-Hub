"""Renderer contracts and actual local NGINX TLS->UDS wire tests.

The wire fixture substitutes temp path aliases and a same-UID synthetic executor,
not an installed executor, root sandbox, foreign UID mapping, issuer or Engine.
"""
import asyncio
import hashlib
import json
import signal
import sys
from types import SimpleNamespace

import pytest

from scripts.render_loopback_executor_proxy_bundle import render

GENERATION = 'b3e3fcee-0702-4af6-9de2-c9f5f6ab9b9b'
CAMPAIGN = '/var/lib/eh-pilot-volume-test-v1/campaign-real-accept-proxy-review'


def inputs():
    return dict(campaign_root=CAMPAIGN, generation_id=GENERATION,
        executor_origin='https://127.0.0.1:9443',
        api_parent=CAMPAIGN + '/hub-pilot-executor-' + GENERATION + '/executor-state/api',
        certificate=CAMPAIGN + '/tls/server.pem', private_key=CAMPAIGN + '/tls/server.key',
        worker_uid=210000)


def test_render_is_inert_bounded_and_uses_same_closed_http_template():
    result = render(**inputs())
    assert result['state'] == 'rendered_not_activated'
    http = result['http']
    assert 'listen 127.0.0.1:9443 ssl;' in http
    assert 'allow 127.0.0.1;' in http and 'deny all;' in http
    assert http.count('server unix:') == 1
    assert 'proxy_next_upstream off;' in http and 'client_max_body_size 8k;' in http
    assert 'proxy_ignore_client_abort on;' in http
    assert 'current-observation' in http and '/reconcile' in http
    assert 'Authorization: Bearer' not in http
    unit = result['files']['proxy.service']
    for line in ('Slice=user-10001.slice', 'MemoryMax=64M', 'MemorySwapMax=0',
                 'CPUQuota=10%', 'TasksMax=16', 'KillSignal=SIGQUIT', 'SendSIGKILL=no'):
        assert line in unit
    assert '[Install]' not in unit and 'Restart=' not in unit
    assert result['api_parent'] + ':/run/' in unit
    assert 'engine.sock' not in unit and 'signing' not in unit
    assert 'user ehproxy ehproxy;' in result['files']['nginx.conf']
    assert ':210000:210000:' in result['files']['passwd']


@pytest.mark.parametrize('key,value', [
    ('executor_origin', 'http://127.0.0.1:9443'),
    ('executor_origin', 'https://localhost:9443'),
    ('executor_origin', 'https://192.168.50.90:9443'),
    ('executor_origin', 'https://127.0.0.1:8770'),
    ('executor_origin', 'https://127.0.0.1:9443/'),
    ('executor_origin', 'https://127.0.0.1:9443?upstream=other'),
    ('api_parent', '/run/docker.sock'), ('api_parent', CAMPAIGN + '/other/api'),
    ('api_parent', CAMPAIGN + '/a/../api'), ('campaign_root', '/tmp/campaign'),
    ('certificate', '/etc/ssl/server.pem'), ('private_key', CAMPAIGN + '/tls/key;evil'),
    ('worker_uid', 10001), ('worker_uid', True), ('generation_id', 'old-generation')])
def test_wrong_origin_path_or_identity_refused(key, value):
    values = inputs(); values[key] = value
    with pytest.raises((ValueError, TypeError)):
        render(**values)


@pytest.fixture
def rendered_wire(monkeypatch):
    if sys.platform != 'linux':
        pytest.skip('Actual NGINX + UDS fixture requires Linux')
    from tests import test_hub_executor_nginx_wire as wire
    # The reused wire fixture originally had DNS-only localhost certificates.
    # Literal-IP production transport needs a real IP SAN, never hostname bypass.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone
    import ipaddress
    def certificate():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
        return cert.public_bytes(serialization.Encoding.PEM), key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    monkeypatch.setattr(wire, 'certificate', certificate)
    result = render(**inputs())
    run = '/run/eh-pilot-loopback-' + GENERATION.replace('-', '')
    credentials = '/run/credentials/' + result['unit_name']
    # Only test aliases differ from the generated runtime sandbox aliases.
    http = result['http'].replace(run + '/uds/executor.sock', '__EXECUTOR_UDS_PATH__')
    http = http.replace(credentials + '/server.pem', '__CERTIFICATE_CHAIN_PATH__')
    http = http.replace(credentials + '/server.key', '__CERTIFICATE_KEY_PATH__')
    monkeypatch.setattr(wire, 'TEMPLATE', SimpleNamespace(read_text=lambda: http))
    return wire


@pytest.mark.asyncio
async def test_actual_rendered_nginx_routes_auth_tls_peer_and_no_retry(rendered_wire):
    await rendered_wire.test_actual_nginx_syntax_tls_uds_exact_routes_auth_and_source()
    await rendered_wire.test_actual_nginx_variants_body_framing_and_no_upstream_retry()


@pytest.mark.asyncio
async def test_actual_rendered_current_observation_preserves_nonce_signed_bytes(rendered_wire):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    class Issuer:
        def __init__(self): self.calls = []; self.raw = None
        def observe_current_launch(self, *, nonce, timeout_ms):
            self.calls.append((nonce, timeout_ms))
            payload = {'nonce': nonce, 'fixture': 'route-only-not-release-authority'}
            raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
            self.raw = json.dumps({'payload': payload, 'signature': key.sign(raw).hex()},
                                  sort_keys=True, separators=(',', ':')).encode()
            return self.raw
    issuer = Issuer()
    async with rendered_wire.proxy_fixture(issuer) as rig:
        path = '/v1/container-pilot/current-observation'
        nonce = 'c' * 64
        status, response = await rendered_wire.raw_request(rig, 'POST', path,
            body=json.dumps({'nonce': nonce}).encode())
        assert status == 200 and issuer.raw in response
        assert len(issuer.calls) == 1 and issuer.calls[0][0] == nonce
        payload = json.loads(issuer.raw)
        key.public_key().verify(bytes.fromhex(payload['signature']),
            json.dumps(payload['payload'], sort_keys=True, separators=(',', ':')).encode())
        # Exercise the production deadline/framing/EOF HTTPS transport through
        # the actual rendered NGINX listener, not merely the raw test client.
        from engineering_hub.current_executor_transport import CurrentExecutorHTTPSTransport
        credential = rendered_wire.TOKEN.encode()
        transport = CurrentExecutorHTTPSTransport.from_captured_material(
            'https://127.0.0.1:' + str(rig['port']), ca_pem=rig['ca'],
            ca_sha256='sha256:' + hashlib.sha256(rig['ca']).hexdigest(),
            credential=credential, credential_sha256='sha256:' + hashlib.sha256(credential).hexdigest())
        actual = await asyncio.to_thread(transport.observe_current_launch,
                                        nonce='d' * 64, timeout_ms=2000)
        assert actual == issuer.raw and issuer.calls[-1][0] == 'd' * 64
        for method, route, auth in [('POST', path, ()), ('POST', path, ('wrong',)),
                ('GET', path, (rendered_wire.TOKEN,)),
                ('POST', path + '?x=1', (rendered_wire.TOKEN,))]:
            status, _ = await rendered_wire.raw_request(rig, method, route,
                auth=auth, body=json.dumps({'nonce': nonce}).encode())
            assert status in (400, 401, 404)
        assert len(issuer.calls) == 2


@pytest.mark.asyncio
async def test_actual_nginx_quit_drains_accepted_request_despite_owner_cancellation(rendered_wire, monkeypatch):
    import threading
    from engineering_hub.lifecycle import finish_owned
    entered, release = threading.Event(), threading.Event()
    original_submit = rendered_wire.FixtureAdapter.submit
    def blocked_submit(adapter, request):
        entered.set()
        assert release.wait(6)
        return original_submit(adapter, request)
    monkeypatch.setattr(rendered_wire.FixtureAdapter, 'submit', blocked_submit)
    fixture = rendered_wire.proxy_fixture()
    rig = await fixture.__aenter__()
    request = rendered_wire.command()
    client = asyncio.create_task(rendered_wire.raw_request(rig, 'POST', rendered_wire.ROOT,
        body=request.model_dump_json().encode()))
    close = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        # The fixture owns only its newly created NGINX process and sends QUIT,
        # matching the rendered unit. Retain the entire existing fixture drain
        # across repeated Python owner cancellation; no process adoption/KILL.
        close = asyncio.create_task(finish_owned(fixture.__aexit__(None, None, None)))
        await asyncio.sleep(.1)
        close.cancel(); await asyncio.sleep(.02); close.cancel(); await asyncio.sleep(.02)
        assert not close.done() and not client.done()
    finally:
        release.set()
        status, _ = await client
        assert status == 200
        if close is None:
            await fixture.__aexit__(None, None, None)
        else:
            with pytest.raises(asyncio.CancelledError):
                await close
