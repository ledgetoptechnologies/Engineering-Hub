"""Human-operated staging pairing helper. Never print credentials except to a
configured private header-helper pipe via the explicit ``headers`` command.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, HTTPRedirectHandler, Request, build_opener


class ClientError(Exception):
    pass


_RFC1918_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _origin(value):
    if type(value) is not str:
        raise ClientError('invalid_origin')
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ClientError('invalid_origin') from None
    if (parsed.scheme not in {'https', 'http'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or (parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'})):
        raise ClientError('https_or_loopback_origin_required')
    try:
        parsed.port
    except ValueError:
        raise ClientError('invalid_origin') from None
    return value


def _private_origin(value):
    if type(value) is not str or not value or '%' in value:
        raise ClientError('invalid_private_vpn_origin')
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or '')
        port = parsed.port
    except ValueError:
        raise ClientError('invalid_private_vpn_origin') from None
    if (parsed.scheme != 'http' or type(address) is not ipaddress.IPv4Address
            or port is None or not 1 <= port <= 65535
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or not any(address in network for network in _RFC1918_NETWORKS)
            or value != f'http://{address}:{port}'):
        raise ClientError('invalid_private_vpn_origin')
    return value


def _protect(raw, *, decrypt=False):
    if os.name != 'nt':
        return raw
    # DPAPI binds both confidentiality and integrity to this Windows user.
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ClientError('private_storage_unavailable')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def _read(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            raise ClientError('invalid_private_profile')
        if os.name != 'nt' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ClientError('private_profile_permissions_required')
        with os.fdopen(descriptor, 'rb') as source:
            descriptor = -1
            raw = source.read(16385)
        data = json.loads(_protect(raw, decrypt=True))
        if type(data) is not dict:
            raise ClientError('invalid_private_profile')
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = _protect(json.dumps(data, sort_keys=True).encode())
    descriptor, temporary = tempfile.mkstemp(prefix='.agent-profile-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _profile_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / '.pairing.lock'
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ClientError('profile_operation_in_progress') from None
    try:
        os.close(descriptor)
        yield
    finally:
        path.unlink()


def _request(origin, path, payload, token=None):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
               'User-Agent': 'Engineering-Hub-Agent-Client/0.7'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    request = Request(origin + '/api/agent-sessions/v1' + path,
                      json.dumps(payload).encode(), headers, method='POST')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(65537)
            if len(raw) > 65536 or response.status != 200:
                raise ClientError('invalid_server_response')
            data = json.loads(raw)
            if type(data) is not dict:
                raise ClientError('invalid_server_response')
            return data
    except HTTPError as error:
        # Never print error response bodies, supplied labels or bearer values.
        raise ClientError('server_request_denied_' + str(error.code)) from None
    except (URLError, TimeoutError):
        raise ClientError('server_unavailable_profile_retained') from None



def _listen_attention(directory, name, project, generation, private_vpn):
    """Explicit foreground SSE consumer; prints only fixed content-free events."""
    if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', name)
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', project)
            or type(generation) is not int or not 1 <= generation < (1 << 63)):
        raise ClientError('invalid_attention_subscription')
    profile = _read(directory / 'installation.private')
    session = _read(directory / (name + '.session.private'))
    selected_private = profile.get('private_vpn', False)
    if type(selected_private) is not bool or selected_private != private_vpn:
        raise ClientError('private_vpn_profile_mismatch')
    if selected_private:
        owner_origin = _origin(profile.get('origin'))
        if urlsplit(owner_origin).scheme != 'https':
            raise ClientError('invalid_private_profile')
        origin = _private_origin(profile.get('private_origin'))
    else:
        if profile.get('private_origin') is not None:
            raise ClientError('invalid_private_profile')
        origin = _origin(profile.get('origin'))
    token, expires = session.get('token'), session.get('expires_at')
    try:
        installation_id = profile.get('installation_id')
        if type(installation_id) is not str or str(uuid.UUID(installation_id)) != installation_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ClientError('invalid_private_profile') from None
    if (type(token) is not str or not re.fullmatch(r'ehs_[A-Za-z0-9_-]{43}', token)
            or type(expires) not in (int, float) or not math.isfinite(expires) or expires <= time.time()
            or session.get('installation_id') != installation_id):
        raise ClientError('invalid_or_expired_session')
    query = urlencode({'project': project, 'generation': generation})
    request = Request(origin + '/api/agent-sessions/v1/hub-attention-stream?' + query,
                      headers={'Authorization': 'Bearer ' + token, 'Accept': 'text/event-stream',
                               'User-Agent': 'Engineering-Hub-Agent-Client/0.7'}, method='GET')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=max(1, min(3600, expires - time.time()))) as response:
            if response.status != 200 or response.headers.get_content_type() != 'text/event-stream':
                raise ClientError('invalid_server_response')
            event = data = None
            while True:
                raw = response.readline(1025)
                if not raw:
                    if event is not None or data is not None:
                        raise ClientError('invalid_server_response')
                    return 0
                if len(raw) > 1024:
                    raise ClientError('invalid_server_response')
                line = raw.rstrip(b'\r\n')
                if not line:
                    if event is not None or data is not None:
                        if event not in ('attention-ready', 'attention-check') or data != '{}':
                            raise ClientError('invalid_server_response')
                        print(json.dumps({'event': event}), flush=True)
                    event = data = None
                elif line.startswith(b':'):
                    continue
                elif line.startswith(b'event: ') and event is None:
                    event = line[7:].decode('ascii')
                elif line.startswith(b'data: ') and data is None:
                    data = line[6:].decode('ascii')
                else:
                    raise ClientError('invalid_server_response')
    except HTTPError as error:
        raise ClientError('server_request_denied_' + str(error.code)) from None
    except (URLError, TimeoutError, UnicodeError):
        raise ClientError('server_unavailable_profile_retained') from None

def main(argv=None):
    parser = argparse.ArgumentParser(description='Pair an isolated agent installation.')
    parser.add_argument('--profile-dir', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    enroll = commands.add_parser('enroll')
    enroll.add_argument('--origin', required=True)
    enroll.add_argument('--private-vpn', action='store_true')
    enroll.add_argument('--private-origin')
    enroll.add_argument('--family', choices=('codex', 'claude', 'hermes'), required=True)
    enroll.add_argument('--name', required=True)
    enroll.add_argument('--host-label', required=True)
    connect = commands.add_parser('connect')
    connect.add_argument('--session', required=True)
    connect.add_argument('--label', default='')
    connect.add_argument('--private-vpn', action='store_true')
    headers = commands.add_parser('headers', help='Private connector pipe only; do not run in chat/terminal logs.')
    headers.add_argument('--session', required=True)
    listen = commands.add_parser('listen-attention', help='Explicit foreground, event-driven attention hints.')
    listen.add_argument('--session', required=True)
    listen.add_argument('--project', required=True)
    listen.add_argument('--generation', type=int, required=True)
    listen.add_argument('--private-vpn', action='store_true')
    args = parser.parse_args(argv)
    if args.command in {'headers', 'connect', 'listen-attention'} and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', args.session):
        raise ClientError('bounded_local_session_name_required')
    directory = args.profile_dir.expanduser().absolute()
    installation_path = directory / 'installation.private'
    if args.command == 'listen-attention':
        return _listen_attention(directory, args.session, args.project, args.generation, args.private_vpn)
    if args.command == 'headers':
        session = _read(directory / (args.session + '.session.private'))
        if type(session.get('expires_at')) not in (float, int) or session['expires_at'] <= time.time():
            raise ClientError('session_expired_reconnect_explicitly')
        token = session.get('token')
        if type(token) is not str or not re.fullmatch(r'ehs_[A-Za-z0-9_-]{43}', token):
            raise ClientError('invalid_private_profile')
        print(json.dumps({'Authorization': 'Bearer ' + token}))
        return 0
    with _profile_lock(directory):
        if args.command == 'enroll':
            origin = _origin(args.origin)
            if args.private_vpn:
                if urlsplit(origin).scheme != 'https':
                    raise ClientError('private_vpn_requires_https_owner_origin')
                private_origin = _private_origin(args.private_origin)
            else:
                if args.private_origin is not None:
                    raise ClientError('private_origin_requires_private_vpn_flag')
                private_origin = None
            if installation_path.exists():
                profile = _read(installation_path)
                selected_private = profile.get('private_vpn', False)
                if (type(selected_private) is not bool
                        or profile.get('origin') != origin or profile.get('family') != args.family
                        or selected_private != args.private_vpn
                        or profile.get('private_origin') != private_origin):
                    raise ClientError('existing_profile_binding_mismatch')
            else:
                profile = {'origin': origin, 'family': args.family, 'private_vpn': args.private_vpn,
                           'private_origin': private_origin,
                           'secret': 'ehi_' + secrets.token_urlsafe(32)}
                # Save before the request: a lost response must not generate a new identity.
                _write(installation_path, profile)
            result = _request(private_origin or origin, '/enrollments', {
                'family': args.family, 'name': args.name, 'host_label': args.host_label,
                'credential_sha256': hashlib.sha256(profile['secret'].encode()).hexdigest()})
            profile['installation_id'] = result['id']
            _write(installation_path, profile)
            print(json.dumps({key: result[key] for key in
                              ('id', 'family', 'state', 'pairing_code', 'key_fingerprint', 'expires_at')}))
            return 0
        profile = _read(installation_path)
        selected_private = profile.get('private_vpn', False)
        if type(selected_private) is not bool:
            raise ClientError('invalid_private_profile')
        if selected_private:
            if not args.private_vpn:
                raise ClientError('private_vpn_flag_required')
            owner_origin = _origin(profile.get('origin'))
            if urlsplit(owner_origin).scheme != 'https':
                raise ClientError('invalid_private_profile')
            transport_origin = _private_origin(profile.get('private_origin'))
        else:
            if args.private_vpn:
                raise ClientError('private_vpn_profile_required')
            if profile.get('private_origin') is not None:
                raise ClientError('invalid_private_profile')
            transport_origin = _origin(profile.get('origin'))
        session_path = directory / (args.session + '.session.private')
        if session_path.exists():
            raise ClientError('session_profile_exists_choose_a_new_name')
        result = _request(transport_origin, '/exchange', {'label': args.label}, profile['secret'])
        from datetime import datetime
        issued = result['session']
        expiry = datetime.fromisoformat(issued['expires_at'].replace('Z', '+00:00')).timestamp()
        _write(session_path, {'token': result['token'], 'expires_at': expiry,
                              'installation_id': profile['installation_id'], 'session_id': issued['id']})
        print(json.dumps({'installation_id': profile['installation_id'], 'session_id': issued['id'],
                          'expires_at': issued['expires_at'], 'mcp_url': transport_origin + '/mcp'}))
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ClientError, OSError, ValueError, KeyError):
        # Fixed failure only; no traceback/profile/server-response secret disclosure.
        print('agent_connection_setup_failed; profile retained for local inspection', file=sys.stderr)
        raise SystemExit(1) from None
