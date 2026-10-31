"""Real backend against fake Engine GETs; probe execution is synthetic."""
import copy
import time
from types import SimpleNamespace

import httpx
import pytest

from hub_container_executor import daemon_backend as module
from hub_container_executor.backend import GateHealthObservation, ProbeObservation
from hub_container_executor.daemon_backend import DaemonBackendError, DockerEngineBackend
from hub_container_executor.runtime_profile import probe_set_digest
from tests.test_hub_container_executor_daemon_backend import (
    binding, container_inspect, docker_config, make_profile, oci_archive, safe_generation, verified_image)

CONTAINER = 'c' * 64
OPERATION = '4c96d3f7-19c5-5ff4-a29e-302f6f661009'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    manifest, image, blobs = verified_image()
    generation = safe_generation(tmp_path)
    original = container_inspect(image, make_profile(image), generation, container_id=CONTAINER, status='running')
    original['Image'] = image.image_config_id
    original['State'].update(Running=True, Paused=False, Restarting=False, Dead=False, Pid=4123)
    state = SimpleNamespace(calls=[], inspections=0, fault=None, probes=0, advance=None)
    def handler(request):
        path = request.url.path; state.calls.append((request.method, path))
        if path.endswith('/info'):
            return httpx.Response(200, json={'ID': manifest.runner_binding.daemon_identity,
                                              'OSType': 'linux', 'Architecture': 'amd64'})
        if '/images/' in path and path.endswith('/json'): return httpx.Response(200, json=docker_config(image))
        if '/images/' in path and path.endswith('/get'): return httpx.Response(200, content=oci_archive(image, blobs))
        if path.endswith('/containers/' + CONTAINER + '/json'):
            state.inspections += 1
            value = copy.deepcopy(original)
            if state.fault == 'pid_drift' and state.inspections >= 3: value['State']['Pid'] = 4124
            elif state.fault == 'foreign_image' and state.inspections >= 3: value['Image'] = 'sha256:'+'f'*64
            elif state.fault == 'stopped' and state.inspections >= 3:
                value['State'].update(Status='exited', Running=False)
            elif state.fault == 'bool_pid': value['State']['Pid'] = True
            return httpx.Response(200, json=value)
        raise AssertionError('unexpected Engine request')
    backend = DockerEngineBackend(binding(manifest, tmp_path), test_transport=httpx.MockTransport(handler))
    backend.inspect_image(image, 5000)
    backend._container_images[CONTAINER] = image
    def probe(container_id, probes, operation_id, budgets):
        state.probes += 1
        assert container_id == CONTAINER and operation_id == OPERATION
        assert len(budgets) == len(probes) and all(type(value) is int and value > 0 for value in budgets)
        if state.advance: state.advance()
        return ProbeObservation(CONTAINER, image.image_config_id, probe_set_digest(probes), 'healthy', True)
    monkeypatch.setattr(backend, 'run_compiled_probes', probe)
    try: yield backend, manifest, image, state
    finally: backend.close()


def run(rig, *, total_timeout_ms=5000):
    backend, manifest, _, _ = rig
    probes = manifest.probes.candidate
    return backend.run_gate_health(CONTAINER, probes, OPERATION, tuple(p.timeout_ms for p in probes), total_timeout_ms)


def test_healthy_probe_bracketed_by_matching_real_engine_inspections(rig):
    _, _, image, state = rig
    result = run(rig)
    assert type(result) is GateHealthObservation and result.daemon_pid == 4123
    assert result.probe.container_id == CONTAINER and result.probe.image_config_id == image.image_config_id
    assert result.probe.health == 'healthy' and result.probe.evidence_verified is True
    assert 0 < result.started_at_ms <= result.finished_at_ms and 0 <= result.elapsed_ms < 5000
    assert state.inspections >= 4 and state.probes == 1
    assert all(method == 'GET' for method, _ in state.calls)


@pytest.mark.parametrize('fault', ['pid_drift', 'foreign_image', 'stopped', 'bool_pid'])
def test_ambiguous_or_changed_engine_identity_denied(rig, fault):
    _, _, _, state = rig; state.fault = fault
    with pytest.raises(DaemonBackendError): run(rig)
    assert state.probes == (0 if fault == 'bool_pid' else 1)
    assert all(method == 'GET' for method, _ in state.calls)


@pytest.mark.parametrize('fault', ['deadline', 'wall_regression', 'monotonic_regression'])
def test_clock_changes_or_expired_deadline_never_return_success(rig, monkeypatch, fault):
    _, _, _, state = rig
    clock = SimpleNamespace(wall=1_800_000_000.0, mono=100.0)
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono))
    def advance():
        if fault == 'deadline': clock.mono += 6
        elif fault == 'wall_regression': clock.wall -= 1; clock.mono += .1
        else: clock.mono -= 1
    state.advance = advance
    with pytest.raises(DaemonBackendError): run(rig)
    assert state.probes == 1


def test_probe_timeout_is_not_reinterpreted_as_success(rig, monkeypatch):
    backend, _, _, state = rig
    def timed_out(*_):
        state.probes += 1
        raise DaemonBackendError('synthetic timeout')
    monkeypatch.setattr(backend, 'run_compiled_probes', timed_out)
    with pytest.raises(DaemonBackendError): run(rig)
    assert state.probes == 1 and state.inspections == 2
