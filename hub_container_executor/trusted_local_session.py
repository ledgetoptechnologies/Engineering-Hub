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

_SUPERVISED_ADMISSION = '/operator-admission/admission.json'
_SUPERVISED_CONFIG = '/operator/coordinator.json'
_SUPERVISED_SESSION = '/operator-evidence/session.json'
_SUPERVISED_ISSUER = '/run/engineering-hub/coordinator-issuer.sock'


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


async def serve_trusted_local_coordinator(config, *, enrollment, admission_verifier=None, github_client=None, oauth_binding=None):
    """Compose one local session under explicit dedicated-VM host trust.

    The operator must ensure exclusive authority to mutate the executor socket
    directory and prohibit executor/socket replacement throughout this call.
    The separate HTTPX command connection is not bound to the observer's inode;
    the checks here cannot establish or enforce those host prerequisites.
    The caller independently captures the operator enrollment.
    """
    session = None
    try:
        if admission_verifier is not None:
            from .runtime_identity import CoordinatorLaunchReceiptIdentityVerifier
            _require(type(admission_verifier) is CoordinatorLaunchReceiptIdentityVerifier)
            admission_verifier.implementation_commit()
        _require(type(config) is CoordinatorConfiguration and type(enrollment) is ManualSessionEnrollment)
        enrolled = parse_enrollment(enrollment_bytes(enrollment))
        _require(enrolled.schema == (SCHEMA_V3 if config.auth.human_auth_mode == 'github' else SCHEMA_V2)
                 and enrolled.machine_id == 'staging-pc'
                 and enrolled.target_id == config.manifest.target_id
                 and enrolled.manifest_digest == config.manifest.digest()
                 and (enrolled.schema != SCHEMA_V3
                      or enrolled.operator_actor == f'human:{config.auth.beau_github_id}'))
        if github_client is not None or oauth_binding is not None:
            from engineering_hub.github_oauth_broker import validate_broker_binding
            digests=[digest for slot,version,digest in config.bindings if (slot,version)==config.roles.get('github')]
            _require(admission_verifier is not None and config.auth.human_auth_mode=='github' and len(digests)==1)
            validate_broker_binding(github_client,oauth_binding,client_id=config.auth.github_client_id,
                                    origin=config.auth.origin,secret_digest=digests[0])
        with ImmutableTestSecretResolver(config.secret_root, bindings=config.bindings) as resolver:
            token = resolver.resolve_test_secret(*config.roles['executor'])
        if admission_verifier is not None: admission_verifier.implementation_commit()
        _require(type(token) is str and token.isascii())
        session = TrustedLocalQueuedSession(config=config, enrollment=enrollment, credential=token.encode('ascii'))
        options = {} if admission_verifier is None else {'admission_verifier': admission_verifier}
        if github_client is not None or oauth_binding is not None:
            options.update(github_client=github_client,oauth_binding=oauth_binding)
        return await run_installed_coordinator(config, reconciliation_factory=session.reconciliation_factory,
                                        session_acceptance_factory=session.session_acceptance_factory, **options)
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


def _read_supervised_input(path, maximum):
    """Capture one private regular file on a read-only mount, without following links."""
    from engineering_hub.private_browser_socket import _trusted_ancestor
    from engineering_hub.unified_pilot_startup import _file_identity
    parent = fd = None
    try:
        source = Path(path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        parent = os.open('/', flags)
        _trusted_ancestor(os.fstat(parent))
        for component in source.parent.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=parent)
            try:
                opened = os.fstat(next_fd)
                _trusted_ancestor(opened)
                named = os.stat(component, dir_fd=parent, follow_symlinks=False)
                _require((opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino))
            except BaseException:
                os.close(next_fd)
                raise
            os.close(parent)
            parent = next_fd
        parent_info = os.fstat(parent)
        fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        before = _file_identity(os.fstat(fd), private=True)
        _require(0 < before[2] <= maximum and os.fstatvfs(fd).f_flag & os.ST_RDONLY)
        _require(_file_identity(os.stat(source.name, dir_fd=parent, follow_symlinks=False), private=True) == before)
        raw = bytearray()
        while len(raw) <= maximum:
            part = os.read(fd, min(65536, maximum + 1 - len(raw)))
            if not part: break
            raw.extend(part)
        _require(len(raw) == before[2] and _file_identity(os.fstat(fd), private=True) == before
                 and _file_identity(os.stat(source.name, dir_fd=parent, follow_symlinks=False), private=True) == before
                 and os.fstatvfs(fd).f_flag & os.ST_RDONLY)
        named_parent = os.stat(source.parent, follow_symlinks=False)
        _trusted_ancestor(named_parent)
        _require(source.parent.resolve(strict=True) == source.parent
                 and (parent_info.st_dev, parent_info.st_ino) == (named_parent.st_dev, named_parent.st_ino))
        return bytes(raw)
    finally:
        try:
            if fd is not None: os.close(fd)
        finally:
            if parent is not None: os.close(parent)


def run_supervised_local_coordinator():
    """Authenticate this coordinator before reading its config or credentials.

    The launch supervisor must independently enforce the signed profile and
    exact raw admission input. A receipt is not a lease or socket-custody proof.
    """
    from .coordinator_admission import MAX_BYTES, parse_admission
    from .launch_transport import CoordinatorUnixLaunchReceiptTransport
    from .runtime_identity import CoordinatorLaunchReceiptIdentityVerifier
    from .installed_coordinator import MAX_CONFIG_BYTES, parse_configuration, _process
    admission = parse_admission(_read_supervised_input(_SUPERVISED_ADMISSION, MAX_BYTES))
    transport = verifier = github_client = None
    try:
        transport = CoordinatorUnixLaunchReceiptTransport(socket_path=_SUPERVISED_ISSUER,
            expected_issuer_uid=admission.issuer_uid, expected_issuer_gid=admission.issuer_gid)
        verifier = CoordinatorLaunchReceiptIdentityVerifier(enrollment=admission.release,
            issuer_transport=transport, max_receipt_lifetime_seconds=admission.lifetime_seconds,
            exchange_timeout_ms=admission.timeout_ms)
        verifier.implementation_commit()
        binding = verifier.launch_binding()
        config_raw = _read_supervised_input(_SUPERVISED_CONFIG, MAX_CONFIG_BYTES)
        session_raw = _read_supervised_input(_SUPERVISED_SESSION, MAX_ENROLLMENT_BYTES)
        _require(hmac.compare_digest('sha256:' + hashlib.sha256(config_raw).hexdigest(), admission.config_digest)
                 and hmac.compare_digest('sha256:' + hashlib.sha256(session_raw).hexdigest(), admission.session_digest))
        _process()
        config = parse_configuration(config_raw)
        if admission.oauth_broker is not None:
            from engineering_hub.github_oauth_broker import GitHubOAuthUnixClient
            oauth=admission.oauth_broker
            digests=[digest for slot,version,digest in config.bindings if (slot,version)==config.roles.get('github')]
            _require(config.auth.human_auth_mode=='github' and len(digests)==1
                     and oauth.client_id==config.auth.github_client_id and oauth.origin==config.auth.origin
                     and oauth.secret_digest==digests[0])
            github_client=GitHubOAuthUnixClient(socket_path=oauth.socket_path,issuer_uid=oauth.issuer_uid,issuer_gid=oauth.issuer_gid)
        config_path, session_path = Path(_SUPERVISED_CONFIG), Path(_SUPERVISED_SESSION)
        _require(config_path.parent != Path(config.database).parent
                 and Path(config.database).parent not in config_path.parents)
        for root in (Path(config.database).parent, Path(config.secret_root)):
            _require(session_path.parent != root and root not in session_path.parents and session_path not in root.parents)
        enrollment = parse_enrollment(session_raw)
        _require(enrollment.schema == (SCHEMA_V3 if config.auth.human_auth_mode == 'github' else SCHEMA_V2)
                 and (enrollment.schema != SCHEMA_V3
                      or enrollment.operator_actor == f'human:{config.auth.beau_github_id}'))
        _require(_read_supervised_input(_SUPERVISED_CONFIG, MAX_CONFIG_BYTES) == config_raw
                 and _read_supervised_input(_SUPERVISED_SESSION, MAX_ENROLLMENT_BYTES) == session_raw)
        verifier.implementation_commit()
        options={} if github_client is None else {'github_client':github_client,'oauth_binding':admission.oauth_broker}
        result = asyncio.run(serve_trusted_local_coordinator(config, enrollment=enrollment, admission_verifier=verifier, **options))
        from .coordinator_drain import LocalCoordinatorDrain, report_local_drain
        _require(type(result) is LocalCoordinatorDrain and result.manifest_digest == config.manifest.digest())
        report_local_drain(socket_path=_SUPERVISED_ISSUER, issuer_uid=admission.issuer_uid,
            issuer_gid=admission.issuer_gid, release=admission.release, binding=binding,
            owner_epoch=result.owner_epoch, config_digest=admission.config_digest, session_digest=admission.session_digest,
            timeout_ms=admission.timeout_ms)
    finally:
        try:
            if github_client is not None: github_client.close()
        finally:
            try:
                if verifier is not None: verifier.close()
            finally:
                if transport is not None: transport.close()


def main(argv=None):
    """Start only an explicitly selected, independently enrolled local session."""
    try:
        parser = _Parser(description='Explicit trusted local staging coordinator', allow_abbrev=False)
        parser.add_argument('--config', action='append')
        parser.add_argument('--enrollment', action='append')
        parser.add_argument('--supervised', action='store_true')
        args = parser.parse_args(argv)
        if args.supervised:
            _require(args.config is None and args.enrollment is None)
            run_supervised_local_coordinator()
            return 0
        _require(args.config is not None and args.enrollment is not None)
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
