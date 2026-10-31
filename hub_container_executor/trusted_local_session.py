"""Trusted manual staging session using the explicit private executor socket.

The caller supplies independently protected operator enrollment and the same
captured dedicated credential used by the command client. No enrollment or
operator acceptance is inferred from a socket response.
"""
import os
import hashlib
import hmac
from pathlib import Path

from engineering_hub.current_executor_observation import CurrentExecutorObserver
from engineering_hub.current_executor_unix_transport import CurrentExecutorUnixTransport
from engineering_hub.executor_launch_profile import verify_profile_bytes
from engineering_hub.startup_reconciliation import ExecutorSessionIdentity
from engineering_hub.trusted_queued_session import (TrustedQueuedSessionComposition,
    ManualSessionEnrollment, TrustedSessionError, SCHEMA_V2, MAX_REPORT_AGE_MS,
    _clock, _require, enrollment_bytes, parse_enrollment)
from .installed_coordinator import CoordinatorConfiguration, run_installed_coordinator
from .local_providers import ImmutableTestSecretResolver


class TrustedLocalQueuedSession(TrustedQueuedSessionComposition):
    """Local counterpart to the HTTPS composition; explicit owner closes it."""
    def __init__(self, *, config, enrollment, credential):
        self._pid, self._closed = os.getpid(), False
        self._observer = self._audit = self._gate = self._broker = self._owner = self._target = None
        self._transport = None
        try:
            _require(type(config) is CoordinatorConfiguration and config.process_id == self._pid
                     and type(enrollment) is ManualSessionEnrollment)
            expected = [digest for slot, version, digest in config.bindings
                        if (slot, version) == config.roles['executor']]
            _require(type(credential) is bytes and len(expected) == 1
                     and hmac.compare_digest('sha256:' + hashlib.sha256(credential).hexdigest(), expected[0]))
            self._enrollment = e = parse_enrollment(enrollment_bytes(enrollment))
            _require(e.schema == SCHEMA_V2 and config.manifest.digest() == e.manifest_digest
                     and config.manifest.target_id == e.target_id and e.machine_id == 'staging-pc'
                     and config.manifest.driver.implementation_commit == e.release.implementation_commit
                     and config.manifest.runner_binding.platform == e.release.platform
                     and config.manifest.runner_binding.daemon_identity == e.launch.engine_id)
            if e.schema == SCHEMA_V2:
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

    def close(self):
        failed = False
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
