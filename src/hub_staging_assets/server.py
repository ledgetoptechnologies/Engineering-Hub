"""Bounded immutable asset reads. The operator owns all registration and paths."""
from dataclasses import dataclass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import threading
from urllib.parse import urlsplit
import uuid

MAX_READ = 8 * 1024**2
MAX_ASSET = 2 * 1024**3
HASH_CHUNK = 65536
MAX_INDEX_CHUNKS = 131072  # At most4MiB of raw digests across the catalog.
MEDIA_TYPES = frozenset({'application/json', 'image/jpeg', 'image/png',
                         'model/gltf-binary', 'application/octet-stream'})


class AssetError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def require(condition, code, status=400):
    if not condition:
        raise AssetError(code, status)


def _identity(info):
    # Python's Windows lstat/fstat expose different ctime definitions. Linux
    # deployment retains its inode-change-time check; Windows is test-only.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns if os.name == 'posix' else None,
            info.st_mode, info.st_uid, info.st_gid, info.st_nlink)


def _path(path):
    path = Path(path)
    require(path.is_absolute(), 'absolute_path_required')
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), 'symlink_rejected')
    return path


def protected_file(path, *, private=False, maximum=65536):
    path = _path(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= maximum,
            'invalid_configuration_file')
    if os.name == 'posix':
        require(before.st_uid == os.getuid() and not stat.S_IMODE(before.st_mode) & (0o077 if private else 0o022),
                'configuration_file_protection')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        require(_identity(os.fstat(fd)) == _identity(before), 'configuration_changed')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(maximum + 1)
        require(len(data) <= maximum and _identity(os.fstat(fd)) == _identity(before) and
                _identity(path.lstat()) == _identity(before), 'configuration_changed')
        return data
    finally:
        os.close(fd)


def load_token(path):
    try:
        token = protected_file(path, private=True, maximum=4096).decode('ascii').strip()
    except (OSError, ValueError, UnicodeError):
        raise AssetError('credential_unavailable', 503) from None
    require(16 <= len(token) <= 4096 and all(33 <= ord(char) <= 126 for char in token), 'credential_invalid')
    return token


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_manifest_key')
        result[key] = value
    return result


@dataclass(frozen=True)
class Asset:
    identity: str
    path: Path
    size: int
    sha256: str
    media_type: str
    stat_identity: tuple
    chunk_hashes: bytes = b''


class AssetCatalog:
    def __init__(self, data_root, manifest_file, *, max_asset_bytes=MAX_ASSET):
        require(type(max_asset_bytes) is int and 0 <= max_asset_bytes <= MAX_ASSET, 'invalid_asset_limit')
        self.root = _path(data_root)
        require(self.root.is_dir(), 'invalid_data_root')
        root_info = self.root.lstat()
        if os.name == 'posix':
            require(root_info.st_uid == os.getuid() and not stat.S_IMODE(root_info.st_mode) & 0o022,
                    'data_root_protection')
        self.root_identity = (root_info.st_dev, root_info.st_ino)
        try:
            values = json.loads(protected_file(manifest_file), object_pairs_hook=_object)
        except (OSError, ValueError, UnicodeError):
            raise AssetError('manifest_unavailable') from None
        require(type(values) is dict and len(values) <= 128, 'manifest_schema')
        self.assets = {}
        indexed = 0
        for identity, value in values.items():
            try:
                require(type(identity) is str and str(uuid.UUID(identity)) == identity, 'invalid_asset_id')
            except (ValueError, TypeError):
                raise AssetError('invalid_asset_id') from None
            require(type(value) is dict and set(value) == {'relative_path', 'size', 'sha256', 'media_type'}, 'manifest_schema')
            relative = value['relative_path']
            require(type(relative) is str and 0 < len(relative) <= 512 and '\\' not in relative and ':' not in relative and
                    '\x00' not in relative and not relative.startswith('/') and
                    all(part not in ('', '.', '..') for part in relative.split('/')), 'unsafe_asset_path')
            require(type(value['size']) is int and 0 <= value['size'] <= max_asset_bytes, 'asset_size_limit')
            indexed += (value['size'] + HASH_CHUNK - 1) // HASH_CHUNK
            require(indexed <= MAX_INDEX_CHUNKS, 'catalog_index_limit')
            require(type(value['sha256']) is str and re.fullmatch('[a-f0-9]{64}', value['sha256']), 'invalid_asset_digest')
            require(type(value['media_type']) is str and value['media_type'] in MEDIA_TYPES, 'asset_media_type')
            path = _path(self.root.joinpath(*relative.split('/')))
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == value['size'], 'asset_identity_invalid')
            if os.name == 'posix':
                require(info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o022, 'asset_protection')
            asset = Asset(identity, path, value['size'], value['sha256'], value['media_type'], _identity(info))
            digest = hashlib.sha256()
            chunks = bytearray()
            fd = self.open(asset)
            try:
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    while chunk := stream.read(HASH_CHUNK):
                        digest.update(chunk)
                        chunks.extend(hashlib.sha256(chunk).digest())
                self.verify(asset, fd)
                require(hmac.compare_digest(digest.hexdigest(), asset.sha256), 'asset_digest_mismatch')
            finally:
                os.close(fd)
            require(len(chunks) == ((asset.size + HASH_CHUNK - 1) // HASH_CHUNK) * 32, 'asset_changed', 409)
            self.assets[identity] = Asset(asset.identity, asset.path, asset.size, asset.sha256,
                                          asset.media_type, asset.stat_identity, bytes(chunks))

    def verify(self, asset, descriptor=None):
        require((self.root.lstat().st_dev, self.root.lstat().st_ino) == self.root_identity, 'data_root_changed', 409)
        _path(asset.path)
        require(_identity(asset.path.lstat()) == asset.stat_identity, 'asset_changed', 409)
        if descriptor is not None:
            require(_identity(os.fstat(descriptor)) == asset.stat_identity, 'asset_changed', 409)

    def open(self, asset):
        self.verify(asset)
        descriptor = os.open(asset.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            self.verify(asset, descriptor)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def read(self, asset, start, length):
        require(0 <= length <= MAX_READ and 0 <= start <= asset.size and start + length <= asset.size,
                'invalid_range', 416)
        descriptor = self.open(asset)
        try:
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                data = bytearray()
                if length:
                    first, last = start // HASH_CHUNK, (start + length - 1) // HASH_CHUNK
                    stream.seek(first * HASH_CHUNK)
                    for index in range(first, last + 1):
                        offset = index * HASH_CHUNK
                        expected_length = min(HASH_CHUNK, asset.size - offset)
                        chunk = stream.read(expected_length)
                        expected_hash = asset.chunk_hashes[index * 32:(index + 1) * 32]
                        require(len(chunk) == expected_length and len(expected_hash) == 32 and
                                hmac.compare_digest(hashlib.sha256(chunk).digest(), expected_hash), 'asset_changed', 409)
                        left, right = max(0, start - offset), min(len(chunk), start + length - offset)
                        data.extend(chunk[left:right])
            self.verify(asset, descriptor)
            require(len(data) == length, 'asset_changed', 409)
            return bytes(data)
        finally:
            os.close(descriptor)


def byte_range(header, size):
    if header is None:
        require(size <= MAX_READ, 'range_required', 416)
        return 0, size, False
    require(type(header) is str and len(header) <= 80, 'invalid_range', 416)
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', header)
    require(match is not None and size > 0, 'invalid_range', 416)
    first, last = match.groups()
    require(bool(first or last), 'invalid_range', 416)
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        require(start < size and start <= end, 'invalid_range', 416)
    else:
        suffix = int(last)
        require(0 < suffix <= MAX_READ, 'invalid_range', 416)
        start, end = max(0, size - suffix), size - 1
    length = end - start + 1
    require(length <= MAX_READ, 'range_too_large', 416)
    return start, length, True


def tls_context(cert_file, key_file):
    require(bool(cert_file) == bool(key_file), 'tls_pair_required')
    if not cert_file:
        return None
    protected_file(cert_file, maximum=65536)
    protected_file(key_file, private=True, maximum=65536)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert_file, key_file)
    except (OSError, ssl.SSLError):
        raise AssetError('tls_configuration_invalid') from None
    return context


class AssetServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, catalog, token, *, allowed_peers, context=None):
        self.catalog, self.token, self.ssl_context = catalog, token, context
        self.peers = frozenset(ipaddress.ip_address(peer) for peer in allowed_peers)
        require(bool(self.peers), 'allowed_peers_required')
        self.slots = threading.BoundedSemaphore(4)
        if ipaddress.ip_address(address[0]).version == 6:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def get_request(self):
        request, peer = super().get_request()
        if ipaddress.ip_address(peer[0]) not in self.peers:
            request.close()  # Before TLS handshake or HTTP parsing.
            raise OSError('peer_rejected')  # Return control to serve_forever/shutdown.
        request.settimeout(5)
        return request, peer

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # No header/path/credential or raw exception logging.


class Handler(BaseHTTPRequestHandler):
    server_version = 'HubAssets/1'

    def setup(self):
        if self.server.ssl_context is not None:
            self.request = self.server.ssl_context.wrap_socket(self.request, server_side=True)
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def reply(self, status, body, *, media='application/json', headers=None, length=None):
        self.send_response(status)
        self.send_header('Content-Type', media)
        self.send_header('Content-Length', str(len(body) if length is None else length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def dispatch(self):
        asset = None
        try:
            auth = self.headers.get_all('Authorization', [])
            require(len(auth) == 1 and auth[0].isascii() and hmac.compare_digest(auth[0], 'Bearer ' + self.server.token), 'unauthorized', 401)
            path = urlsplit(self.path)
            require(not path.query and not path.fragment, 'not_found', 404)
            if path.path == '/health':
                self.reply(200, json.dumps({'ok': True, 'assets': len(self.server.catalog.assets)}).encode())
                return
            match = re.fullmatch('/v1/assets/([a-f0-9-]{36})', path.path)
            require(match is not None and match[1] in self.server.catalog.assets, 'not_found', 404)
            asset = self.server.catalog.assets[match[1]]
            ranges = self.headers.get_all('Range', [])
            require(len(ranges) <= 1, 'invalid_range', 416)
            if self.command == 'HEAD' and not ranges:
                descriptor = self.server.catalog.open(asset)
                try:
                    self.server.catalog.verify(asset, descriptor)
                finally:
                    os.close(descriptor)
                start, length, partial, data = 0, asset.size, False, b''
            else:
                start, length, partial = byte_range(ranges[0] if ranges else None, asset.size)
                if self.command == 'HEAD':
                    descriptor = self.server.catalog.open(asset)
                    try:
                        self.server.catalog.verify(asset, descriptor)
                    finally:
                        os.close(descriptor)
                    data = b''
                else:
                    data = self.server.catalog.read(asset, start, length)
            headers = {'Accept-Ranges': 'bytes', 'ETag': '"sha256:' + asset.sha256 + '"'}
            if partial:
                headers['Content-Range'] = f'bytes {start}-{start + length - 1}/{asset.size}'
            self.reply(206 if partial else 200, data, media=asset.media_type, headers=headers, length=length)
        except AssetError as exc:
            headers = {'Content-Range': 'bytes */' + str(asset.size)} if exc.status == 416 and asset else None
            self.reply(exc.status, json.dumps({'error': exc.code}).encode(), headers=headers)
        except Exception:
            self.reply(503, b'{"error":"asset_unavailable"}')

    do_GET = dispatch
    do_HEAD = dispatch
