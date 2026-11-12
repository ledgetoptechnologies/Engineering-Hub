"""Opt-in binding of independently enrolled effects to a private key/journal.

The supplied closed effect records are independent operator inputs, not an
enrollment service, host identity attestation, fencing enforcement or proof of
the full Engine request. Caller must establish those properties before use.
This module never migrates a journal or clears a held operation.
"""
from .daemon_backend import TrustedEffectBindingProvider, compare_enrolled_effect
from .effect_binding_canonical import NonsecretEffectBinding, canonical_nonsecret_binding
from .effect_mac_key import EffectMacKey
from .effect_binding_verify import verify_retained_binding
from .operation_journal import OperationBinding, SQLiteOperationJournal


def enrolled_effect_provider(journal, key, effects, *, verify_generation):
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
    for effect in effects:
        if type(effect) is not NonsecretEffectBinding or effect.owner_epoch != epoch:
            raise ValueError('effect enrollment differs from journal owner')
        canonical = canonical_nonsecret_binding(effect)
        if effect.operation_id in by_id:
            raise ValueError('duplicate enrolled operation')
        by_id[effect.operation_id] = (effect, canonical)

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
        pair = by_id.get(actual.context.operation_id)
        return pair is not None and compare_enrolled_effect(actual, pair[0], verify_generation)

    def recheck(actual):
        pair = by_id.get(actual.context.operation_id)
        if pair is None:
            return False
        record = journal.inspect_record(actual.context.operation_id)
        if record is None or record.state != 'pending':
            return False
        verify_retained_binding(journal, key, pair[0])
        # The independent live-generation/fence verification is deliberately
        # last, after key custody and durable intent checks. Its observation and
        # the subsequent socket request are not an atomic host fence.
        return compare_enrolled_effect(actual, pair[0], verify_generation)

    return TrustedEffectBindingProvider(create, compare, recheck)
