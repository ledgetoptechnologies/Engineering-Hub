"""Explicit standalone public gateway; no private configuration or state owner.

This entry point is opt-in. It neither reads environment settings nor starts
anything on import. The existing runner binds literal loopback before invoking
the dedicated Unix-socket client factory; the client performs authoritative
Linux filesystem checks then. No TCP fallback or socket recovery is supplied.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import PurePosixPath
import posixpath
import re
import sys


def _socket_path(value):
    # Lexical validation is intentionally platform-independent for --help and
    # argument handling. The existing Linux helper remains authoritative.
    try:
        raw = os.fspath(value)
        if (type(raw) is not str or not raw.startswith('/') or raw.startswith('//')
                or posixpath.normpath(raw) != raw or '\x00' in raw
                or len(raw.encode('utf-8')) > 107
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',
                                PurePosixPath(raw).name) is None):
            raise ValueError()
    except (TypeError, ValueError, UnicodeError):
        raise ValueError('an explicit normalized absolute Unix socket path is required') from None
    return raw


def _port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError('public browser port must be between 1 and 65535')
    return value


async def run(socket_path, port=8771, *, preview_origin=None, allow_github=False):
    """Serve only the public browser proxy, with no implicit settings factory."""
    path = _socket_path(socket_path)
    _port(port)
    if type(allow_github) is not bool:
        raise ValueError('Explicit GitHub gateway opt-in required')
    from .private_gateway import _preview_host
    _preview_host(preview_origin)
    from .private_browser_socket import create_private_browser_client
    from .split_server import serve_public_browser

    def client_factory():
        return create_private_browser_client(path)

    options = {} if preview_origin is None else {'preview_origin': preview_origin}
    if allow_github:
        options['allow_github'] = True
    await serve_public_browser(client_factory, browser_port=port, **options)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse otherwise echoes unknown values, potentially pasted secrets.
        self.exit(2, 'Invalid public browser arguments; use --help.\n')


def main(argv=None):
    parser = _Parser(description='Engineering Hub public loopback browser gateway')
    parser.add_argument('--socket', required=True)
    parser.add_argument('--port', default=8771, type=int)
    parser.add_argument('--preview-origin', default=None,
                        help='Explicit separate HTTPS capability-preview origin (disabled by default)')
    parser.add_argument('--allow-github', action='store_true',
                        help='Allow the exact GitHub OAuth callback route (disabled by default)')
    args = parser.parse_args(argv)
    try:
        path, port = _socket_path(args.socket), _port(args.port)
        from .private_gateway import _preview_host
        _preview_host(args.preview_origin)
    except ValueError:
        parser.error('invalid explicit socket or port')
    try:
        options = {} if args.preview_origin is None else {'preview_origin': args.preview_origin}
        if args.allow_github:
            options['allow_github'] = True
        asyncio.run(run(path, port, **options))
    except Exception:
        print('Engineering Hub public browser unavailable.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
