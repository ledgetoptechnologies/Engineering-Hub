"""Snapshot-only staging observer. Run behind an operator-provisioned TLS proxy."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler
import ipaddress
import json
from pathlib import Path

from .__main__ import BoundedServer, credentials
from .runtime_inventory import _empty, _read_host, _source, _timestamp, _utc, _validate_host


def snapshot(path):
    now = _utc()
    result = _empty(now)
    result['sources'].append(_source('worker', 'not_configured'))
    try:
        source, containers, services = _validate_host(_read_host(path), now)
        result['sources'].append(source)
        result['containers'], result['services'] = containers, services
    except Exception:
        result['sources'].append(_source('host', 'unavailable'))
    return result


def handler(path, token, peers):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'HubObserver/1'
        def setup(self):
            super().setup()
            self.connection.settimeout(5)
        def log_message(self, *args):
            pass
        def reply(self, status, value):
            data = json.dumps(value, separators=(',', ':')).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if ipaddress.ip_address(self.client_address[0]) not in peers:
                return self.reply(403, {'error': 'peer_not_allowed'})
            auth = self.headers.get_all('Authorization', [])
            if len(auth) != 1 or not auth[0].isascii() or not hmac.compare_digest(auth[0], 'Bearer ' + token):
                return self.reply(401, {'error': 'unauthorized'})
            if self.path != '/v1/runtime':
                return self.reply(404, {'error': 'not_found'})
            return self.reply(200, snapshot(path))
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--port', required=True, type=int)
    args = parser.parse_args()
    if not args.snapshot.is_absolute() or '..' in args.snapshot.parts or not 1024 <= args.port <= 65535:
        parser.error('absolute snapshot and valid unprivileged port required')
    token = credentials(args.token_file)
    if not token.isascii():
        parser.error('ASCII token required')
    server = BoundedServer(('127.0.0.1', args.port), handler(args.snapshot, token, {ipaddress.ip_address('127.0.0.1')}))
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

