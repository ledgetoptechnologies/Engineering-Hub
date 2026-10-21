"""Explicit NAS container-shell transport/auth check; no activation or database.
No token value is accepted in arguments or discovered from the environment.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import re
import sys
from uuid import UUID

from engineering_hub.container_pilot_client import ContainerPilotClient
from engineering_hub.unified_pilot_startup import read_protected_file

CLOSE_TIMEOUT_SECONDS = 1.0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('Invalid connection check arguments') from None


def parse(argv):
    parser = Parser(allow_abbrev=False)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--ca-file', required=True)
    parser.add_argument('--ca-sha256', required=True)
    parser.add_argument('--credential-file', required=True)
    parser.add_argument('--credential-sha256', required=True)
    parser.add_argument('--unknown-command-id', required=True)
    args = parser.parse_args(argv)
    if (not all(re.fullmatch('sha256:[0-9a-f]{64}', pin) for pin in
                (args.ca_sha256, args.credential_sha256))
            or str(UUID(args.unknown_command_id)) != args.unknown_command_id):
        raise ValueError('Invalid connection check arguments')
    if Path(args.ca_file) == Path(args.credential_file):
        raise ValueError('Invalid connection check paths')
    return args


async def check(args, *, progress=None):
    # Only fixed local stage names are public. Never use exception text or inputs.
    def stage(name):
        if progress is not None:
            progress['phase'] = name

    # Existing protected reader enforces no-follow stable retained bytes,
    # current UID0600 credential and private0700 parent, trusted ancestors.
    stage('credential_read')
    credential = read_protected_file(Path(args.credential_file), maximum_bytes=4096)
    stage('ca_read')
    ca = read_protected_file(Path(args.ca_file), maximum_bytes=1024 * 1024, private=False)
    stage('material_validation')
    if (not hmac.compare_digest('sha256:' + hashlib.sha256(credential).hexdigest(), args.credential_sha256)
            or not hmac.compare_digest('sha256:' + hashlib.sha256(ca).hexdigest(), args.ca_sha256)
            or not credential or not all(33 <= value <= 126 for value in credential)):
        raise ValueError('Invalid protected connection material')
    stage('tls_client_setup')
    client = ContainerPilotClient.for_verified_https(args.origin, credential.decode('ascii'), ca,
                                                     timeout_seconds=10.0)
    try:
        stage('transport_auth_probe')
        result = await client.probe_connection(args.unknown_command_id)
        return result
    finally:
        active_error = sys.exc_info()[1]
        if active_error is None:
            stage('client_cleanup')
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                await client.aclose()
        except BaseException:
            if isinstance(active_error, asyncio.CancelledError):
                raise active_error
            raise


def main(argv=None):
    progress = {'phase': 'arguments'}
    try:
        args = parse(argv)
        result = asyncio.run(check(args, progress=progress))
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except BaseException as error:
        if type(error) is SystemExit and error.code == 0:
            return 0
        # Do not reflect parser values, origin, paths, digests or TLS errors.
        print(json.dumps({'state': 'connection_check_failed', 'automatic_retry': False,
                          'command_posts': 0, 'reconciliation_posts': 0,
                          'phase': progress['phase']}), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
