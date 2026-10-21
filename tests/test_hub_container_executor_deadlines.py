"""Deadline boundary regressions using explicitly fake backend/state providers."""
import pytest
import hub_container_executor.adapter as adapter_module
from tests.test_hub_container_executor_adapter import make_fixture, command


def test_readiness_policy_requires_full_budget_without_starving_later_probes(tmp_path, monkeypatch):
    adapter, backend, _state, manifest = make_fixture(tmp_path, startup_grace_ms=1000)
    clock = [100.0]
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: clock[0])
    try:
        assert adapter._probe_timeouts(manifest.probes.candidate, 104.0) == (1000, 1000, 1000)
        with pytest.raises(adapter_module._Held):
            adapter._probe_timeouts(manifest.probes.candidate, 103.999)
        assert 'probe' not in backend.events
    finally:
        adapter.close()


def test_zero_grace_retains_legacy_greedy_budget_allocation(tmp_path, monkeypatch):
    adapter, _backend, _state, manifest = make_fixture(tmp_path)
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: 100.0)
    try:
        assert adapter._probe_timeouts(manifest.probes.candidate, 101.5) == (1000, 499, 1)
    finally:
        adapter.close()


def test_smoke_refuses_insufficient_readiness_budget_without_new_probe_or_lifecycle(tmp_path, monkeypatch):
    adapter, backend, _state, manifest = make_fixture(tmp_path, startup_grace_ms=1000)
    clock = [100.0]
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: clock[0])
    deployed = adapter.submit(command(manifest, 'deploy_container'))
    assert deployed.state == 'succeeded'
    original = backend.inspect_container
    def delayed_inspection(*args):
        observation = original(*args)
        clock[0] += 27.0  # 3s remains, below explicit4s readiness sequence.
        return observation
    monkeypatch.setattr(backend, 'inspect_container', delayed_inspection)
    before = {kind: backend.events.count(kind) for kind in ('probe', 'create', 'start', 'stop')}
    try:
        result = adapter.submit(command(manifest, 'smoke_container', run_id=deployed.run_id))
        assert result.state == 'held' and result.reason == 'inspect_required'
        assert {kind: backend.events.count(kind) for kind in before} == before
        rows = adapter.ledger.operation_rows(result.command_id)
        assert rows[-1]['kind'] == 'probe' and rows[-1]['status'] == 'intent'
    finally:
        adapter.close()


def test_expired_durable_intent_does_not_start_effect_callback(tmp_path, monkeypatch):
    adapter, backend, state, manifest = make_fixture(tmp_path, command_budget_ms=300)
    clock = [100.0]
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: clock[0])
    original = adapter.ledger.begin_operation

    def delayed_admission(*args, **kwargs):
        operation_id = original(*args, **kwargs)
        clock[0] += 1.0
        return operation_id

    monkeypatch.setattr(adapter.ledger, 'begin_operation', delayed_admission)
    try:
        response = adapter.submit(command(manifest, 'deploy_container'))
        assert response.state == 'held' and response.reason == 'inspect_required'
        assert state.calls == [], 'expired admission must not invoke the state provider'
        assert 'create' not in backend.events
        rows = adapter.ledger.operation_rows(response.command_id)
        assert len(rows) == 1 and rows[0]['kind'] == 'state_copy' and rows[0]['status'] == 'intent'
    finally:
        adapter.close()


def test_final_container_inspection_overrun_cannot_report_success(tmp_path, monkeypatch):
    adapter, backend, _state, manifest = make_fixture(tmp_path, command_budget_ms=1000)
    clock = [100.0]
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: clock[0])
    original = backend.inspect_container
    late = []

    def final_inspection(container_id, timeout_ms):
        observation = original(container_id, timeout_ms)
        if container_id == 'c' * 64 and observation.status == 'running':
            clock[0] += 2.0
            late.append(container_id)
        return observation

    monkeypatch.setattr(backend, 'inspect_container', final_inspection)
    try:
        response = adapter.submit(command(manifest, 'deploy_container'))
        assert late, 'fixture must exercise the final running-container inspection'
        assert response.state == 'held' and response.reason == 'inspect_required'
        assert adapter.get(response.command_id).state == 'held'
    finally:
        adapter.close()


@pytest.mark.parametrize('action', ['deploy_container', 'smoke_container'])
def test_success_is_rechecked_after_evidence_preparation(tmp_path, monkeypatch, action):
    adapter, backend, _state, manifest = make_fixture(tmp_path, command_budget_ms=1000)
    clock = [100.0]
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: clock[0])
    run_id = None
    if action == 'smoke_container':
        deployed = adapter.submit(command(manifest, 'deploy_container'))
        assert deployed.state == 'succeeded'
        run_id = deployed.run_id
        backend.probe_health = ['healthy']
    original = adapter._evidence

    def late_evidence(*args):
        result = original(*args)
        clock[0] += 2.0
        return result

    monkeypatch.setattr(adapter, '_evidence', late_evidence)
    try:
        response = adapter.submit(command(manifest, action, run_id=run_id))
        assert response.state == 'held' and response.reason == 'inspect_required'
        assert adapter.get(response.command_id).state == 'held'
    finally:
        adapter.close()


@pytest.mark.parametrize('proof_source', ['driver', 'baseline-verifier', 'baseline-provider'])
def test_rejected_coroutine_proofs_are_closed_before_cleanup(tmp_path, monkeypatch, proof_source):
    adapter, backend, state, manifest = make_fixture(tmp_path)
    pending = []
    ran = []

    async def denied():
        ran.append(True)
        return False

    def asynchronous_proof(*_args):
        result = denied()
        pending.append(result)
        return result

    if proof_source == 'driver':
        monkeypatch.setattr(adapter.driver_identity_verifier, 'implementation_commit', asynchronous_proof)
    elif proof_source == 'baseline-verifier':
        monkeypatch.setattr(adapter.baseline_provider, 'verify_baseline', asynchronous_proof)
    else:
        monkeypatch.setattr(adapter.baseline_provider, 'enrolled_baseline', asynchronous_proof)
    try:
        response = adapter.submit(command(manifest, 'deploy_container'))
        assert response.state == 'held'
        assert state.calls == [] and 'create' not in backend.events
        assert pending and all(coroutine.cr_frame is None for coroutine in pending)
        assert ran == [], 'a rejected coroutine proof must not execute'
    finally:
        for coroutine in pending:
            coroutine.close()
        adapter.close()
