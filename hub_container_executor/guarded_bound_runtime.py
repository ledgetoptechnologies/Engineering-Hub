"""Explicit bound pilot composition using signed current-launch observations.

Inputs are independently supplied operator enrollment, not self-enrollment.
Caller owns all resources and a protected configuration/launch boundary. This
module starts nothing, creates no keys, migrates no database and clears no hold.
Observations, ledger reads and Engine POSTs are separate operations; this is
not a kernel fence against hostile same-UID or privileged mutation.
"""
from dataclasses import dataclass
import os
import time

from engineering_hub.current_executor_observation import (
    CurrentExecutorObserver, CurrentExecutorReleasePins, CurrentLaunchPins,
    VerifiedExecutorObservation,
)
from .adapter import HostAdapter, TrustedEffectContextGuard
from .daemon_backend import DockerEngineBackend
from .effect_mac_key import EffectMacKey
from .enrolled_effect_provider import enrolled_effect_provider
from .operation_journal import SQLiteOperationJournal


@dataclass(frozen=True)
class GuardedBoundRuntime:
    provider: object
    guard: TrustedEffectContextGuard


def build_guarded_bound_runtime(adapter, key, effects, *, observer, release, launch):
    """Build explicit guard/provider using one independently enrolled observer.

    The owner supplies pre-enrolled effects and original launch/release pins.
    No caller-supplied verification callback is accepted. The signed observer
    checks its own nonce, deadline, signature and the full retained pins.
    """
    if (type(adapter) is not HostAdapter or type(adapter.backend) is not DockerEngineBackend
            or type(adapter.backend.operation_journal) is not SQLiteOperationJournal
            or type(key) is not EffectMacKey or type(observer) is not CurrentExecutorObserver
            or type(release) is not CurrentExecutorReleasePins or type(launch) is not CurrentLaunchPins
            or observer._release != release or observer._launch != launch
            or adapter.backend.binding.daemon_identity != launch.engine_id
            or adapter.backend.binding.platform != release.platform
            or adapter.manifest.driver.implementation_commit != release.implementation_commit):
        raise ValueError('independent bound runtime enrollment required')
    ledger = adapter.ledger
    backend = adapter.backend
    journal = backend.operation_journal
    pid = os.getpid()

    def current_request(context):
        if os.getpid() != pid or adapter.ledger is not ledger or backend.operation_journal is not journal:
            return False
        ledger._check_open()
        held = os.fstat(ledger._lock_fd)
        named = os.stat(ledger.lock_path, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            return False
        recorded = ledger.get(context.request.command_id)
        if recorded is None or recorded.model_dump(include=set(type(context.request).model_fields)) != context.request.model_dump():
            return False
        target = ledger.target_snapshot(context.request.target_id)
        return bool(target and target['current_run_id'] == context.request.run_id
                    and target['fence'] == context.request.fencing_generation
                    and target['manifest_digest'] == context.request.manifest_digest
                    and target['state'] == 'running')

    def fresh():
        proof = observer.observe_verified()
        return (type(proof) is VerifiedExecutorObservation and proof.expires_wall_ms is not None
                and time.monotonic() < proof.expires_monotonic
                and int(time.time() * 1000) < proof.expires_wall_ms)

    def guard_verify(context):
        return current_request(context) and fresh() and current_request(context)

    def verify_generation(actual, expected):
        if (actual.engine_binding is not backend.binding or expected.boot_id != launch.boot_id
                or expected.daemon_pid != launch.engine_pid
                or expected.daemon_start_ticks != launch.engine_start_ticks
                or expected.owner_epoch != journal._owner.epoch):
            return False
        return current_request(actual.context) and fresh() and current_request(actual.context)

    provider = enrolled_effect_provider(journal, key, effects,
        verify_generation=verify_generation, source_ledger=ledger)
    return GuardedBoundRuntime(provider, TrustedEffectContextGuard(guard_verify, backend.effect_context_scope))
