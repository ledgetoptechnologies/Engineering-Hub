"""Private bearer-authenticated staging HTTP worker, normally behind private TLS proxy."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from .core import Rejected, Service, ensure, storage_limits
from .docker import DockerBackend

# Development interlock: scoped execution and external fencing are incomplete.
_SCOPED_LIFECYCLE_READY = False


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
    """Read one protected POSIX credential without reopening a checked path.

    This does not protect against same-UID/root attackers or establish an
    effective Windows ACL. Parent directories must be controlled; a root-owned
    sticky directory is allowed only for a root/current-user-owned child.
    """
    ensure(os.name == 'posix' and all(hasattr(os, name) for name in
           ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK')), 'credential_protection_unavailable', 503)
    try:
        item = Path(path)
    except (TypeError, ValueError):
        raise Rejected('credential_reference_required') from None
    ensure(item.is_absolute() and 2 <= len(item.parts) <= 128 and '..' not in item.parts,
           'credential_reference_required')
    try:
        ensure(len(os.fsencode(item)) <= 4096, 'credential_reference_required')
    except UnicodeError:
        raise Rejected('credential_reference_required') from None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    fds, edges = [], []
    def identity(info): return info.st_dev, info.st_ino
    def trusted_directory(info):
        mode = stat.S_IMODE(info.st_mode)
        sticky_root = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
        ensure(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.geteuid())
               and (not mode & 0o022 or sticky_root), 'credential_directory_protection')
        return sticky_root
    try:
        parent = os.open('/', flags)
        fds.append(parent)
        root_identity = identity(os.fstat(parent))
        for name in item.parts[1:-1]:
            sticky = trusted_directory(os.fstat(parent))
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            ensure(stat.S_ISDIR(before.st_mode) and (not sticky or before.st_uid in (0, os.geteuid())),
                   'credential_directory_protection')
            child = os.open(name, flags, dir_fd=parent)
            fds.append(child)
            ensure(identity(before) == identity(os.fstat(child)), 'credential_directory_changed')
            edges.append((parent, name, child, identity(before)))
            parent = child
        sticky = trusted_directory(os.fstat(parent))
        name = item.name
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        ensure(stat.S_ISREG(before.st_mode) and before.st_uid in (0, os.geteuid())
               and (not sticky or before.st_uid in (0, os.geteuid())) and before.st_nlink == 1
               and not stat.S_IMODE(before.st_mode) & 0o077 and 0 < before.st_size <= 4096,
               'credential_file_protection')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0), dir_fd=parent)
        fds.append(fd)
        opened = os.fstat(fd)
        def version(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid,
                    info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)
        ensure(version(opened) == version(before), 'credential_file_changed')
        raw = bytearray()
        while len(raw) <= 4096:
            chunk = os.read(fd, 4097 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        ensure(len(raw) == before.st_size and len(raw) <= 4096, 'credential_file_changed')
        os.lseek(fd, 0, os.SEEK_SET)
        check = bytearray()
        while len(check) <= 4096:
            chunk = os.read(fd, 4097 - len(check))
            if not chunk:
                break
            check.extend(chunk)
        ensure(hmac.compare_digest(bytes(raw), bytes(check)), 'credential_file_changed')
        ensure(version(os.fstat(fd)) == version(before) and
               version(os.stat(name, dir_fd=parent, follow_symlinks=False)) == version(before),
               'credential_file_changed')
        ensure(identity(os.stat('/', follow_symlinks=False)) == root_identity, 'credential_directory_changed')
        trusted_directory(os.fstat(fds[0]))
        trusted_directory(os.fstat(parent))
        for ancestor, edge, child, expected in edges:
            ensure(identity(os.fstat(child)) == expected == identity(os.stat(edge, dir_fd=ancestor, follow_symlinks=False)),
                   'credential_directory_changed')
            trusted_directory(os.fstat(child))
        try:
            token = bytes(raw).decode('ascii')
        except UnicodeError:
            raise Rejected('credential_file_invalid') from None
        if token.endswith('\n'):
            token = token[:-1]
        ensure(16 <= len(token) <= 4096 and all(0x21 <= ord(char) <= 0x7e for char in token),
               'credential_file_invalid')
        return token
    except (OSError, TypeError, ValueError, NotImplementedError):
        raise Rejected('credential_file_invalid') from None
    finally:
        close_failed = False
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                close_failed = True
        if close_failed:
            raise Rejected('credential_file_invalid') from None


def scoped_credentials(config, projects, legacy_token):
    """Load explicit static principals; this is not live revocation or an epoch fence."""
    ensure(type(config) is list and 0 < len(config) <= 32, 'invalid_v2_credentials')
    output = []
    for item in config:
        ensure(type(item) is dict and set(item) in ({'principal', 'token_file', 'projects'},
               {'principal', 'token_file', 'projects', 'recipes'}), 'invalid_v2_credentials')
        principal, allowed = item['principal'], item['projects']
        ensure(type(principal) is str and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', principal), 'invalid_v2_credentials')
        ensure(type(allowed) is list and allowed and all(type(p) is str and p in projects for p in allowed)
               and len(set(allowed)) == len(allowed), 'invalid_v2_credentials')
        recipes = item.get('recipes', {})
        ensure(type(recipes) is dict and set(recipes) <= set(allowed), 'invalid_v2_credentials')
        for project, values in recipes.items():
            configured = projects[project]['recipes']
            ensure(type(values) is list and values and all(type(value) is str and value in configured for value in values)
                   and len(set(values)) == len(values), 'invalid_v2_credentials')
        value = credentials(item['token_file'])
        ensure(not hmac.compare_digest(value, legacy_token)
               and all(principal != prior[0] and not hmac.compare_digest(value, prior[1]) for prior in output),
               'invalid_v2_credentials')
        output.append((principal, value, frozenset(allowed), {key: frozenset(value) for key, value in recipes.items()}))
    return tuple(output)


def observer_credential(config, legacy_token, v2_credentials):
    """Separate static, operator-only credential; rotation requires restart."""
    ensure(type(config) is dict and set(config) == {'token_file'} and
           type(config['token_file']) is str, 'invalid_observer_credential')
    value = credentials(config['token_file'])
    ensure(not hmac.compare_digest(value, legacy_token) and
           all(not hmac.compare_digest(value, entry[1]) for entry in v2_credentials or ()),
           'invalid_observer_credential')
    return value


_OBSERVER = object()


def handler(service, token, peers, *, host_snapshot=None, v2_credentials=None, observer_token=None):
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
            ensure(len(values) == 1, 'unauthorized', 401)
            if hmac.compare_digest(values[0], 'Bearer ' + token):
                return None
            if observer_token is not None and hmac.compare_digest(values[0], 'Bearer ' + observer_token):
                return _OBSERVER
            for entry in v2_credentials or ():
                principal, secret, projects = entry[:3]
                if hmac.compare_digest(values[0], 'Bearer ' + secret):
                    return principal, projects, entry[3] if len(entry) == 4 else {}
            raise Rejected('unauthorized', 401)

        def dispatch(self):
            try:
                identity = self.authorize()
                raw_path = urlsplit(self.path).path
                if identity is _OBSERVER:
                    ensure(self.command == 'GET' and self.path == '/v2/operator/runtime', 'not_found', 404)
                    from .runtime_inventory import collect_runtime
                    return self.respond(200, collect_runtime(service.backend, host_snapshot))
                decoded_path = unquote(raw_path)
                if identity is not None or decoded_path == '/v2' or decoded_path.startswith('/v2/'):
                    ensure('%' not in raw_path and '\\' not in raw_path and '//' not in raw_path,
                           'not_found', 404)
                if identity is not None:
                    ensure(raw_path.startswith('/v2/') and not raw_path.endswith('/'), 'not_found', 404)
                elif raw_path == '/v2/status' and self.command == 'GET' and v2_credentials is None:
                    # The unscoped protocol has no principal boundary and must
                    # never advertise readiness, even if experimental globals
                    # are enabled in a test or future build.
                    from . import core
                    return self.respond(200, {'schema': 'hub-staging.v2', 'active_command_id': service.legacy_active(),
                        'profile_protocol': core.PROFILE_PROTOCOL, 'profile_protocol_ready': False,
                        'projects': {name: sorted(recipes) for name, recipes in service.projects.items()}})
                else:
                    ensure(not (raw_path == '/v2' or raw_path.startswith('/v2/')), 'not_found', 404)
                path = decoded_path
                parts = path.strip('/').split('/')
                if identity is not None:
                    principal, allowed, recipes = identity
                    if self.command == 'GET' and parts == ['v2', 'status']:
                        from . import core
                        return self.respond(200, {'schema': 'hub-staging.v2', 'active_command_id': service.scoped_active(principal, allowed, recipes),
                            'profile_protocol': core.PROFILE_PROTOCOL,
                            'profile_protocol_ready': bool(_SCOPED_LIFECYCLE_READY and core._PROFILE_PROTOCOL_READY
                                                           and core._SOURCE_ATTEMPT_ENABLED and core._SOURCE_EVIDENCE_ENABLED),
                            'projects': {project: sorted(recipes[project]) for project in allowed if recipes.get(project)}})
                    if self.command == 'GET' and len(parts) == 3 and parts[:2] == ['v2', 'source-attempts']:
                        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, strict_parsing=True)
                        ensure(set(query) == {'project', 'recipe'} and all(len(values) == 1 for values in query.values()),
                               'source_attempt_query')
                        ensure(query['project'][0] in allowed and query['recipe'][0] in recipes.get(query['project'][0], ()),
                               'not_found', 404)
                        return self.respond(200, service.source_attempt(parts[2], query['project'][0],
                                                                       principal=principal, recipe=query['recipe'][0]))
                    if self.command == 'GET' and len(parts) == 3 and parts[:2] == ['v2', 'source-evidence']:
                        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, strict_parsing=True)
                        ensure(set(query) == {'project', 'recipe', 'digest', 'binding'} and all(len(values) == 1 for values in query.values())
                               and len(query['binding'][0]) <= 1024, 'source_evidence_query')
                        ensure(query['project'][0] in allowed and query['recipe'][0] in recipes.get(query['project'][0], ()),
                               'not_found', 404)
                        binding = json.loads(query['binding'][0], object_pairs_hook=unique_object)
                        return self.respond(200, service.scoped_source_evidence(parts[2], query['project'][0], query['recipe'][0],
                            query['digest'][0], binding, principal, allowed))
                    if self.command == 'GET' and len(parts) == 3 and parts[:2] == ['v2', 'jobs']:
                        return self.respond(200, service.scoped_status(parts[2], principal, allowed, recipes))
                    if self.command == 'GET' and len(parts) == 3 and parts[:2] == ['v2', 'operations']:
                        return self.respond(200, service.scoped_operation(parts[2], principal, allowed, recipes))
                    if self.command == 'GET' and len(parts) == 4 and parts[:2] == ['v2', 'resources'] and parts[3] == 'metadata':
                        return self.respond(200, service.scoped_metadata(parts[2], principal, allowed, recipes))
                    if self.command == 'GET' and len(parts) >= 6 and parts[:2] == ['v2', 'resources'] and parts[3] == 'diagnostics':
                        return self.respond(200, service.scoped_diagnostic(parts[2], parts[4], '/'.join(parts[5:]), principal, allowed, recipes), 'application/octet-stream')
                    if self.command == 'GET' and len(parts) >= 5 and parts[:2] == ['v2', 'resources'] and parts[3] == 'files':
                        name = '/'.join(parts[4:])
                        return self.respond(200, service.scoped_file(parts[2], name, principal, allowed, recipes), mimetypes.guess_type(name)[0] or 'application/octet-stream')
                    ensure(self.command == 'POST' and parts in (['v2', 'source-attempts'], ['v2', 'jobs'], ['v2', 'cleanup']),
                           'v2_lifecycle_unavailable', 503)
                if self.command == 'GET':
                    if len(parts) == 3 and parts[:2] == ['v2', 'source-attempts']:
                        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, strict_parsing=True)
                        ensure(set(query) == {'project'} and len(query['project']) == 1, 'source_attempt_query')
                        return self.respond(200, service.source_attempt(parts[2], query['project'][0]))
                    if len(parts) == 3 and parts[:2] == ['v2', 'source-evidence']:
                        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True, strict_parsing=True)
                        ensure(set(query) == {'project', 'digest', 'binding'} and all(len(values) == 1 for values in query.values())
                               and len(query['binding'][0]) <= 1024,
                               'source_evidence_query')
                        binding = json.loads(query['binding'][0], object_pairs_hook=unique_object)
                        return self.respond(200, service.source_evidence(parts[2], query['project'][0], query['digest'][0], binding))
                    if parts == ['v1', 'runtime']:
                        ensure(v2_credentials is None and service.legacy_runtime_allowed(), 'not_found', 404)
                        from .runtime_inventory import collect_runtime
                        return self.respond(200, collect_runtime(service.backend, host_snapshot))
                    if parts == ['v1', 'status']:
                        return self.respond(200, {'schema': 'hub-staging.v1', 'active_command_id': service.legacy_active(scoped_mode=v2_credentials is not None),
                            'projects': {name: sorted(recipes) for name, recipes in service.projects.items()}})
                    if parts == ['v2', 'status']:
                        from . import core
                        return self.respond(200, {'schema': 'hub-staging.v2', 'active_command_id': service.active,
                            'profile_protocol': core.PROFILE_PROTOCOL,
                            'profile_protocol_ready': core._PROFILE_PROTOCOL_READY and core._SOURCE_ATTEMPT_ENABLED,
                            'projects': {name: sorted(recipes) for name, recipes in service.projects.items()}})
                    if len(parts) == 3 and parts[:2] == ['v1', 'jobs']:
                        return self.respond(200, service.legacy_status(parts[2]))
                    if len(parts) == 3 and parts[:2] == ['v1', 'operations']:
                        return self.respond(200, service.legacy_operation(parts[2]))
                    if len(parts) == 4 and parts[:2] == ['v1', 'resources'] and parts[3] == 'metadata':
                        return self.respond(200, service.legacy_metadata(parts[2]))
                    if len(parts) >= 6 and parts[:2] == ['v1', 'resources'] and parts[3] == 'diagnostics':
                        return self.respond(200, service.legacy_diagnostic(parts[2], parts[4], '/'.join(parts[5:])), 'application/octet-stream')
                    if len(parts) >= 5 and parts[:2] == ['v1', 'resources'] and parts[3] == 'files':
                        name = '/'.join(parts[4:])
                        return self.respond(200, service.legacy_file(parts[2], name), mimetypes.guess_type(name)[0] or 'application/octet-stream')
                if self.command == 'POST':
                    ensure(not self.headers.get('Transfer-Encoding'), 'transfer_encoding_not_supported')
                    sizes = self.headers.get_all('Content-Length', [])
                    ensure(len(sizes) == 1 and sizes[0].isdigit() and 0 < int(sizes[0]) <= 12 * 1024 * 1024, 'request_size')
                    data = self.rfile.read(int(sizes[0]))
                    ensure(len(data) == int(sizes[0]), 'incomplete_request')
                    payload = json.loads(data, object_pairs_hook=unique_object)
                    if identity is not None:
                        ensure(type(payload) is dict, 'invalid_request')
                        from . import core
                        ensure(_SCOPED_LIFECYCLE_READY and core._PROFILE_PROTOCOL_READY and core._SOURCE_ATTEMPT_ENABLED
                               and core._SOURCE_EVIDENCE_ENABLED,
                               'v2_lifecycle_unavailable', 503)
                        if parts == ['v2', 'source-attempts']:
                            reviewed = payload.get('reviewed_profile')
                            ensure(type(reviewed) is dict and reviewed.get('project') == payload.get('project')
                                   and payload.get('project') in allowed and
                                   reviewed.get('recipe') in recipes.get(payload.get('project'), ()),
                                   'not_found', 404)
                            selected_recipe = reviewed['recipe']
                            return self.respond(200, service.put_source_attempt(
                                {key: value for key, value in payload.items() if key != 'reviewed_profile'},
                                principal=principal, recipe=selected_recipe, reviewed_profile=reviewed))
                        if parts == ['v2', 'jobs']:
                            ensure(payload.get('project') in allowed and payload.get('recipe') in recipes.get(payload.get('project'), ()),
                                   'not_found', 404)
                            return self.respond(202, service._submit_v2(payload, principal=principal, projects=allowed, recipes=recipes))
                        return self.respond(200, service.cleanup(payload, principal=principal, projects=allowed, recipes=recipes))
                    if parts == ['v2', 'source-attempts']:
                        return self.respond(200, service.put_source_attempt(payload))
                    if parts == ['v1', 'jobs']:
                        return self.respond(202, service._legacy_submit(payload))
                    if parts == ['v1', 'sources']:
                        return self.respond(200, service._legacy_put_source(payload))
                    if parts == ['v2', 'jobs']:
                        return self.respond(202, service._submit_v2(payload))
                    if parts == ['v2', 'sources']:
                        return self.respond(200, service._put_source_v2(payload))
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
    v2_credentials = (scoped_credentials(config['v2_credentials'], projects, token)
                      if 'v2_credentials' in config else None)
    observer_token = (observer_credential(config['observer_credential'], token, v2_credentials)
                      if 'observer_credential' in config else None)
    ensure(Path(config['worker_root']).is_absolute(), 'absolute_worker_root_required')
    bind = str(ipaddress.ip_address(config.get('bind', '127.0.0.1')))
    port = config.get('port', 8780)
    ensure(type(port) is int and 1024 <= port <= 65535, 'invalid_port')
    backend = DockerBackend(config['docker_endpoint'])
    scratch_slot = None
    if config.get('scratch_slot') is not None:
        from .scratch import ScratchSlot
        scratch_slot = ScratchSlot.from_config(config['scratch_slot'])
    from .core import terminal_receipt_config
    terminal_receipt_config(config.get('terminal_receipts'), config['worker_root'])
    host_snapshot = config.get('host_snapshot')
    if host_snapshot is not None:
        ensure(type(host_snapshot) is str and Path(host_snapshot).is_absolute()
               and '..' not in Path(host_snapshot).parts, 'invalid_host_snapshot')
    capacity_bytes, free_floor_bytes = storage_limits(
        config.get('capacity_bytes', 1024**3), config.get('free_floor_bytes', 512 * 1024**2))
    if args.check_config:
        print(json.dumps({'valid': True, 'projects': sorted(projects), 'credentials_disclosed': False}))
        return
    storage = dict(capacity_bytes=capacity_bytes, free_floor_bytes=free_floor_bytes)
    if scratch_slot is not None:
        storage['scratch_slot'] = scratch_slot
    if config.get('terminal_receipts') is not None:
        storage['terminal_receipts'] = config['terminal_receipts']
    service = Service(config['worker_root'], projects, backend, **storage)
    server = BoundedServer((bind, port), handler(service, token, peers, host_snapshot=host_snapshot,
                                                v2_credentials=v2_credentials, observer_token=observer_token))
    server.daemon_threads = True
    server.timeout = 10
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


if __name__ == '__main__':
    main()
