"""Request one fresh proposal from an explicitly selected root supervisor.

The output is a newly created proposal-<nonce>.json next to the socket. This
command does not approve a launch or alter its original expiration.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import sys
from uuid import UUID

PROTOCOL = 'engineering-hub.launch-proposal-request.v1'


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--launch-id', required=True)
    args = parser.parse_args(argv)
    try:
        if os.getuid() != 0 or os.geteuid() != 0 or str(UUID(args.launch_id)) != args.launch_id:
            raise ValueError()
        path = Path(args.socket)
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError()
        nonce = secrets.token_hex(32)
        body = json.dumps({'protocol': PROTOCOL, 'launch_id': args.launch_id, 'nonce': nonce},
                          sort_keys=True, separators=(',', ':')).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(10)
            peer.connect(str(path))
            pid, uid, _ = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid <= 0 or uid != 0:
                raise ValueError()
            peer.sendall(struct.pack('!I', len(body)) + body)
            peer.shutdown(socket.SHUT_WR)
            if peer.recv(3) != b'OK':
                raise ValueError()
        print(str(path.parent / ('proposal-' + nonce + '.json')))
        return 0
    except Exception:
        print('proposal request unavailable; preserve evidence', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
