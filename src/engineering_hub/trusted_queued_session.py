"""Fixed opt-in manual-session composition; imports and data grant no admission.

Protected operator material is an attestation, not automated inventory proof.
Neither JSON nor executor responses can select code, providers or identity pins.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from .container_pilot_manifest import Platform
from .container_pilot import ContainerPilotTarget
from .pilot_ownership import PilotOwner
from .current_executor_observation import (CurrentExecutorObserver,
    CurrentExecutorReleasePins, CurrentLaunchPins, VerifiedExecutorObservation)
from .current_executor_transport import CurrentExecutorHTTPSTransport
from .queued_session_acceptance import attach_queued_session_acceptance
from .session_acceptance_audit import OwnedSessionAcceptanceAudit
from .startup_reconciliation import (ExecutorSessionIdentity, OperatorReconciliation,
    StartupReconciliation)

SCHEMA = 'engineering-hub.manual-staging-session.v1'
MAX_ENROLLMENT_BYTES = 32 * 1024
MAX_REPORT_LIFETIME_MS = 600_000
MAX_REPORT_AGE_MS = 300_000


class TrustedSessionError(RuntimeError):
    def __init__(self):
        super().__init__('trusted manual staging session unavailable')


def _require(value):
    if not value:
        raise TrustedSessionError()


def _object(value, fields):
    _require(type(value) is dict and set(value) == set(fields))
    return value


def _digest(value):
    return type(value) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', value) is not None


def _clock():
    mono, wall = time.monotonic(), time.time()
    _require(type(mono) in (int, float) and math.isfinite(mono) and mono >= 0
             and type(wall) in (int, float) and math.isfinite(wall) and wall > 0)
    return mono, int(wall * 1000)


@dataclass(frozen=True)
class ManualSessionEnrollment:
    release: CurrentExecutorReleasePins
    launch: CurrentLaunchPins
    operator_actor: str
    target_id: str
    manifest_digest: str
    machine_id: str
    source: str
    observed_at_ms: int
    expires_at_ms: int
    evidence: str
    inventory_reconciled: bool
    writers_excluded: bool
    unresolved_effects_absent: bool


def parse_enrollment(raw):
    """Closed DATA parser. Successful parsing neither proves facts nor accepts."""
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= MAX_ENROLLMENT_BYTES)
        def unique(pairs):
            result = {}
            for name, value in pairs:
                _require(name not in result)
                result[name] = value
            return result
        def reject(_):
            raise TrustedSessionError()
        data = json.loads(raw, object_pairs_hook=unique, parse_float=reject, parse_constant=reject)
        _object(data, {'schema', 'release', 'launch', 'operator_actor', 'report'})
        _require(data['schema'] == SCHEMA)
        release = dict(_object(data['release'],
            (set(CurrentExecutorReleasePins.__dataclass_fields__) - {'public_key'}) | {'public_key_hex'}))
        key = release.pop('public_key_hex')
        _require(type(key) is str and re.fullmatch('[0-9a-f]{64}', key))
        release['public_key'] = bytes.fromhex(key)
        release['platform'] = Platform.model_validate(release['platform'])
        release = CurrentExecutorReleasePins(**release)
        launch = CurrentLaunchPins(**_object(data['launch'], CurrentLaunchPins.__dataclass_fields__))
        actor = data['operator_actor']
        _require(type(actor) is str and re.fullmatch(r'human:cf:[0-9a-f]{64}', actor))
        report = _object(data['report'], {'target_id', 'manifest_digest', 'machine_id', 'source',
            'observed_at_ms', 'expires_at_ms', 'executor', 'evidence', 'evidence_sha256', 'attestations'})
        _require(report['target_id'] == 'engineering-hub:staging' and report['machine_id'] == 'staging-pc'
                 and _digest(report['manifest_digest']) and type(report['source']) is str
                 and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', report['source']))
        binding = _report_executor(release, launch)
        _object(report['executor'], binding)
        _require(all(type(report['executor'][name]) is type(value)
                     and report['executor'][name] == value for name, value in binding.items()))
        observed, expiry = report['observed_at_ms'], report['expires_at_ms']
        _require(type(observed) is int and type(expiry) is int and 0 < observed < expiry
                 and expiry - observed <= MAX_REPORT_LIFETIME_MS)
        evidence = report['evidence']
        _require(type(evidence) is str and '\x00' not in evidence
                 and 0 < len(evidence.encode('utf-8')) <= 6000
                 and _digest(report['evidence_sha256'])
                 and report['evidence_sha256'] == 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest())
        claims = _object(report['attestations'], {
            'inventory_reconciled', 'writers_excluded', 'unresolved_effects_absent'})
        _require(all(value is True for value in claims.values()))
        return ManualSessionEnrollment(release, launch, actor, report['target_id'],
            report['manifest_digest'], report['machine_id'], report['source'], observed, expiry,
            evidence, **claims)
    except Exception:
        raise TrustedSessionError() from None


class TrustedQueuedSessionComposition:
    """One process, exact profile/owner/gate and one manual acceptance challenge.

    The selector must supply the SAME already captured TLS/credential bytes to
    both clients. Caller owns close only AFTER its retained service has drained.
    Do not copy protected enrollment from public notes or remote response claims.
    """
    def __init__(self, *, profile, database, enrollment, ca_pem, credential):
        self._pid, self._closed = os.getpid(), False
        self._observer = self._audit = self._gate = self._broker = self._owner = self._target = None
        try:
            from .unified_pilot_startup import PilotProfile
            _require(type(profile) is PilotProfile and type(enrollment) is ManualSessionEnrollment)
            # Reparse a retained typed enrollment to prevent bypass via direct
            # dataclass construction with malformed/false attestation fields.
            self._enrollment = parse_enrollment(enrollment_bytes(enrollment))
            e = self._enrollment
            _require(profile.manifest.digest() == e.manifest_digest
                     and profile.manifest.target_id == e.target_id and profile.machine_id == e.machine_id
                     and profile.manifest.driver.implementation_commit == e.release.implementation_commit
                     and profile.manifest.runner_binding.platform == e.release.platform
                     and profile.manifest.runner_binding.daemon_identity == e.launch.engine_id)
            self._database = Path(database)
            _require(self._database.is_absolute())
            self._manifest = e.manifest_digest
            self._identity = ExecutorSessionIdentity(e.launch.launch_id, e.release.image_config_id)
            mono, wall = _clock()
            _require(0 <= wall - e.observed_at_ms <= MAX_REPORT_AGE_MS
                     and wall < e.expires_at_ms <= e.launch.approval_expires_at_ms)
            self._last_mono, self._last_wall = mono, wall
            self._deadline = mono + (e.expires_at_ms - wall) / 1000
            transport = CurrentExecutorHTTPSTransport.from_captured_material(profile.origin,
                ca_pem=ca_pem, ca_sha256=profile.ca_sha256,
                credential=credential, credential_sha256=profile.credential_sha256)
            self._observer = CurrentExecutorObserver(release=e.release, launch=e.launch, transport=transport)
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise TrustedSessionError() from None
            raise

    def _fresh(self):
        _require(os.getpid() == self._pid and not self._closed)
        mono, wall = _clock()
        e = self._enrollment
        _require(self._last_mono <= mono < self._deadline
                 and self._last_wall <= wall < e.expires_at_ms
                 and 0 <= wall - e.observed_at_ms <= MAX_REPORT_LIFETIME_MS)
        self._last_mono, self._last_wall = mono, wall
        return mono

    def _current_verified(self):
        self._fresh()
        value = self._observer.observe_verified()
        self._fresh()
        _require(type(value) is VerifiedExecutorObservation)
        # Never refresh the original operator-report ceiling when a newer
        # signed identity response arrives. Gate-owned async preparation runs
        # this bounded synchronous verifier off the shared Hub event loop.
        _require(type(value.verified_wall_ms) is int and type(value.expires_wall_ms) is int
                 and 0 < value.verified_wall_ms < value.expires_wall_ms)
        return replace(value, expires_monotonic=min(value.expires_monotonic, self._deadline),
                       expires_wall_ms=min(value.expires_wall_ms, self._enrollment.expires_at_ms,
                                           self._enrollment.launch.approval_expires_at_ms))

    def reconciliation_factory(self, owner, target):
        try:
            self._fresh()
            _require(self._gate is None and self._owner is None
                     and type(owner) is PilotOwner and type(target) is ContainerPilotTarget
                     and target.target_id == self._enrollment.target_id
                     and target.manifest_digest == self._manifest)
            self._owner = owner
            self._target = target
            lifetime = int(self._deadline - self._fresh())
            _require(lifetime >= 1)
            self._gate = StartupReconciliation(owner=owner, database=self._database,
                target_id=target.target_id, manifest_digest=self._manifest, executor=self._identity,
                lifetime_seconds=lifetime, current_executor_identity=self._current_verified,
                async_admission=True)
            return self._gate
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise TrustedSessionError() from None
            raise

    def session_acceptance_factory(self, core, owner, repo, target, gate):
        try:
            self._fresh()
            _require(owner is self._owner and gate is self._gate and target is self._target
                     and self._broker is None
                     and Path(repo.path).absolute() == self._database.absolute()
                     and repo.pilot_owner is owner and target.manifest_digest == self._manifest
                     and target.target_id == self._enrollment.target_id)
            self._audit = OwnedSessionAcceptanceAudit(repo)
            e = self._enrollment
            # Original source time is preserved and shown; fresh challenge/epoch
            # binding is NOT a new observation of inventory or writer exclusion.
            evidence = (f'Operator-attested source: {e.source}\n'
                f'Original observed_at_ms: {e.observed_at_ms}\nExpires_at_ms: {e.expires_at_ms}\n'
                f'Target: {e.target_id}\nManifest: {e.manifest_digest}\n'
                'Retained report executor: ' + json.dumps(_report_executor(e.release, e.launch),
                    sort_keys=True, separators=(',', ':')) + '\n'
                'Current signed identity is not inventory/writer-exclusion proof.\n\n' + e.evidence)
            report = OperatorReconciliation(gate.challenge, owner.epoch, target.target_id,
                self._manifest, self._identity, 'sha256:' + hashlib.sha256(evidence.encode()).hexdigest(),
                e.inventory_reconciled, e.writers_excluded, e.unresolved_effects_absent)
            lifetime = int(self._deadline - self._fresh())
            _require(lifetime >= 1)
            self._broker = attach_queued_session_acceptance(core, owner=owner, repository=repo,
                target=target, gate=gate, operator_actor=e.operator_actor, audit=self._audit,
                report=report, evidence=evidence, lifetime_seconds=lifetime)
            return self._broker
        except BaseException as error:
            self.close()
            if isinstance(error, Exception):
                raise TrustedSessionError() from None
            raise

    def close(self):
        self._closed = True
        if os.getpid() != self._pid:
            return
        failed = False
        for value in (self._broker, self._gate, self._audit, self._observer):
            if value is not None:
                try:
                    value.close()
                except BaseException:
                    failed = True
        if failed:
            raise TrustedSessionError()


def enrollment_bytes(enrollment):
    """Serialize typed DATA only; never discovers pins or creates enrollment."""
    e = enrollment
    release = {name: getattr(e.release, name) for name in CurrentExecutorReleasePins.__dataclass_fields__
               if name not in {'public_key', 'platform'}}
    release.update(public_key_hex=e.release.public_key.hex(), platform=e.release.platform.model_dump(mode='json'))
    value = {'schema': SCHEMA, 'release': release,
        'launch': {name: getattr(e.launch, name) for name in CurrentLaunchPins.__dataclass_fields__},
        'operator_actor': e.operator_actor, 'report': {
            'target_id': e.target_id, 'manifest_digest': e.manifest_digest, 'machine_id': e.machine_id,
            'source': e.source, 'observed_at_ms': e.observed_at_ms, 'expires_at_ms': e.expires_at_ms,
            'executor': _report_executor(e.release, e.launch),
            'evidence': e.evidence, 'evidence_sha256': 'sha256:' + hashlib.sha256(e.evidence.encode()).hexdigest(),
            'attestations': {name: getattr(e, name) for name in (
                'inventory_reconciled', 'writers_excluded', 'unresolved_effects_absent')}}}
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _report_executor(release, launch):
    return {name: getattr(launch, name) for name in CurrentLaunchPins.__dataclass_fields__} | {
        'image_config_id': release.image_config_id}
