"""Trusted manual staging session using the explicit private executor socket.

The caller supplies independently protected operator enrollment and the same
captured dedicated credential used by the command client. No enrollment or
operator acceptance is inferred from a socket response.
"""
import os
import argparse
import asyncio
import hashlib
import hmac
from pathlib import Path
import sys

from engineering_hub.current_executor_observation import CurrentExecutorObserver
from engineering_hub.current_executor_unix_transport import CurrentExecutorUnixTransport
from engineering_hub.executor_launch_profile import verify_profile_bytes
from engineering_hub.startup_reconciliation import ExecutorSessionIdentity
from engineering_hub.trusted_queued_session import (TrustedQueuedSessionComposition,
    ManualSessionEnrollment, TrustedSessionError, SCHEMA_V2, SCHEMA_V3, MAX_REPORT_AGE_MS, MAX_ENROLLMENT_BYTES,
    _clock, _require, enrollment_bytes, parse_enrollment)
from .installed_coordinator import CoordinatorConfiguration, load_configuration, run_installed_coordinator
from .local_providers import ImmutableTestSecretResolver
from engineering_hub.unified_pilot_startup import read_protected_file


class TrustedLocalQueuedSession(TrustedQueuedSessionComposition):
    """Local counterpart to the HTTPS composition; explicit owner closes it."""
    def __init__(self, *, config, enrollment, credential):
        self._pid, self._closed = os.getpid(), False
        self._observer = self._audit = self._gate = self._broker = self._owner = self._target = None
        self._transport = None
        try:
            _require(type(config) is CoordinatorConfiguration and config.process_id == self._pid
                     and type(enrollment) is ManualSessionEnrollment)
            self._config = config
            expected = [digest for slot, version, digest in config.bindings
                        if (slot, version) == config.roles['executor']]
            _require(type(credential) is bytes and len(expected) == 1
                     and hmac.compare_digest('sha256:' + hashlib.sha256(credential).hexdigest(), expected[0]))
            self.__credential = credential
            self._enrollment = e = parse_enrollment(enrollment_bytes(enrollment))
            _require(e.schema == (SCHEMA_V3 if config.auth.human_auth_mode == 'github' else SCHEMA_V2)
                     and (e.schema != SCHEMA_V3 or e.operator_actor == f'human:{config.auth.beau_github_id}')
                     and config.manifest.digest() == e.manifest_digest
                     and config.manifest.target_id == e.target_id and e.machine_id == 'staging-pc'
                     and config.manifest.driver.implementation_commit == e.release.implementation_commit
                     and config.manifest.runner_binding.platform == e.release.platform
                     and config.manifest.runner_binding.daemon_identity == e.launch.engine_id)
            if e.schema in (SCHEMA_V2, SCHEMA_V3):
                profile = verify_profile_bytes(e.profile_bytes, e.release.launch_profile_digest)
                _require(profile.broker_mode is None and profile.role is None and profile.command_channel is None
                         and profile.daemon_policy is not None and profile.daemon_policy.engine_id == e.launch.engine_id)
            self._database = Path(config.database)
            _require(self._database.is_absolute())
            self._manifest = e.manifest_digest
            self._identity = ExecutorSessionIdentity(e.launch.launch_id, e.release.image_config_id)
            mono, wall = _clock()
            _require(0 <= wall - e.observed_at_ms <= MAX_REPORT_AGE_MS
                     and wall < e.expires_at_ms <= e.launch.approval_expires_at_ms)
            self._last_mono, self._last_wall = mono, wall
            self._deadline = mono + (e.expires_at_ms - wall) / 1000
            self._transport = CurrentExecutorUnixTransport(config.executor_socket, credential=credential)
            self._observer = CurrentExecutorObserver(release=e.release, launch=e.launch, transport=self._transport)
        except BaseException as error:
            self.close()
            if isinstance(error, Exception): raise TrustedSessionError() from None
            raise

    def executor_client_factory(self):
        """Use the same enrolled socket and credential as the signed observer."""
        self._fresh()
        from engineering_hub.container_pilot_client import ContainerPilotClient
        return ContainerPilotClient.for_private_unix(self._config.executor_socket,
                                                       self.__credential.decode('ascii'))

    def close(self):
        failed = False
        self.__credential = b''
        try:
            super().close()
        except BaseException:
            failed = True
        if os.getpid() == self._pid and self._transport is not None:
            try: self._transport.close()
            except BaseException: failed = True
            self._transport = None
        if failed: raise TrustedSessionError()


async def serve_trusted_local_coordinator(config, *, enrollment):
    """Compose one local session under explicit dedicated-VM host trust.

    The operator must ensure exclusive authority to mutate the executor socket
    directory and prohibit executor/socket replacement throughout this call.
    The separate HTTPX command connection is not bound to the observer's inode;
    the checks here cannot establish or enforce those host prerequisites.
    The caller independently captures the operator enrollment.
    """
    session = None
    try:
        _require(type(config) is CoordinatorConfiguration and type(enrollment) is ManualSessionEnrollment)
        enrolled = parse_enrollment(enrollment_bytes(enrollment))
        _require(enrolled.schema == (SCHEMA_V3 if config.auth.human_auth_mode == 'github' else SCHEMA_V2)
                 and enrolled.machine_id == 'staging-pc'
                 and enrolled.target_id == config.manifest.target_id
                 and enrolled.manifest_digest == config.manifest.digest()
                 and (enrolled.schema != SCHEMA_V3
                      or enrolled.operator_actor == f'human:{config.auth.beau_github_id}'))
        with ImmutableTestSecretResolver(config.secret_root, bindings=config.bindings) as resolver:
            token = resolver.resolve_test_secret(*config.roles['executor'])
        _require(type(token) is str and token.isascii())
        session = TrustedLocalQueuedSession(config=config, enrollment=enrollment, credential=token.encode('ascii'))
        await run_installed_coordinator(config, reconciliation_factory=session.reconciliation_factory,
                                        session_acceptance_factory=session.session_acceptance_factory)
    except Exception:
        raise TrustedSessionError() from None
    finally:
        if session is not None:
            session.close()


def load_local_enrollment(path, *, config):
    """Capture pre-existing operator attestation; protected bytes grant no approval.

    The leaf is current-UID 0600 under a trusted private 0700 parent. The
    independent operator is responsible for the file's provenance and facts.
    """
    try:
        _require(type(config) is CoordinatorConfiguration and config.process_id == os.getpid()
                 and type(path) is str and path.startswith('/') and os.path.normpath(path) == path
                 and not path.startswith('//'))
        source = Path(path)
        # Enrollment cannot be inside the writable database or secret trees.
        for root in (Path(config.database).parent, Path(config.secret_root)):
            _require(source.parent != root and root not in source.parents and source not in root.parents)
        raw = read_protected_file(source, maximum_bytes=MAX_ENROLLMENT_BYTES)
        result = parse_enrollment(raw)
        _require(result.schema == (SCHEMA_V3 if config.auth.human_auth_mode == 'github' else SCHEMA_V2)
                 and (result.schema != SCHEMA_V3
                      or result.operator_actor == f'human:{config.auth.beau_github_id}'))
        return result
    except Exception:
        raise TrustedSessionError() from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise TrustedSessionError()


def main(argv=None):
    """Start only an explicitly selected, independently enrolled local session."""
    try:
        parser = _Parser(description='Explicit trusted local staging coordinator', allow_abbrev=False)
        parser.add_argument('--config', required=True, action='append')
        parser.add_argument('--enrollment', required=True, action='append')
        args = parser.parse_args(argv)
        _require(len(args.config) == len(args.enrollment) == 1)
        config = load_configuration(args.config[0])
        enrollment = load_local_enrollment(args.enrollment[0], config=config)
        asyncio.run(serve_trusted_local_coordinator(config, enrollment=enrollment))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('Trusted local staging coordinator unavailable', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
