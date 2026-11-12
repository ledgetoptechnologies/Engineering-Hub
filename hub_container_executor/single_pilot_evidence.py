"""Single-pilot local cross-checks, not independent daemon attestation.

Requires externally enforced private Engine and exclusive enrolled owner; stores
are not one atomic snapshot and hostile same-UID writers are out of scope.
"""
import time
import uuid
from .backend import BaselineIdentity, StateGeneration
from .daemon_backend import DockerEngineBackend, TrustedEffectBindingProvider
from .operation_journal import SQLiteOperationJournal
from .state_provider import FrozenSQLiteStateProvisioner
from .runtime_profile import probe_set_digest


def require_resources(backend, state):
    if (type(backend) is not DockerEngineBackend or type(backend.operation_journal) is not SQLiteOperationJournal
            or type(state) is not FrozenSQLiteStateProvisioner):
        raise ValueError('single pilot requires exact private providers')


def require_journal_ready(adapter):
    """Allow v2 only with an explicit guard, bound transport and receipt check."""
    backend = adapter.backend
    provider = getattr(backend, 'effect_binding_provider', None)
    if provider is None:
        backend.operation_journal.require_no_pending()
        return False
    from .adapter import TrustedEffectContextGuard
    guard = getattr(adapter, 'effect_context_guard', None)
    if (type(backend) is not DockerEngineBackend or type(provider) is not TrustedEffectBindingProvider
            or type(guard) is not TrustedEffectContextGuard
            or getattr(guard.scope, '__self__', None) is not backend
            or getattr(guard.scope, '__func__', None) is not DockerEngineBackend.effect_context_scope
            or not all(callable(x) for x in (provider.compare_actual, provider.recheck, provider.verify_completed))):
        raise ValueError('bound single pilot requires explicit guarded admission')
    backend.operation_journal.require_no_pending(allow_bound=True)
    return True


def verify_phase(adapter, request, response, deadline):
    journal = adapter.backend.operation_journal
    bound = require_journal_ready(adapter)
    adapter.state_provisioner.require_complete_reservations(deadline_monotonic=deadline)
    if response.replayed or response.model_dump(include=set(type(request).model_fields)) != request.model_dump():
        raise ValueError('phase identity mismatch')
    role = 'rollback' if request.action in ('rollback_container', 'smoke_container_rollback') else 'candidate'
    deploy = request.action in ('deploy_container', 'rollback_container')
    expected = ({'state_copy' if role == 'candidate' else 'rollback_state', 'create_'+role, 'start_'+role}
                if deploy else {'probe'})
    rows = adapter.ledger.operation_rows(request.command_id)
    if len(rows) != len(expected) or {row['kind'] for row in rows} != expected:
        raise ValueError('phase operation set mismatch')
    by_kind = {row['kind']:row for row in rows}
    for row in rows:
        if (row['operation_id'] != str(uuid.uuid5(uuid.UUID(request.command_id), row['kind']))
                or row['command_id'] != request.command_id or row['status'] != 'completed'
                or row['receipt_authentic'] is not True):
            raise ValueError('phase ledger evidence unavailable')
    evidence = response.evidence
    target = adapter.ledger.target_snapshot(request.target_id)
    image = adapter.manifest.rollback if role == 'rollback' else adapter.manifest.candidate
    if (not target or target['current_run_id'] != request.run_id or target['fence'] != request.fencing_generation
            or target['container_id'] != evidence.container_id or target['image_config_id'] != evidence.image_config_id
            or evidence.image_config_id != image.image_config_id):
        raise ValueError('phase target mismatch')
    if deploy:
        if (response.state != 'succeeded' or response.reason is not None or evidence.health != 'unknown'
                or evidence.evidence_verified is not True):
            raise ValueError('deploy outcome not verified')
        state_row = by_kind['state_copy' if role == 'candidate' else 'rollback_state']
        receipt = state_row['receipt']
        fields = {'generation_id','previous_generation_id','source_backup_digest','generation_digest','size_bytes','baseline_unchanged'}
        if type(receipt) is not dict or set(receipt) != fields:
            raise ValueError('state receipt mismatch')
        baseline = adapter.baseline_provider.enrolled_baseline(adapter.manifest)
        if type(baseline) is not BaselineIdentity or adapter.baseline_provider.verify_baseline(baseline,adapter.manifest) is not True:
            raise ValueError('baseline unavailable')
        state = StateGeneration(**receipt)
        adapter._validate_generation(state,baseline)
        seen = adapter.state_provisioner.inspect_reservation(state_row['operation_id'],deadline_monotonic=deadline)
        if (seen is None or not seen.complete or seen.run_id != request.run_id or seen.role != role
                or seen.generation_id != state.generation_id or target['generation_id'] != state.generation_id):
            raise ValueError('state reservation mismatch')
        create, start = by_kind['create_'+role], by_kind['start_'+role]
        if create['receipt'] != {'container_id':evidence.container_id} or start['receipt'] != {'container_id':evidence.container_id,'started':True}:
            raise ValueError('container receipt mismatch')
        pairs = ((create,'create_'+role), (start,'start'))
    else:
        if (evidence.evidence_verified is not True or not
                ((response.state == 'succeeded' and response.reason is None and evidence.health == 'healthy')
                 or (response.state == 'failed' and response.reason == 'health_failed' and evidence.health == 'unhealthy'))):
            raise ValueError('probe outcome not verified')
        probe = by_kind['probe']
        probes = adapter.manifest.probes.rollback if role == 'rollback' else adapter.manifest.probes.candidate
        if probe['receipt'] != {'container_id':evidence.container_id,'image_config_id':evidence.image_config_id,
                                'profile_digest':probe_set_digest(probes),'health':evidence.health,'evidence_verified':True}:
            raise ValueError('probe receipt mismatch')
        pairs = ((probe,'probe'),)
    for row, kind in pairs:
        record = journal.inspect_record(row['operation_id'])
        if record is None or record.kind != kind or record.state != 'completed' or record.container_id != evidence.container_id:
            raise ValueError('journal receipt mismatch')
        if bound and adapter.backend.effect_binding_provider.verify_completed(
                row['operation_id'], kind, request, evidence.container_id) is not True:
            raise ValueError('bound journal evidence unavailable')
    if time.monotonic() >= deadline:
        raise ValueError('phase verification timed out')
