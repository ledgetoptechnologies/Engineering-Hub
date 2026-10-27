"""Human-operated staging pairing helper. Never print credentials except to a
configured private header-helper pipe via the explicit ``headers`` command.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, HTTPRedirectHandler, Request, build_opener


class ClientError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {'https', 'http'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or (parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'})):
        raise ClientError('https_or_loopback_origin_required')
    try:
        parsed.port
    except ValueError:
        raise ClientError('invalid_origin') from None
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
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
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


def main(argv=None):
    parser = argparse.ArgumentParser(description='Pair an isolated staging agent installation.')
    parser.add_argument('--profile-dir', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    enroll = commands.add_parser('enroll')
    enroll.add_argument('--origin', required=True)
    enroll.add_argument('--family', choices=('codex', 'claude', 'hermes'), required=True)
    enroll.add_argument('--name', required=True)
    enroll.add_argument('--host-label', required=True)
    connect = commands.add_parser('connect')
    connect.add_argument('--session', required=True)
    connect.add_argument('--label', default='')
    headers = commands.add_parser('headers', help='Private connector pipe only; do not run in chat/terminal logs.')
    headers.add_argument('--session', required=True)
    args = parser.parse_args(argv)
    if args.command in {'headers', 'connect'} and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', args.session):
        raise ClientError('bounded_local_session_name_required')
    directory = args.profile_dir.expanduser().absolute()
    installation_path = directory / 'installation.private'
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
            if installation_path.exists():
                profile = _read(installation_path)
                if profile.get('origin') != origin or profile.get('family') != args.family:
                    raise ClientError('existing_profile_binding_mismatch')
            else:
                profile = {'origin': origin, 'family': args.family,
                           'secret': 'ehi_' + secrets.token_urlsafe(32)}
                # Save before the request: a lost response must not generate a new identity.
                _write(installation_path, profile)
            result = _request(origin, '/enrollments', {
                'family': args.family, 'name': args.name, 'host_label': args.host_label,
                'credential_sha256': hashlib.sha256(profile['secret'].encode()).hexdigest()})
            profile['installation_id'] = result['id']
            _write(installation_path, profile)
            print(json.dumps({key: result[key] for key in
                              ('id', 'family', 'state', 'pairing_code', 'key_fingerprint', 'expires_at')}))
            return 0
        profile = _read(installation_path)
        session_path = directory / (args.session + '.session.private')
        if session_path.exists():
            raise ClientError('session_profile_exists_choose_a_new_name')
        result = _request(_origin(profile['origin']), '/exchange', {'label': args.label}, profile['secret'])
        from datetime import datetime
        issued = result['session']
        expiry = datetime.fromisoformat(issued['expires_at'].replace('Z', '+00:00')).timestamp()
        _write(session_path, {'token': result['token'], 'expires_at': expiry,
                              'installation_id': profile['installation_id'], 'session_id': issued['id']})
        print(json.dumps({'installation_id': profile['installation_id'], 'session_id': issued['id'],
                          'expires_at': issued['expires_at'], 'mcp_url': profile['origin'] + '/mcp'}))
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ClientError, OSError, ValueError, KeyError):
        # Fixed failure only; no traceback/profile/server-response secret disclosure.
        print('agent_connection_setup_failed; profile retained for local inspection', file=sys.stderr)
        raise SystemExit(1) from None
