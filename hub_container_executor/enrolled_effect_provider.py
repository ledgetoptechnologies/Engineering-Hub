"""Opt-in binding of independently enrolled effects to a private key/journal.

The supplied closed effect records are independent operator inputs, not an
enrollment service, host identity attestation, fencing enforcement or proof of
the full Engine request. Caller must establish those properties before use.
This module never migrates a journal or clears a held operation.
"""
from .daemon_backend import TrustedEffectBindingProvider
from .effect_binding_canonical import NonsecretEffectBinding, canonical_nonsecret_binding
from .effect_mac_key import EffectMacKey
from .operation_journal import OperationBinding, SQLiteOperationJournal


def enrolled_effect_provider(journal, key, effects):
    """Bind an immutable finite independently enrolled set to an active owner.

    This asserts exact IDs and target/name at admission. It does not establish
    that caller-provided nonsecret profile fields match the daemon request.
    No effect is issued here and no migration or credential setup is performed.
    """
    if (type(journal) is not SQLiteOperationJournal or type(key) is not EffectMacKey
            or type(effects) is not tuple or not effects):
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

    return TrustedEffectBindingProvider(create)
