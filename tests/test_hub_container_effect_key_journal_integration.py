"""Opt-in key/journal local integration; no backend, effects, or trust enrollment."""
import os
import sys
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Actual Linux owner and local filesystem required")
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux private key custody required")

from hub_container_executor.effect_mac_key import EffectMacKey, EffectMacKeyError, create_for_new_journal
from hub_container_executor.operation_journal import (
    OperationBinding, OperationJournalConflict, OperationJournalError, SQLiteOperationJournal,
)


def test_key_then_journal_bound_pending_reopens_without_replay(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = root / "operations.sqlite"
    identity, boot_id = str(uuid4()), str(uuid4())
    canonical = b'{"operation":"synthetic-nonsecret-fixture"}'

    # Bootstrap order is intentional; no trusted enrollment is implemented here.
    key_id = create_for_new_journal(root, path.name)
    with pytest.raises(EffectMacKeyError):
        EffectMacKey(root, path.name, expected_key_id=key_id)
    with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
        holder = EffectMacKey(root, path.name, expected_key_id=key_id)
        try:
            journal.enable_bindings()
            with pytest.raises(OperationJournalError):
                journal.begin(str(uuid4()), "create_candidate")
            mac = holder.mac(canonical)
            binding = OperationBinding(boot_id, 123, 456,
                "hub-pilot-demo-0123456789ab-candidate", None, mac, 1)
            journal.begin_bound(identity, "create_candidate", binding)
            first_epoch = journal.inspect_binding(identity).owner_epoch
            assert journal.inspect(identity).state == "pending"
        finally:
            holder.close()

    with SQLiteOperationJournal(path, create=False, allow_ephemeral_test_storage=True) as journal:
        holder = EffectMacKey(root, path.name, expected_key_id=key_id)
        try:
            assert holder.mac(canonical) == mac
            stored = journal.inspect_binding(identity)
            assert stored.request_mac == mac and stored.owner_epoch == first_epoch
            assert journal.inspect(identity).state == "pending"
            with pytest.raises(OperationJournalError):
                journal.begin(identity, "create_candidate")
            with pytest.raises(OperationJournalConflict):
                journal.begin_bound(identity, "create_candidate", binding)
        finally:
            holder.close()


def test_replaced_or_missing_key_never_regenerated_for_existing_journal(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = root / "operations.sqlite"
    key_id = create_for_new_journal(root, path.name)
    with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True):
        pass
    key = root / (path.name + ".effect-binding.key")
    key.unlink()
    with pytest.raises(EffectMacKeyError):
        EffectMacKey(root, path.name, expected_key_id=key_id)
    with pytest.raises(EffectMacKeyError):
        create_for_new_journal(root, path.name)
    assert not key.exists()
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    with pytest.raises(EffectMacKeyError):
        EffectMacKey(root, path.name, expected_key_id=key_id)
