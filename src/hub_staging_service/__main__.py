"""Private bearer-authenticated staging HTTP worker, normally behind private TLS proxy."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
from pathlib import Path
import stat
import threading
from urllib.parse import unquote, urlsplit

from .core import Rejected, Service, ensure, storage_limits
from .docker import DockerBackend


class BoundedServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        ensure(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def credentials(path):
    item = Path(path)
    ensure(item.is_absolute() and not item.is_symlink(), 'credential_reference_required')
    info = item.stat()
    ensure(stat.S_ISREG(info.st_mode) and info.st_size <= 4096 and
           not stat.S_IMODE(info.st_mode) & 0o077, 'credential_file_protection')
    token = item.read_text().strip()
    ensure(16 <= len(token) <= 4096 and '\n' not in token and '\r' not in token, 'credential_file_invalid')
    return token


def handler(service, token, peers, *, host_snapshot=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'HubStaging/1'

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass  # Never log bearer headers, source payloads, or arbitrary paths.

        def respond(self, status, value, content_type='application/json'):
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "sandbox allow-scripts; default-src 'self' data:; style-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

        def authorize(self):
            # Socket address only. X-Forwarded-For and human cookies confer nothing.
            ensure(ipaddress.ip_address(self.client_address[0]) in peers, 'peer_not_allowed', 403)
            values = self.headers.get_all('Authorization', [])
            ensure(len(values) == 1 and hmac.compare_digest(values[0], 'Bearer ' + token), 'unauthorized', 401)

        def dispatch(self):
            try:
                self.authorize()
                path = unquote(urlsplit(self.path).path)
                parts = path.strip('/').split('/')
                if self.command == 'GET':
                    if parts == ['v1', 'runtime']:
                        from .runtime_inventory import collect_runtime
                        return self.respond(200, collect_runtime(service.backend, host_snapshot))
                    if parts == ['v1', 'status']:
                        return self.respond(200, {'schema': 'hub-staging.v1', 'active_command_id': service.active,
                                                  'projects': {name: sorted(recipes) for name, recipes in service.projects.items()}})
                    if len(parts) == 3 and parts[:2] == ['v1', 'jobs']:
                        return self.respond(200, service.status(parts[2]))
                    if len(parts) == 3 and parts[:2] == ['v1', 'operations']:
                        return self.respond(200, service.operation(parts[2]))
                    if len(parts) == 4 and parts[:2] == ['v1', 'resources'] and parts[3] == 'metadata':
                        return self.respond(200, service.metadata(parts[2]))
                    if len(parts) >= 5 and parts[:2] == ['v1', 'resources'] and parts[3] == 'files':
                        name = '/'.join(parts[4:])
                        return self.respond(200, service.file(parts[2], name), mimetypes.guess_type(name)[0] or 'application/octet-stream')
                if self.command == 'POST':
                    ensure(not self.headers.get('Transfer-Encoding'), 'transfer_encoding_not_supported')
                    sizes = self.headers.get_all('Content-Length', [])
                    ensure(len(sizes) == 1 and sizes[0].isdigit() and 0 < int(sizes[0]) <= 12 * 1024 * 1024, 'request_size')
                    data = self.rfile.read(int(sizes[0]))
                    ensure(len(data) == int(sizes[0]), 'incomplete_request')
                    payload = json.loads(data, object_pairs_hook=unique_object)
                    if parts == ['v1', 'jobs']:
                        return self.respond(202, service.submit(payload))
                    if parts == ['v1', 'sources']:
                        return self.respond(200, service.put_source(payload))
                    if parts == ['v1', 'cleanup']:
                        return self.respond(200, service.cleanup(payload))
                self.respond(404, {'error': 'not_found'})
            except Rejected as exc:
                self.respond(exc.status, {'error': exc.code})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {'error': 'invalid_request'})
            except Exception:
                self.respond(500, {'error': 'worker_error'})

        do_GET = dispatch
        do_POST = dispatch
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    token = credentials(config['token_file'])
    peers = {ipaddress.ip_address(item) for item in config['allowed_peer_ips']}
    ensure(bool(peers), 'allowed_peers_required')
    from .core import recipe
    projects = {name: {'recipes': {key: recipe(profile) for key, profile in
                value.get('recipes', {'default': value}).items()}} for name, value in config['projects'].items()}
    ensure(bool(projects), 'projects_required')
    ensure(Path(config['worker_root']).is_absolute(), 'absolute_worker_root_required')
    bind = str(ipaddress.ip_address(config.get('bind', '127.0.0.1')))
    port = config.get('port', 8780)
    ensure(type(port) is int and 1024 <= port <= 65535, 'invalid_port')
    backend = DockerBackend(config['docker_endpoint'])
    host_snapshot = config.get('host_snapshot')
    if host_snapshot is not None:
        ensure(type(host_snapshot) is str and Path(host_snapshot).is_absolute()
               and '..' not in Path(host_snapshot).parts, 'invalid_host_snapshot')
    capacity_bytes, free_floor_bytes = storage_limits(
        config.get('capacity_bytes', 1024**3), config.get('free_floor_bytes', 512 * 1024**2))
    if args.check_config:
        print(json.dumps({'valid': True, 'projects': sorted(projects), 'credentials_disclosed': False}))
        return
    service = Service(config['worker_root'], projects, backend,
                      capacity_bytes=capacity_bytes, free_floor_bytes=free_floor_bytes)
    server = BoundedServer((bind, port), handler(service, token, peers, host_snapshot=host_snapshot))
    server.daemon_threads = True
    server.timeout = 10
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
