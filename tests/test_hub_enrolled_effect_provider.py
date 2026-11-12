"""Explicitly enrolled synthetic binding through the real backend and journal."""
from dataclasses import replace
from uuid import uuid4
import pytest

pytest.importorskip('fcntl', reason='Linux owner and private key required')
from hub_container_executor.enrolled_effect_provider import enrolled_effect_provider
from hub_container_executor.effect_mac_key import EffectMacKey, create_for_new_journal
from hub_container_executor.effect_binding_verify import verify_retained_binding
from hub_container_executor.operation_journal import SQLiteOperationJournal
from hub_container_executor.daemon_backend import DaemonBackendError
from tests.test_hub_container_effect_binding_canonical import fixture
from tests.test_hub_container_executor_daemon_backend import create_transcript


def test_enrolled_create_reaches_engine_only_after_authenticated_intent(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'operations.sqlite'
    key_id = create_for_new_journal(private, path.name)
    backend, _, _, plan, requests = create_transcript(tmp_path)
    try:
        with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
            journal.enable_bindings()
            key = EffectMacKey(private, path.name, expected_key_id=key_id)
            try:
                effect = replace(fixture(), operation_id=str(uuid4()), owner_epoch=journal._owner.epoch,
                                 resource_name=plan.resource_name)
                backend.operation_journal = journal
                backend.effect_binding_provider = enrolled_effect_provider(journal, key, (effect,))
                original = backend._read_response
                def observed(method, route, *args, **kwargs):
                    if method == 'POST' and route.endswith('/containers/create'):
                        assert journal.inspect_record(effect.operation_id).state == 'pending'
                        verify_retained_binding(journal, key, effect)
                    return original(method, route, *args, **kwargs)
                monkeypatch.setattr(backend, '_read_response', observed)
                assert backend.create_container(plan, effect.operation_id, 5000) == 'c' * 64
                assert journal.inspect_record(effect.operation_id).state == 'completed'
                before = list(requests)
                with pytest.raises(DaemonBackendError):
                    backend.create_container(plan, str(uuid4()), 5000)
                assert sum(method == 'POST' for method, _ in requests) == sum(method == 'POST' for method, _ in before)
            finally:
                key.close()
    finally:
        backend.close()


def test_enrollment_rejects_duplicate_wrong_owner_and_key_pair(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path, other = root / 'a.sqlite', root / 'b.sqlite'
    key_id = create_for_new_journal(root, path.name)
    other_id = create_for_new_journal(root, other.name)
    with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
        with SQLiteOperationJournal(other, create=True, allow_ephemeral_test_storage=True):
            key = EffectMacKey(root, path.name, expected_key_id=key_id)
            other_key = EffectMacKey(root, other.name, expected_key_id=other_id)
            try:
                effect = replace(fixture(), owner_epoch=journal._owner.epoch)
                with pytest.raises(ValueError): enrolled_effect_provider(journal, key, (effect, effect))
                with pytest.raises(ValueError): enrolled_effect_provider(journal, key, (replace(effect, owner_epoch=str(uuid4())),))
                with pytest.raises(ValueError): enrolled_effect_provider(journal, other_key, (effect,))
            finally:
                key.close()
                other_key.close()
