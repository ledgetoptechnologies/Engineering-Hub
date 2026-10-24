"""Native trusted read-only assets; no jobs, installs, registration API or autostart."""
import argparse
import ipaddress
import json
import os
import sys

from .server import AssetCatalog, AssetError, AssetServer, load_token, require, tls_context, MAX_ASSET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument('--cert-file')
    parser.add_argument('--key-file')
    parser.add_argument('--allowed-peer', action='append', default=[])
    parser.add_argument('--max-asset-bytes', type=int, default=MAX_ASSET)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    host = ipaddress.ip_address(args.host)
    peers = [str(ipaddress.ip_address(value)) for value in args.allowed_peer]
    require(1024 <= args.port <= 65535, 'invalid_port')
    require(host.is_loopback or host.is_private and not host.is_unspecified and not host.is_multicast,
            'private_listener_required')
    require(bool(args.cert_file) == bool(args.key_file), 'tls_pair_required')
    require(host.is_loopback or args.cert_file and peers, 'lan_requires_tls_and_peers')
    if not peers:
        peers = ['127.0.0.1', '::1']
    token = load_token(args.token_file)
    catalog = AssetCatalog(args.data_root, args.manifest, max_asset_bytes=args.max_asset_bytes)
    context = tls_context(args.cert_file, args.key_file)
    if args.check:
        print(json.dumps({'valid': True, 'assets': len(catalog.assets), 'tls': context is not None}))
        return
    require(sys.platform == 'linux' and os.getuid() != 0, 'nonroot_linux_service_required')
    server = AssetServer((str(host), args.port), catalog, token, allowed_peers=peers, context=context)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': exc.code if isinstance(exc, AssetError) else 'asset_configuration_unavailable'}), file=sys.stderr)
        sys.exit(1)
