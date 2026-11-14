"""Real signed observer and local ledgers; no actual host enrollment."""
from dataclasses import replace
from uuid import UUID, uuid4, uuid5
import time
import pytest

pytest.importorskip('fcntl', reason='Linux local owner required')
from engineering_hub.current_executor_observation import CurrentExecutorObserver, CurrentExecutorObservationError
from hub_container_executor.adapter import EffectCallContext, _Held
from hub_container_executor.effect_mac_key import EffectMacKey, create_for_new_journal
from hub_container_executor.guarded_bound_runtime import build_guarded_bound_runtime
from hub_container_executor.operation_journal import SQLiteOperationJournal
from tests.test_hub_current_executor_observation import setup, Signer
from tests.test_hub_container_effect_binding_canonical import fixture
from tests.test_hub_container_executor_adapter import make_fixture, command
from tests.test_hub_container_executor_daemon_backend import create_transcript


def test_signed_guard_checks_original_run_and_fence_then_denies_changed_generation(tmp_path, monkeypatch, setup):
    clock, release, launch, signer, old_observer = setup
    root = tmp_path / 'adapter'
    root.mkdir()
    adapter, _, _, manifest = make_fixture(root)
    engine_root = tmp_path / 'engine'
    engine_root.mkdir()
    backend, _, _, _, _ = create_transcript(engine_root)
    journal_root = tmp_path / 'journal'
    journal_root.mkdir(mode=0o700)
    path = journal_root / 'journal.sqlite'
    key_id = create_for_new_journal(journal_root, path.name)
    journal = SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True)
    key = EffectMacKey(journal_root, path.name, expected_key_id=key_id)
    try:
        adapter.backend = backend
        backend.operation_journal = journal
        request = command(manifest, 'deploy_container')
        assert adapter.ledger.admit(request).accepted
        release = replace(release, implementation_commit=manifest.driver.implementation_commit,
                          platform=backend.binding.platform)
        launch = replace(launch, engine_id=backend.binding.daemon_identity)
        signer = Signer(release, launch, signer.key, clock)
        observer = CurrentExecutorObserver(release=release, launch=launch, transport=signer)
        try:
            effect = replace(fixture(), owner_epoch=journal._owner.epoch,
                             run_id=request.run_id, fence=request.fencing_generation)
            runtime = build_guarded_bound_runtime(adapter, key, (effect,), observer=observer,
                                                  release=release, launch=launch)
            context = EffectCallContext(request, str(uuid4()), 'state_copy')
            assert runtime.guard.verify(context) is True
            changed = request.model_copy(update={'fencing_generation': request.fencing_generation + 1})
            assert runtime.guard.verify(EffectCallContext(changed, context.operation_id, 'state_copy')) is False
            signer.before = lambda: adapter.set_paused(True)
            assert runtime.guard.verify(context) is False
            signer.before = lambda: None
            adapter.set_paused(False)
            signer.mutate = lambda payload: payload.__setitem__('engine_pid', launch.engine_pid + 1)
            with pytest.raises(CurrentExecutorObservationError):
                runtime.guard.verify(context)
        finally:
            observer.close()
    finally:
        key.close()
        journal.close()
        backend.close()
        adapter.close()
        old_observer.close()


def test_composed_signed_guard_pause_after_name_lookup_leaves_pending_without_post(tmp_path, monkeypatch, setup):
    clock, release, launch, signer, old_observer = setup
    adapter_root, engine_root, journal_root = (tmp_path / n for n in ('adapter', 'engine', 'journal'))
    adapter_root.mkdir(); engine_root.mkdir(); journal_root.mkdir(mode=0o700)
    adapter, _, _, manifest = make_fixture(adapter_root)
    backend, _, image, plan, requests = create_transcript(engine_root)
    path = journal_root / 'journal.sqlite'
    key_id = create_for_new_journal(journal_root, path.name)
    journal = SQLiteOperationJournal(path, create=True, allow_ephemeral_test_storage=True)
    key = EffectMacKey(journal_root, path.name, expected_key_id=key_id)
    try:
        adapter.backend = backend
        backend.operation_journal = journal
        journal.enable_bindings()
        request = command(manifest, 'deploy_container')
        assert adapter.ledger.admit(request).accepted
        operation = str(uuid5(UUID(request.command_id), 'create_candidate'))
        profile = plan.profile
        release = replace(release, implementation_commit=manifest.driver.implementation_commit,
                          platform=backend.binding.platform)
        launch = replace(launch, engine_id=backend.binding.daemon_identity)
        signer = Signer(release, launch, signer.key, clock)
        observer = CurrentExecutorObserver(release=release, launch=launch, transport=signer)
        try:
            effect = replace(fixture(), operation_id=operation, run_id=request.run_id,
                fence=request.fencing_generation, owner_epoch=journal._owner.epoch,
                boot_id=launch.boot_id, daemon_pid=launch.engine_pid, daemon_start_ticks=launch.engine_start_ticks,
                resource_name=plan.resource_name, image_index_digest=image.runnable_reference,
                platform_manifest_digest=image.platform_manifest_digest, image_config_id=image.image_config_id,
                state_generation_id=profile.state_generation_id,
                environment_names=tuple(n for n, _ in profile.environment),
                memory_bytes=profile.memory_bytes, cpu_millicores=profile.cpu_millicores,
                pids_limit=profile.pids_limit, tmpfs_bytes=profile.tmpfs[0][1])
            runtime = build_guarded_bound_runtime(adapter, key, (effect,), observer=observer,
                                                  release=release, launch=launch)
            backend.effect_binding_provider, adapter.effect_context_guard = runtime.provider, runtime.guard
            original = backend._response_bytes
            def pause_after_lookup(method, route, *args, **kwargs):
                result = original(method, route, *args, **kwargs)
                if method == 'GET' and plan.resource_name in route:
                    adapter.set_paused(True)
                return result
            monkeypatch.setattr(backend, '_response_bytes', pause_after_lookup)
            with pytest.raises(_Held):
                adapter._effect(request, 'create_candidate',
                    lambda op, timeout: backend.create_container(plan, op, timeout),
                    5000, time.monotonic() + 5, lambda cid: {'container_id': cid})
            assert journal.inspect_record(operation).state == 'pending'
            assert not any(method == 'POST' for method, _ in requests)
            assert adapter.ledger.is_paused()
            assert len(signer.calls) >= 2
        finally:
            observer.close()
    finally:
        key.close()
        journal.close()
        backend.close()
        adapter.close()
        old_observer.close()
