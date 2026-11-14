"""Fixed issuer-UDS composition of an independently approved manual session."""
import os
import inspect
import threading
from pathlib import Path

from engineering_hub.current_executor_observation import CurrentExecutorObserver
from engineering_hub.startup_reconciliation import ExecutorSessionIdentity
from engineering_hub.trusted_queued_session import (
    TrustedQueuedSessionComposition, TrustedSessionError, ManualSessionEnrollment,
    MAX_REPORT_AGE_MS, _clock, _require, _session_evidence, enrollment_bytes, parse_enrollment,
)
from .coordinator_observation_transport import CoordinatorObservationTransport


def validate_colocated_session(config, enrollment):
    """Validate static binding and freshness; establishes no operator authority."""
    from .installed_coordinator import CoordinatorConfiguration
    _require(type(config) is CoordinatorConfiguration and type(enrollment) is ManualSessionEnrollment)
    e = parse_enrollment(enrollment_bytes(enrollment))
    _session_evidence(e)
    _require(config.manifest.digest() == e.manifest_digest
             and config.manifest.target_id == e.target_id and e.machine_id == 'staging-pc'
             and config.manifest.driver.implementation_commit == e.release.implementation_commit
             and config.manifest.runner_binding.platform == e.release.platform
             and config.manifest.runner_binding.daemon_identity == e.launch.engine_id
             and bool(e.actors) and bool(e.task_ids)
             and set(e.actors) <= config.actors and set(e.task_ids) <= config.allowed_task_ids
             and Path(config.database).is_absolute())
    mono, wall = _clock()
    _require(0 <= wall - e.observed_at_ms <= MAX_REPORT_AGE_MS
             and wall < e.expires_at_ms <= e.launch.approval_expires_at_ms)
    return e, mono, wall


def require_core_compatibility():
    """Reject cores missing the required read-only reader API before loading session material."""
    from engineering_hub.unified_pilot_startup import read_protected_file
    try:
        parameter = inspect.signature(read_protected_file).parameters['require_readonly']
        _require(parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is False)
    except (KeyError, TypeError, ValueError):
        raise TrustedSessionError() from None


class ColocatedTrustedSession(TrustedQueuedSessionComposition):
    """Same report/challenge checks as the unified selector, via pinned issuer UDS."""
    def __init__(self, *, config, enrollment, credential):
        self._pid, self._closed = os.getpid(), False
        self._clock_lock = threading.Lock()
        self._observer = self._audit = self._gate = self._broker = self._owner = self._target = None
        self._transport = None
        try:
            self._enrollment, mono, wall = validate_colocated_session(config, enrollment)
            e = self._enrollment
            self._database = Path(config.database)
            _require(self._database.is_absolute())
            self._manifest = e.manifest_digest
            self._identity = ExecutorSessionIdentity(e.launch.launch_id, e.release.image_config_id)
            self._last_mono, self._last_wall = mono, wall
            self._deadline = mono + (e.expires_at_ms - wall) / 1000
            self._transport = CoordinatorObservationTransport(socket_path=config.executor_socket, credential=credential)
            self._observer = CurrentExecutorObserver(release=e.release, launch=e.launch, transport=self._transport)
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise TrustedSessionError() from None
            raise

    def close(self):
        try:
            super().close()
        finally:
            if self._transport is not None and os.getpid() == self._pid:
                self._transport.close()
