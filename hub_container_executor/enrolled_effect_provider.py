"""Opt-in binding of independently enrolled effects to a private key/journal.

The supplied closed effect records are independent operator inputs, not an
enrollment service, host identity attestation, fencing enforcement or proof of
the full Engine request. Caller must establish those properties before use.
This module never migrates a journal or clears a held operation.
"""
from dataclasses import dataclass, replace
from uuid import UUID, uuid5

from .daemon_backend import TrustedEffectBindingProvider, compare_enrolled_effect
from .backend import ContainerObservation
from .effect_binding_canonical import NonsecretEffectBinding, canonical_nonsecret_binding
from .effect_mac_key import EffectMacKey
from .effect_binding_verify import verify_retained_binding
from .operation_journal import OperationBinding, SQLiteOperationJournal
from .ledger import SQLiteLedger


@dataclass(frozen=True)
class DeferredTargetEffect:
    """Pre-enrolled target-free intent linked to an authenticated create."""
    template: NonsecretEffectBinding
    create_command_id: str


def enrolled_effect_provider(journal, key, effects, *, verify_generation, source_ledger=None):
    """Bind an immutable finite independently enrolled set to an active owner.

    Compares the actual daemon inputs against independently enrolled fields.
    The required trusted verifier must independently verify current generation,
    writer fencing and enrollment; a callback is not itself host attestation.
    No effect is issued here and no migration or credential setup is performed.
    """
    if (type(journal) is not SQLiteOperationJournal or type(key) is not EffectMacKey
            or type(effects) is not tuple or not effects or not callable(verify_generation)):
        raise TypeError('explicit journal, key and enrolled effects required')
    journal._active()
    if (key._directory / key._journal_name != journal.path
            or key._journal_identity != journal._owner._database_identity):
        raise ValueError('key and journal differ')
    epoch = journal._owner.epoch
    by_id = {}
    deferred = {}
    for item in effects:
        effect = item.template if type(item) is DeferredTargetEffect else item
        if type(effect) is not NonsecretEffectBinding or effect.owner_epoch != epoch:
            raise ValueError('effect enrollment differs from journal owner')
        if effect.operation_id in by_id or effect.operation_id in deferred:
            raise ValueError('duplicate enrolled operation')
        if type(item) is DeferredTargetEffect:
            if (type(source_ledger) is not SQLiteLedger or effect.kind not in {'start', 'stop', 'probe'}
                    or effect.container_id is not None or effect.resource_name is not None
                    or type(item.create_command_id) is not str
                    or str(UUID(item.create_command_id)) != item.create_command_id):
                raise ValueError('independent create receipt enrollment required')
            deferred[effect.operation_id] = item
        else:
            by_id[effect.operation_id] = (effect, canonical_nonsecret_binding(effect))

    def resolve(actual):
        item = deferred.get(actual.context.operation_id)
        if item is None:
            return by_id.get(actual.context.operation_id)
        request = actual.context.request
        observed = actual.observation
        if type(observed) is not ContainerObservation:
            raise ValueError('independent target inspection required')
        original = source_ledger.get(item.create_command_id)
        if (original is None or original.action not in {'deploy_container', 'rollback_container'}
                or original.run_id != request.run_id or original.fencing_generation != request.fencing_generation
                or original.target_id != request.target_id or original.manifest_digest != request.manifest_digest
                or original.policy_revision != request.policy_revision or original.candidate_commit != request.candidate_commit):
            raise ValueError('original create run differs')
        kind = 'create_rollback' if original.action == 'rollback_container' else 'create_candidate'
        create_id = str(uuid5(UUID(item.create_command_id), kind))
        prior = by_id.get(create_id)
        if prior is None or prior[0].kind != kind or prior[0].run_id != request.run_id or prior[0].fence != request.fencing_generation:
            raise ValueError('original create binding unavailable')
        verify_retained_binding(journal, key, prior[0])
        snapshot = journal.inspect_bound_record(create_id)
        rows = [row for row in source_ledger.operation_rows(item.create_command_id) if row['operation_id'] == create_id]
        target = source_ledger.target_snapshot(request.target_id)
        container = observed.container_id
        if (snapshot is None or snapshot[0].state != 'completed' or snapshot[0].container_id != container
                or len(rows) != 1 or rows[0]['kind'] != kind or rows[0]['status'] != 'completed'
                or rows[0]['receipt_authentic'] is not True or rows[0]['receipt'] != {'container_id': container}
                or not target or target['current_run_id'] != request.run_id or target['fence'] != request.fencing_generation
                or target['container_id'] != container or target['image_config_id'] != observed.selected_image.image_config_id
                or target['generation_id'] != observed.state_generation_id):
            raise ValueError('authenticated create and observed target differ')
        effect = replace(item.template, container_id=container)
        resolved = (effect, canonical_nonsecret_binding(effect))
        old = by_id.get(effect.operation_id)
        if old is not None and old != resolved:
            raise ValueError('deferred target changed')
        by_id[effect.operation_id] = resolved
        return resolved

    def create(operation_id, kind, resource_name, target_id):
        journal._active()
        if journal._owner.epoch != epoch:
            raise ValueError('journal owner changed')
        pair = by_id.get(operation_id)
        if pair is None:
            raise ValueError('operation not enrolled')
        effect, canonical = pair
        if (effect.kind != kind or effect.resource_name != resource_name
                or effect.container_id != target_id):
            raise ValueError('effect differs from enrollment')
        return OperationBinding(effect.boot_id, effect.daemon_pid, effect.daemon_start_ticks,
                                effect.resource_name, effect.container_id, key.mac(canonical), effect.fence)

    def compare(actual):
        pair = resolve(actual)
        return pair is not None and compare_enrolled_effect(actual, pair[0], verify_generation)

    def recheck(actual):
        pair = resolve(actual)
        if pair is None:
            return False
        # Pending state and binding must come from the same owner-guarded SQLite
        # snapshot. This does not make the later Engine request atomic with it.
        verify_retained_binding(journal, key, pair[0], require_pending=True)
        # The independent live-generation/fence verification is deliberately
        # last, after key custody and durable intent checks. Its observation and
        # the subsequent socket request are not an atomic host fence.
        return compare_enrolled_effect(actual, pair[0], verify_generation)

    def verify_completed(operation_id, kind, request, container_id):
        pair = by_id.get(operation_id)
        if pair is None:
            return False
        effect, _ = pair
        if (effect.kind != kind or effect.run_id != request.run_id
                or effect.fence != request.fencing_generation
                or effect.owner_epoch != epoch
                or (effect.container_id is not None and effect.container_id != container_id)):
            return False
        verify_retained_binding(journal, key, effect)
        snapshot = journal.inspect_bound_record(operation_id)
        return (snapshot is not None and snapshot[0].state == 'completed'
                and snapshot[0].kind == kind and snapshot[0].container_id == container_id)

    return TrustedEffectBindingProvider(create, compare, recheck, verify_completed)
