"""Private journal evidence comparison; no host effect or recovery authority."""
from dataclasses import replace
import os
import sqlite3
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


def test_verifier_reads_record_and_binding_in_one_snapshot(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'journal.sqlite'
    key_id = create_for_new_journal(root, path.name)
    with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
        key = EffectMacKey(root, path.name, expected_key_id=key_id)
        try:
            expected = replace(fixture(), owner_epoch=journal._owner.epoch)
            journal.enable_bindings()
            journal.begin_bound(expected.operation_id, expected.kind, OperationBinding(
                expected.boot_id, expected.daemon_pid, expected.daemon_start_ticks,
                expected.resource_name, expected.container_id,
                key.mac(canonical_nonsecret_binding(expected)), expected.fence))
            def reject_split_read(*args):
                raise AssertionError('split journal read')
            monkeypatch.setattr(journal, 'inspect_record', reject_split_read)
            monkeypatch.setattr(journal, 'inspect_binding', reject_split_read)
            assert verify_retained_binding(journal, key, expected) is None
        finally:
            key.close()


def test_pending_required_uses_bound_snapshot_after_interleaved_completion(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'journal.sqlite'
    key_id = create_for_new_journal(root, path.name)
    with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
        key = EffectMacKey(root, path.name, expected_key_id=key_id)
        try:
            expected = replace(fixture(), owner_epoch=journal._owner.epoch)
            journal.enable_bindings()
            journal.begin_bound(expected.operation_id, expected.kind, OperationBinding(
                expected.boot_id, expected.daemon_pid, expected.daemon_start_ticks,
                expected.resource_name, expected.container_id,
                key.mac(canonical_nonsecret_binding(expected)), expected.fence))
            original = journal.inspect_bound_record
            def complete_before_snapshot(operation_id):
                journal.completed(operation_id, expected.kind, 'c' * 64)
                return original(operation_id)
            monkeypatch.setattr(journal, 'inspect_bound_record', complete_before_snapshot)
            with pytest.raises(EffectBindingVerificationError):
                verify_retained_binding(journal, key, expected, require_pending=True)
            assert journal.inspect_record(expected.operation_id).state == 'completed'
        finally:
            key.close()


def test_replaced_key_file_and_closed_handles_fail_closed(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'journal.sqlite'
    key_id = create_for_new_journal(root, path.name)
    journal = SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True)
    key = EffectMacKey(root, path.name, expected_key_id=key_id)
    expected = replace(fixture(), owner_epoch=journal._owner.epoch)
    journal.enable_bindings()
    journal.begin_bound(expected.operation_id, expected.kind, OperationBinding(
        expected.boot_id, expected.daemon_pid, expected.daemon_start_ticks,
        expected.resource_name, expected.container_id,
        key.mac(canonical_nonsecret_binding(expected)), expected.fence))
    key_path = root / (path.name + '.effect-binding.key')
    replacement = root / 'replacement'
    replacement.write_bytes(os.urandom(32))
    replacement.chmod(0o600)
    os.replace(replacement, key_path)
    with pytest.raises(EffectBindingVerificationError):
        verify_retained_binding(journal, key, expected)
    key.close()
    with pytest.raises(EffectBindingVerificationError):
        verify_retained_binding(journal, key, expected)
    journal.close()
    with pytest.raises(EffectBindingVerificationError):
        verify_retained_binding(journal, key, expected)


def test_key_from_another_journal_is_rejected(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'journal.sqlite'
    other_path = root / 'other.sqlite'
    create_for_new_journal(root, path.name)
    other_key_id = create_for_new_journal(root, other_path.name)
    with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
        with SQLiteOperationJournal(other_path, create=True, allow_ephemeral_test_storage=True):
            key = EffectMacKey(root, other_path.name, expected_key_id=other_key_id)
            try:
                with pytest.raises(EffectBindingVerificationError):
                    verify_retained_binding(journal, key, replace(fixture(), owner_epoch=journal._owner.epoch))
            finally:
                key.close()


def test_closed_journal_is_rejected_with_key_still_open(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'journal.sqlite'
    key_id = create_for_new_journal(root, path.name)
    journal = SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True)
    key = EffectMacKey(root, path.name, expected_key_id=key_id)
    try:
        expected = replace(fixture(), owner_epoch=journal._owner.epoch)
        journal.enable_bindings()
        journal.begin_bound(expected.operation_id, expected.kind, OperationBinding(
            expected.boot_id, expected.daemon_pid, expected.daemon_start_ticks,
            expected.resource_name, expected.container_id,
            key.mac(canonical_nonsecret_binding(expected)), expected.fence))
        journal.close()
        with pytest.raises(EffectBindingVerificationError):
            verify_retained_binding(journal, key, expected)
    finally:
        key.close()


def test_create_receipt_is_not_authenticated_by_binding_mac(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'journal.sqlite'
    key_id = create_for_new_journal(root, path.name)
    with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
        key = EffectMacKey(root, path.name, expected_key_id=key_id)
        try:
            expected = replace(fixture(), owner_epoch=journal._owner.epoch)
            assert expected.kind == 'create_candidate'
            journal.enable_bindings()
            journal.begin_bound(expected.operation_id, expected.kind, OperationBinding(
                expected.boot_id, expected.daemon_pid, expected.daemon_start_ticks,
                expected.resource_name, expected.container_id,
                key.mac(canonical_nonsecret_binding(expected)), expected.fence))
            journal.completed(expected.operation_id, expected.kind, 'a' * 64)
            with sqlite3.connect(path) as db:
                db.execute('UPDATE private_operation_journal SET container_id=? WHERE operation_id=?',
                           ('b' * 64, expected.operation_id))
            assert journal.inspect_record(expected.operation_id).container_id == 'b' * 64
            # The MAC covers the intent projection, not the completion receipt.
            assert verify_retained_binding(journal, key, expected) is None
        finally:
            key.close()
