"""Synthetic local assets/TLS only; no staging host, installs or live secrets."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import ssl
import socket
import threading
import time
import uuid

import pytest

from hub_staging_assets.server import (AssetCatalog, AssetError, AssetServer, HASH_CHUNK,
                                       MAX_READ, byte_range, load_token, tls_context)

TOKEN = 'synthetic-private-asset-token-not-live'


def asset_files(tmp_path, content=b'0123456789', media='application/octet-stream'):
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    asset = root / 'asset.bin'
    asset.write_bytes(content); asset.chmod(0o600)
    identity = str(uuid.uuid4())
    values = {identity: {'relative_path': 'asset.bin', 'size': len(content),
                         'sha256': hashlib.sha256(content).hexdigest(), 'media_type': media}}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps(values)); manifest.chmod(0o600)
    return root, asset, manifest, identity, values


@contextmanager
def running(catalog, *, context=None, peers=('127.0.0.1',)):
    server = AssetServer(('127.0.0.1', 0), catalog, TOKEN, allowed_peers=peers, context=context)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=20)
        assert not thread.is_alive()


def request(server, path, method='GET', headers=None, context=None):
    cls = http.client.HTTPSConnection if context else http.client.HTTPConnection
    options = {'timeout': 5}
    if context:
        options['context'] = context
    connection = cls(*server.server_address, **options)
    connection.request(method, path, headers={'Authorization': 'Bearer ' + TOKEN, **(headers or {})})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_full_head_ranges_suffix_and_health(tmp_path):
    root, asset, manifest, identity, _ = asset_files(tmp_path)
    with running(AssetCatalog(root, manifest)) as server:
        path = '/v1/assets/' + identity
        code, headers, data = request(server, path)
        assert code == 200 and data == b'0123456789'
        assert headers['Content-Length'] == '10' and headers['Accept-Ranges'] == 'bytes'
        assert headers['ETag'] == '"sha256:' + hashlib.sha256(data).hexdigest() + '"'
        assert headers['X-Content-Type-Options'] == 'nosniff'
        code, headers, data = request(server, path, 'HEAD')
        assert code == 200 and headers['Content-Length'] == '10' and data == b''
        code, headers, data = request(server, path, headers={'Range': 'bytes=2-5'})
        assert code == 206 and data == b'2345' and headers['Content-Range'] == 'bytes 2-5/10'
        assert request(server, path, headers={'Range': 'bytes=-3'})[2] == b'789'
        assert request(server, path, headers={'Range': 'bytes=7-'})[2] == b'789'
        code, headers, data = request(server, path, 'HEAD', {'Range': 'bytes=2-5'})
        assert code == 206 and headers['Content-Length'] == '4' and data == b''
        assert json.loads(request(server, '/health')[2]) == {'ok': True, 'assets': 1}


@pytest.mark.parametrize('header', ['bytes=0-1,3-4', 'bytes=-', 'bytes=-0', 'bytes=99-', 'bytes=7-2',
                                   'items=0-1', 'bytes= 0-1', 'bytes=-999999999999999999999999999999'])
def test_invalid_ranges_are_strict_and_do_not_read(header):
    with pytest.raises(AssetError) as error:
        byte_range(header, 10)
    assert error.value.status == 416


def test_large_asset_requires_bounded_range_but_head_advertises_total(tmp_path):
    data = b'x' * (MAX_READ + 1)
    root, asset, manifest, identity, _ = asset_files(tmp_path, data)
    catalog = AssetCatalog(root, manifest)
    with running(catalog) as server:
        path = '/v1/assets/' + identity
        code, headers, body = request(server, path)
        assert code == 416 and headers['Content-Range'] == 'bytes */' + str(len(data))
        assert len(body) < 128
        code, headers, body = request(server, path, 'HEAD')
        assert code == 200 and headers['Content-Length'] == str(len(data)) and body == b''
        assert request(server, path, headers={'Range': 'bytes=0-3'})[2] == b'xxxx'


def test_bearer_is_mandatory_and_cookies_forwarded_identity_do_nothing(tmp_path):
    root, _, manifest, identity, _ = asset_files(tmp_path)
    with running(AssetCatalog(root, manifest)) as server:
        for path in ('/health', '/v1/assets/' + identity):
            code, _, body = request(server, path, headers={'Authorization': '', 'Cookie': 'CF_Authorization=human',
                                                         'X-Forwarded-For': '192.168.50.80'})
            assert code == 401 and TOKEN.encode() not in body


def test_exact_peer_gate_rejects_before_http_read(tmp_path):
    root, _, manifest, _, _ = asset_files(tmp_path)
    with running(AssetCatalog(root, manifest), peers=('192.168.50.80',)) as server:
        with pytest.raises((OSError, http.client.RemoteDisconnected)):
            request(server, '/health', headers={'X-Forwarded-For': '192.168.50.80'})


def test_changed_asset_is_never_served_under_old_digest(tmp_path):
    root, asset, manifest, identity, _ = asset_files(tmp_path)
    catalog = AssetCatalog(root, manifest)
    asset.write_bytes(b'abcdefghij')
    with running(catalog) as server:
        code, _, body = request(server, '/v1/assets/' + identity)
        assert code == 409 and b'abcdefghij' not in body


def test_inflight_change_is_detected_before_returning_bytes(tmp_path, monkeypatch):
    root, asset, manifest, identity, _ = asset_files(tmp_path)
    catalog = AssetCatalog(root, manifest)
    original = catalog.open
    # Model coarse filesystem clocks: metadata observations reveal no drift.
    # Change the actual bytes after opening and before the real descriptor read.
    monkeypatch.setattr(catalog, 'verify', lambda *args, **kwargs: None)
    def change_before_read(value):
        descriptor = original(value)
        asset.write_bytes(b'abcdefghij')
        return descriptor
    monkeypatch.setattr(catalog, 'open', change_before_read)
    with pytest.raises(AssetError, match='asset_changed'):
        catalog.read(catalog.assets[identity], 0, 10)


def test_changed_bytes_denied_with_unchanged_metadata_observation(tmp_path, monkeypatch):
    root, asset, manifest, identity, _ = asset_files(tmp_path)
    catalog = AssetCatalog(root, manifest)
    asset.write_bytes(b'abcdefghij')
    monkeypatch.setattr(catalog, 'verify', lambda *args, **kwargs: None)
    with pytest.raises(AssetError, match='asset_changed'):
        catalog.read(catalog.assets[identity], 0, 4)


def test_unaligned_range_verifies_covering_chunks_and_only_reads_needed_chunks(tmp_path, monkeypatch):
    content = bytes(range(256)) * (HASH_CHUNK // 256 * 3)
    root, asset, manifest, identity, _ = asset_files(tmp_path, content)
    catalog = AssetCatalog(root, manifest)
    registered = catalog.assets[identity]
    assert len(registered.chunk_hashes) == 3 * 32
    assert catalog.read(registered, HASH_CHUNK - 3, 10) == content[HASH_CHUNK - 3:HASH_CHUNK + 7]
    monkeypatch.setattr(catalog, 'verify', lambda *args, **kwargs: None)
    with asset.open('r+b') as output:
        output.seek(HASH_CHUNK * 2)
        output.write(b'changed-outside-request')
    # An unrelated chunk's change cannot make the authorized original first
    # chunk wrong; no whole-file rehash is needed for a small range.
    assert catalog.read(registered, 0, 4) == content[:4]
    with pytest.raises(AssetError, match='asset_changed'):
        catalog.read(registered, HASH_CHUNK * 2, 4)


def test_chunk_index_memory_budget_is_bounded(tmp_path, monkeypatch):
    root, _, manifest, _, _ = asset_files(tmp_path)
    monkeypatch.setattr('hub_staging_assets.server.MAX_INDEX_CHUNKS', 0)
    with pytest.raises(AssetError, match='catalog_index_limit'):
        AssetCatalog(root, manifest)


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/windows', 'folder\\file', 'a/../file'])
def test_manifest_rejects_path_escape(tmp_path, name):
    root, _, manifest, identity, values = asset_files(tmp_path)
    values[identity]['relative_path'] = name
    manifest.write_text(json.dumps(values))
    with pytest.raises(AssetError, match='unsafe_asset_path'):
        AssetCatalog(root, manifest)


@pytest.mark.parametrize('field,value', [('sha256', '0' * 64), ('size', 11), ('media_type', 'text/html'),
                                        ('media_type', 'text/javascript')])
def test_manifest_hash_size_and_active_media_denied(tmp_path, field, value):
    root, _, manifest, identity, values = asset_files(tmp_path)
    values[identity][field] = value
    manifest.write_text(json.dumps(values))
    with pytest.raises(AssetError):
        AssetCatalog(root, manifest)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX symlink/permissions')
def test_links_and_world_readable_credentials_rejected(tmp_path):
    root, asset, manifest, _, _ = asset_files(tmp_path)
    foreign = tmp_path / 'outside'
    foreign.write_bytes(asset.read_bytes())
    asset.unlink(); asset.symlink_to(foreign)
    with pytest.raises(AssetError, match='symlink_rejected'):
        AssetCatalog(root, manifest)
    token = tmp_path / 'token'
    token.write_text(TOKEN); token.chmod(0o644)
    with pytest.raises(AssetError, match='configuration_file_protection'):
        load_token(token)


def test_token_load_is_reference_only_and_invalid_values_are_opaque(tmp_path):
    token = tmp_path / 'token'
    token.write_text(TOKEN); token.chmod(0o600)
    assert load_token(token) == TOKEN
    token.write_text('too short')
    with pytest.raises(AssetError) as failure:
        load_token(token)
    assert TOKEN not in str(failure.value) and str(token) not in str(failure.value)


def tls_files(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-assets')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(private, hashes.SHA256()))
    cert_file, key_file = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM)); cert_file.chmod(0o600)
    key_file.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption())); key_file.chmod(0o600)
    return cert_file, key_file


def test_real_private_tls_san_validation_and_bearer_read(tmp_path):
    cert_file, key_file = tls_files(tmp_path)
    root, _, manifest, identity, _ = asset_files(tmp_path)
    context = tls_context(cert_file, key_file)
    trust = ssl.create_default_context(cafile=str(cert_file))
    with running(AssetCatalog(root, manifest), context=context) as server:
        assert request(server, '/v1/assets/' + identity, context=trust)[2] == b'0123456789'
        assert request(server, '/health', context=trust, headers={'Authorization': ''})[0] == 401
    with pytest.raises(AssetError, match='tls_pair_required'):
        tls_context(cert_file, None)


def test_peer_gate_is_before_tls_wrapping(tmp_path, monkeypatch):
    root, _, manifest, _, _ = asset_files(tmp_path)
    class ForbiddenContext:
        def wrap_socket(self, *args, **kwargs):
            raise AssertionError('unapproved peer reached TLS')
    with running(AssetCatalog(root, manifest), context=ForbiddenContext(), peers=('192.168.50.80',)) as server:
        with pytest.raises((OSError, http.client.RemoteDisconnected)):
            request(server, '/health')


def test_duplicate_range_header_is_refused(tmp_path):
    root, _, manifest, identity, _ = asset_files(tmp_path)
    with running(AssetCatalog(root, manifest)) as server:
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        connection.putrequest('GET', '/v1/assets/' + identity)
        connection.putheader('Authorization', 'Bearer ' + TOKEN)
        connection.putheader('Range', 'bytes=0-1')
        connection.putheader('Range', 'bytes=4-5')
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 416
        assert response.getheader('Content-Range') == 'bytes */10'
        response.read(); connection.close()


def test_four_request_threads_bound_slow_clients(tmp_path):
    root, _, manifest, _, _ = asset_files(tmp_path)
    connections = []
    with running(AssetCatalog(root, manifest)) as server:
        try:
            for _ in range(4):
                connections.append(socket.create_connection(server.server_address, timeout=3))
            deadline = time.monotonic() + 3
            while len(server._threads) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(server._threads) == 4
            fifth = socket.create_connection(server.server_address, timeout=3)
            try:
                assert fifth.recv(1) == b''
            finally:
                fifth.close()
        finally:
            for connection in connections:
                connection.close()


@pytest.mark.parametrize('extra', [[], ['--cert-file', 'missing-cert'],
                                 ['--cert-file', 'missing-cert', '--key-file', 'missing-key']])
def test_lan_cli_requires_explicit_tls_pair_and_peers_before_loading_credentials(monkeypatch, extra):
    from hub_staging_assets.__main__ import main
    monkeypatch.setattr('sys.argv', ['assets', '--data-root', '/private/data', '--manifest', '/private/manifest',
                                    '--token-file', '/private/token', '--host', '192.168.50.90', *extra])
    with pytest.raises(AssetError, match='tls_pair_required|lan_requires_tls_and_peers'):
        main()
