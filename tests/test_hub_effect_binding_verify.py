"""Private journal evidence comparison; no host effect or recovery authority."""
from dataclasses import replace
from uuid import uuid4
import pytest

pytest.importorskip('fcntl', reason='Linux private owner required')
from hub_container_executor.effect_binding_canonical import canonical_nonsecret_binding
from hub_container_executor.effect_binding_verify import verify_retained_binding, EffectBindingVerificationError
from hub_container_executor.effect_mac_key import EffectMacKey, create_for_new_journal
from hub_container_executor.operation_journal import OperationBinding, SQLiteOperationJournal
from tests.test_hub_container_effect_binding_canonical import fixture


def test_exact_binding_matches_and_mutations_fail(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'journal.sqlite'
    key_id = create_for_new_journal(root, path.name)
    with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
        key = EffectMacKey(root, path.name, expected_key_id=key_id)
        try:
            expected = replace(fixture(), owner_epoch=journal._owner.epoch)
            journal.enable_bindings()
            binding = OperationBinding(expected.boot_id, expected.daemon_pid, expected.daemon_start_ticks,
                expected.resource_name, expected.container_id, key.mac(canonical_nonsecret_binding(expected)), expected.fence)
            journal.begin_bound(expected.operation_id, expected.kind, binding)
            assert verify_retained_binding(journal, key, expected) is None
            for changed in (replace(expected, operation_id=str(uuid4())), replace(expected, fence=2),
                    replace(expected, daemon_start_ticks=457), replace(expected, run_id=str(uuid4())),
                    replace(expected, image_config_id='sha256:'+'d'*64)):
                with pytest.raises(EffectBindingVerificationError):
                    verify_retained_binding(journal, key, changed)
            assert journal.inspect_record(expected.operation_id).state == 'pending'
            (root / (path.name + '.effect-binding.key')).unlink()
            with pytest.raises(EffectBindingVerificationError):
                verify_retained_binding(journal, key, expected)
        finally:
            key.close()


def test_unbound_legacy_record_is_not_authenticated(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'journal.sqlite'
    key_id = create_for_new_journal(root, path.name)
    with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
        key = EffectMacKey(root, path.name, expected_key_id=key_id)
        try:
            expected = replace(fixture(), owner_epoch=journal._owner.epoch)
            journal.begin(expected.operation_id, expected.kind)
            with pytest.raises(EffectBindingVerificationError):
                verify_retained_binding(journal, key, expected)
        finally:
            key.close()
