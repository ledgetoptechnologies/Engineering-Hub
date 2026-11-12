"""Compare retained journal evidence against independently supplied effect data.

This is an offline comparison, not host attestation, a fence, or permission to
execute, replay, adopt, stop, or release anything. The journal snapshot and key
check are not atomic together. The caller must supply the complete binding
from an independently trusted source, not reconstruct it from the stored row.
"""
import hmac

from .effect_binding_canonical import NonsecretEffectBinding, canonical_nonsecret_binding
from .effect_mac_key import EffectMacKey
from .operation_journal import SQLiteOperationJournal


class EffectBindingVerificationError(ValueError):
    """Sanitized rejection of unavailable or mismatched evidence."""


def verify_retained_binding(journal, key, expected):
    """Return no authority; raise unless stored identities and MAC match exactly."""
    try:
        if (type(journal) is not SQLiteOperationJournal or type(key) is not EffectMacKey
                or type(expected) is not NonsecretEffectBinding):
            raise ValueError
        if (key._directory / key._journal_name != journal.path
                or key._journal_identity != journal._owner._database_identity):
            raise ValueError
        canonical = canonical_nonsecret_binding(expected)
        snapshot = journal.inspect_bound_record(expected.operation_id)
        if snapshot is None:
            raise ValueError
        record, stored = snapshot
        if (record.operation_id != expected.operation_id or record.kind != expected.kind
                or stored.boot_id != expected.boot_id or stored.daemon_pid != expected.daemon_pid
                or stored.daemon_start != expected.daemon_start_ticks
                or stored.owner_epoch != expected.owner_epoch or stored.fence != expected.fence
                or stored.resource_name != expected.resource_name or stored.target_id != expected.container_id):
            raise ValueError
        if not hmac.compare_digest(stored.request_mac, key.mac(canonical)):
            raise ValueError
    except Exception:
        raise EffectBindingVerificationError('Retained effect binding unavailable or mismatched') from None
