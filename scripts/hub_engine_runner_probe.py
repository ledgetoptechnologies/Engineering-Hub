"""Read-only explicit Linux Engine runner probe, not pilot acceptance.

No environment discovery, image pull, container effect or state provisioning.
The existing concrete backend verifies only enrolled daemon identity/OS/arch.
Generation-root is a required inert binding field, never created by this probe.
"""
from __future__ import annotations

import argparse
import json
from pathlib import PurePosixPath
import re
import sys

from engineering_hub.container_pilot_manifest import Platform


def _backend_types():
    # The optional host package is Linux-only (fcntl). Defer its imports until
    # after the platform gate so importing this probe is portable and inert.
    from hub_container_executor.backend import RunnerObservation
    from hub_container_executor.daemon_backend import DockerEngineBackend, DockerEngineBinding
    return DockerEngineBinding, DockerEngineBackend, RunnerObservation


class ProbeInputError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error text reflects offending values, which may
        # accidentally contain a credential. Never emit those values.
        raise ProbeInputError('invalid_probe_arguments')


def _identity(value):
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is None:
        raise argparse.ArgumentTypeError('invalid identity')
    return value


def _path(value):
    path = PurePosixPath(value)
    if (not path.is_absolute() or len(value) > 4096 or '..' in path.parts
            or '\\' in value or any(ord(c) < 32 or ord(c) > 126 for c in value)
            or path == PurePosixPath('/')):
        raise argparse.ArgumentTypeError('invalid absolute Linux binding path')
    return value


def _api(value):
    if re.fullmatch(r'1\.(?:0|[1-9][0-9]{0,2})', value) is None or int(value.split('.')[1]) < 41:
        raise argparse.ArgumentTypeError('unsupported explicit API version')
    return value


def _timeout(value):
    if re.fullmatch(r'[1-9][0-9]{0,4}', value) is None or not 100 <= int(value) <= 10000:
        raise argparse.ArgumentTypeError('bounded timeout required')
    return int(value)


def parse_arguments(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    seen = set()
    for item in argv:
        if not isinstance(item, str):
            raise ProbeInputError('invalid_probe_arguments')
        if item.startswith('--'):
            option = item.split('=', 1)[0]
            if option in seen:
                raise ProbeInputError('duplicate_probe_binding')
            seen.add(option)
    parser = _Parser(prog='hub-engine-runner-probe', description=__doc__, allow_abbrev=False)
    parser.add_argument('--runner-id', required=True, type=_identity)
    parser.add_argument('--daemon-id', required=True, type=_identity)
    parser.add_argument('--architecture', required=True, choices=('amd64', 'arm64'))
    parser.add_argument('--variant', choices=('v8',))
    parser.add_argument('--socket', required=True, type=_path)
    parser.add_argument('--api-version', required=True, type=_api)
    parser.add_argument('--generation-root', required=True, type=_path)
    parser.add_argument('--timeout-ms', type=_timeout, default=5000)
    args = parser.parse_args(argv)
    if (PurePosixPath(args.socket) == PurePosixPath(args.generation_root)
            or args.architecture == 'amd64' and args.variant is not None):
        raise ProbeInputError('invalid_probe_binding')
    return args


def main(argv=None):
    try:
        args = parse_arguments(argv)
    except (ProbeInputError, ValueError, TypeError):
        print(json.dumps({'ok': False, 'code': 'invalid_probe_arguments', 'runner_only': True}))
        return 2
    if sys.platform != 'linux':
        print(json.dumps({'ok': False, 'code': 'linux_host_required', 'runner_only': True}))
        return 1
    try:
        DockerEngineBinding, DockerEngineBackend, RunnerObservation = _backend_types()
        platform = Platform(os='linux', architecture=args.architecture, variant=args.variant)
        binding = DockerEngineBinding(args.runner_id, args.daemon_id, platform,
                                      args.socket, args.api_version, args.generation_root)
        backend = DockerEngineBackend(binding)
        try:
            observed = backend.inspect_runner(args.timeout_ms)
            if (type(observed) is not RunnerObservation or observed.runner_id != binding.runner_id
                    or observed.daemon_identity != binding.daemon_identity or observed.platform != platform):
                raise ProbeInputError('runner_evidence_mismatch')
        finally:
            backend.close()
    except Exception:
        # No input strings, daemon bodies, credentials, URLs or traceback.
        print(json.dumps({'ok': False, 'code': 'runner_probe_failed', 'runner_only': True}))
        return 1
    print(json.dumps({'ok': True, 'runner_only': True, 'read_only': True,
                      'runner_identity_verified': True, 'runner_id': observed.runner_id,
                      'daemon_identity': observed.daemon_identity, 'platform': platform.model_dump(),
                      'variant_independently_verified': False,
                      'driver_implementation_verified': False, 'image_verified': False,
                      'profile_verified': False, 'deployment_verified': False,
                      'rollback_verified': False}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
