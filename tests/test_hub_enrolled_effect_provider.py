"""Opt-in composition; real local journal/key, synthetic Engine and verifier."""
from dataclasses import replace
from uuid import uuid4
import time
import pytest

pytest.importorskip('fcntl', reason='Linux owner and private key required')
from hub_container_executor.enrolled_effect_provider import enrolled_effect_provider, DeferredTargetEffect
from hub_container_executor.effect_mac_key import EffectMacKey, create_for_new_journal
from hub_container_executor.effect_binding_verify import verify_retained_binding, EffectBindingVerificationError
from hub_container_executor.operation_journal import SQLiteOperationJournal
from hub_container_executor.adapter import TrustedEffectContextGuard, _Held
from hub_container_executor.adapter import EffectCallContext
from hub_container_executor.backend import ActualImageSelection, ContainerObservation
from hub_container_executor.daemon_backend import ActualEffectInputs
from hub_container_executor.ledger import SQLiteLedger
from hub_container_executor.runtime_profile import probe_set_digest
from tests.test_hub_container_effect_binding_canonical import fixture
from tests.test_hub_container_executor_daemon_backend import create_transcript, probe_fixture, run_readiness, make_profile
from tests.test_hub_container_executor_adapter import make_fixture, command


@pytest.mark.parametrize('tamper', ['none', 'receipt', 'binding'])
def test_deferred_start_uses_authenticated_create_and_observed_target(tmp_path, monkeypatch, tamper):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'operations.sqlite'
    key_id = create_for_new_journal(private, path.name)
    backend, manifest, image, plan, _ = create_transcript(tmp_path)
    ledger = SQLiteLedger(private / 'adapter.sqlite', manifest_digest=manifest.digest(),
        candidate_commit=manifest.candidate.source_commit, policy_revision=manifest.policy_revision)
    try:
        with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
            journal.enable_bindings()
            key = EffectMacKey(private, path.name, expected_key_id=key_id)
            try:
                req = command(manifest, 'deploy_container')
                assert ledger.admit(req).accepted
                create_id = ledger.begin_operation(req.command_id, 'create_candidate')
                start_id = str(__import__('uuid').uuid5(__import__('uuid').UUID(req.command_id), 'start_candidate'))
                profile = plan.profile
                create = replace(fixture(), operation_id=create_id, run_id=req.run_id,
                    fence=req.fencing_generation, owner_epoch=journal._owner.epoch,
                    resource_name=plan.resource_name, image_index_digest=image.runnable_reference,
                    platform_manifest_digest=image.platform_manifest_digest, image_config_id=image.image_config_id,
                    state_generation_id=profile.state_generation_id,
                    environment_names=tuple(name for name, _ in profile.environment),
                    memory_bytes=profile.memory_bytes, cpu_millicores=profile.cpu_millicores,
                    pids_limit=profile.pids_limit, tmpfs_bytes=profile.tmpfs[0][1])
                from hub_container_executor.effect_binding_canonical import canonical_nonsecret_binding
                from hub_container_executor.operation_journal import OperationBinding
                journal.begin_bound(create_id, 'create_candidate', OperationBinding(create.boot_id,
                    create.daemon_pid, create.daemon_start_ticks, create.resource_name, None,
                    key.mac(canonical_nonsecret_binding(create)), create.fence))
                journal.completed(create_id, 'create_candidate', 'c' * 64)
                ledger.complete_operation(create_id, {'container_id': 'c' * 64})
                ledger.record_target_instance(req.command_id, 'c' * 64, image.image_config_id, profile.state_generation_id)
                if tamper == 'receipt':
                    with ledger._transaction() as db:
                        db.execute('UPDATE operations SET receipt=? WHERE operation_id=?', ('{"container_id":"' + 'd'*64 + '"}', create_id))
                elif tamper == 'binding':
                    with journal._transaction() as db:
                        db.execute('UPDATE private_operation_binding SET request_mac=? WHERE operation_id=?',
                                   ('hmac-sha256:' + '0'*64, create_id))
                start = replace(create, operation_id=start_id, kind='start', resource_name=None, container_id=None)
                provider = enrolled_effect_provider(journal, key, (create, DeferredTargetEffect(start, req.command_id)),
                    verify_generation=lambda *_: True, source_ledger=ledger)
                selection = ActualImageSelection(image.runnable_reference, image.platform_manifest_digest, image.image_config_id, image.platform)
                obs = ContainerObservation('c' * 64, selection, profile, profile.state_generation_id, 'created')
                actual = ActualEffectInputs(EffectCallContext(req, start_id, 'start_candidate'), backend.binding, None, obs, image, None)
                if tamper != 'none':
                    with pytest.raises((ValueError, EffectBindingVerificationError)):
                        provider.compare_actual(actual)
                    assert journal.inspect_record(start_id) is None
                    return
                assert provider.compare_actual(actual) is True
                bound = provider.create(start_id, 'start', None, 'c' * 64)
                journal.begin_bound(start_id, 'start', bound)
                assert provider.recheck(actual) is True
                monkeypatch.setattr(ledger, 'target_snapshot', lambda _: {'current_run_id': req.run_id,
                    'fence': req.fencing_generation, 'container_id': 'd' * 64,
                    'image_config_id': image.image_config_id, 'generation_id': profile.state_generation_id})
                with pytest.raises(ValueError): provider.recheck(actual)
                assert journal.inspect_record(start_id).state == 'pending'
            finally:
                key.close()
    finally:
        ledger.close()
        backend.close()


@pytest.mark.parametrize('kind', ['create_candidate', 'probe'])
@pytest.mark.parametrize('failure', ['none', 'profile', 'context', 'generation', 'probe',
                                     'revoked_after_journal', 'revoked_on_name', 'revoked_after_probe_create'])
def test_adapter_through_bound_journal_to_fake_engine(tmp_path, monkeypatch, kind, failure):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'operations.sqlite'
    key_id = create_for_new_journal(private, path.name)
    engine_root = tmp_path / 'engine'
    engine_root.mkdir()
    if kind == 'probe':
        backend, manifest, image, requests = probe_fixture(engine_root)
        profile = make_profile(image)
        plan = None
    else:
        backend, manifest, image, plan, requests = create_transcript(engine_root)
        profile = plan.profile
    adapter_root = tmp_path / 'adapter'
    adapter_root.mkdir()
    adapter, _, _, _ = make_fixture(adapter_root)
    operation = '4c96d3f7-19c5-5ff4-a29e-302f6f661009'
    request = command(manifest, 'smoke_container' if kind == 'probe' else 'deploy_container')
    effect = replace(fixture(), operation_id=operation, run_id=request.run_id,
        fence=request.fencing_generation, kind=kind,
        resource_name=plan.resource_name if plan else None,
        container_id=None if plan else 'c' * 64,
        image_index_digest=image.runnable_reference,
        platform_manifest_digest=image.platform_manifest_digest, image_config_id=image.image_config_id,
        state_generation_id=profile.state_generation_id,
        environment_names=tuple(name for name, _ in profile.environment),
        memory_bytes=profile.memory_bytes, cpu_millicores=profile.cpu_millicores,
        pids_limit=profile.pids_limit, tmpfs_bytes=profile.tmpfs[0][1],
        probe_set_digest=probe_set_digest(manifest.probes.candidate) if kind == 'probe' else None)
    if failure == 'profile': effect = replace(effect, memory_bytes=effect.memory_bytes + 1)
    if failure == 'context': effect = replace(effect, run_id=str(uuid4()))
    if failure == 'probe' and kind == 'probe': effect = replace(effect, probe_set_digest='sha256:' + 'a' * 64)
    if failure == 'probe' and kind != 'probe': effect = replace(effect, fence=effect.fence + 1)
    try:
        with SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True) as journal:
            journal.enable_bindings()
            key = EffectMacKey(private, path.name, expected_key_id=key_id)
            try:
                effect = replace(effect, owner_epoch=journal._owner.epoch)
                backend.operation_journal = journal
                live = {'verified': True}
                backend.effect_binding_provider = enrolled_effect_provider(journal, key, (effect,),
                    verify_generation=lambda actual, expected: failure != 'generation' and live['verified'])
                adapter.effect_context_guard = TrustedEffectContextGuard(lambda _: True, backend.effect_context_scope)
                monkeypatch.setattr(adapter.ledger, 'begin_operation', lambda *_: operation)
                monkeypatch.setattr(adapter.ledger, 'complete_operation', lambda *_: None)
                original_begin = journal.begin_bound
                def begin_then_revoke(*args):
                    original_begin(*args)
                    if failure == 'revoked_after_journal': live['verified'] = False
                monkeypatch.setattr(journal, 'begin_bound', begin_then_revoke)
                original_bytes = backend._response_bytes
                def read_then_revoke(method, route, *args, **kwargs):
                    result = original_bytes(method, route, *args, **kwargs)
                    if failure == 'revoked_on_name' and plan and method == 'GET' and plan.resource_name in route:
                        live['verified'] = False
                    return result
                monkeypatch.setattr(backend, '_response_bytes', read_then_revoke)
                original = backend._read_response
                def check_before_effect(method, route, *args, **kwargs):
                    if method == 'POST':
                        assert journal.inspect_record(operation).state == 'pending'
                        verify_retained_binding(journal, key, effect)
                    result = original(method, route, *args, **kwargs)
                    if failure == 'revoked_after_probe_create' and kind == 'probe' and method == 'POST' and route.endswith('/exec'):
                        live['verified'] = False
                    return result
                monkeypatch.setattr(backend, '_read_response', check_before_effect)
                before = sum(item[0] == 'POST' for item in requests)
                callback = (lambda op, timeout: run_readiness(backend, manifest.probes.candidate)) if kind == 'probe' else (lambda op, timeout: backend.create_container(plan, op, timeout))
                # Name lookup is create-specific; exec creation is probe-specific.
                succeeds = (failure == 'none' or failure == 'revoked_on_name' and kind == 'probe'
                            or failure == 'revoked_after_probe_create' and kind != 'probe')
                if succeeds:
                    adapter._effect(request, kind, callback, 30000, time.monotonic() + 30, lambda _: {})
                    assert journal.inspect_record(operation).state == 'completed'
                    assert backend.effect_binding_provider.verify_completed(operation, kind, request, 'c' * 64)
                    assert not backend.effect_binding_provider.verify_completed(operation, kind,
                        request.model_copy(update={'fencing_generation': request.fencing_generation + 1}), 'c' * 64)
                    assert not backend.effect_binding_provider.verify_completed(operation, kind, request, 'd' * 64)
                    assert sum(item[0] == 'POST' for item in requests) > before
                else:
                    with pytest.raises(_Held):
                        adapter._effect(request, kind, callback, 30000, time.monotonic() + 30, lambda _: {})
                    row = journal.inspect_record(operation)
                    post_admission = failure in {'revoked_after_journal', 'revoked_on_name', 'revoked_after_probe_create'}
                    assert (row is not None and row.state == 'pending') if post_admission else row is None
                    expected_posts = 1 if failure == 'revoked_after_probe_create' and kind == 'probe' else 0
                    assert sum(item[0] == 'POST' for item in requests) == before + expected_posts
            finally:
                key.close()
    finally:
        backend.close()
        adapter.close()


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
                options = {'verify_generation': lambda *_: True}
                with pytest.raises(ValueError): enrolled_effect_provider(journal, key, (effect, effect), **options)
                with pytest.raises(ValueError): enrolled_effect_provider(journal, key, (replace(effect, owner_epoch=str(uuid4())),), **options)
                with pytest.raises(ValueError): enrolled_effect_provider(journal, other_key, (effect,), **options)
            finally:
                key.close()
                other_key.close()
