"""Real signed observer and local ledgers; no actual host enrollment."""
from dataclasses import replace
from uuid import uuid4
import pytest

pytest.importorskip('fcntl', reason='Linux local owner required')
from engineering_hub.current_executor_observation import CurrentExecutorObserver, CurrentExecutorObservationError
from hub_container_executor.adapter import EffectCallContext
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
