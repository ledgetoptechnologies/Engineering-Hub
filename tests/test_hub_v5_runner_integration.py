"""Actual launcher/runner UDS and worker lifecycle, with synthetic host authority.

This deliberately patches the installed guard only in this disposable test. It
does not launch an Engine container, exercise the app bootstrap, or prove host
mount isolation, production approvals, or permission to enable the guard.
"""
import asyncio
import os
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from engineering_hub.executor_launch_profile import ExecutorLaunchProfile
from hub_container_executor import installed_runner, launcher_cli, ledger as ledger_module, service
from hub_container_executor.adapter import HostAdapter
from hub_container_executor import launch_supervisor as launch
from hub_container_executor.runtime_identity import ExecutorReleaseEnrollment
from tests.test_executor_launch_profile import gate_profile
from tests.test_hub_container_executor_installed_runner import configuration, canonical
from tests.test_hub_container_executor_launch_supervisor import short_root, root_journal, synthetic_issuer


@pytest.mark.asyncio
async def test_v5_runner_actual_handoff_and_owned_worker_with_synthetic_host(synthetic_issuer, short_root, tmp_path, monkeypatch):
    if os.geteuid() != 0: pytest.skip('requires isolated root-owned fixture')
    authority, issuer_path, _ = synthetic_issuer
    fixture = short_root / 'hub-pilot-fixture'; fixture.mkdir(mode=0o711)
    gate_dir = fixture / 'gate-control'; gate_dir.mkdir(mode=0o711)
    profile_data = gate_profile(str(fixture))
    profile_data['gate_completion'] = {'candidate_executable_digest': 'sha256:' + 'a' * 64,
        'rollback_executable_digest': 'sha256:' + 'b' * 64,
        'health_contract': 'engineering-hub.health-json-build-commit.v1'}
    profile_data['gate_ledger_channel'] = 'supervisor-listener-v1'
    profile = ExecutorLaunchProfile.model_validate(profile_data)
    previous_release = authority.release
    authority.release = ExecutorReleaseEnrollment(previous_release.issuer_id, previous_release.public_key,
        previous_release.implementation_commit, previous_release.runnable_digest,
        previous_release.platform_manifest_digest, previous_release.image_config_id,
        previous_release.platform, profile.digest())
    authority.profile = profile
    authority._mounts = {item.role: item for item in profile.mounts}
    authority._source_pins = {'gate_channel_directory': launch._identity(os.lstat(gate_dir))}
    authority._ledger_handoff_parent = authority._ledger_handoff_listener = None
    authority._ledger_handoff_directory = authority._ledger_handoff_path_identity = None
    private = short_root / 'journal-private'; private.mkdir(mode=0o700)
    journal = launch._Journal(str(private / 'ledger-handoff.sqlite'), create=True,
                              gate_mode=True, start_mode=True, ledger_mode=True)
    authority._journal = journal
    journal.reserve(authority._launch_id, {'synthetic': True})
    journal.state(authority._launch_id, 'running', authority._record())
    authority._request_ms = 5000
    data = configuration(); data['schema'] = installed_runner.GATE_LEDGER_SCHEMA
    data['gate_socket_directory'] = str(gate_dir)
    data['launch_transport'] = {'socket_path': str(issuer_path), 'expected_issuer_uid': os.getuid(),
                                'expected_issuer_gid': os.getgid()}
    release = data['release_enrollment']
    release.update(issuer_id=authority.release.issuer_id, public_key_hex=authority.release.public_key.hex(),
        implementation_commit=authority.release.implementation_commit, runnable_digest=authority.release.runnable_digest,
        platform_manifest_digest=authority.release.platform_manifest_digest, image_config_id=authority.release.image_config_id,
        launch_profile_digest=profile.digest())
    service_dir = short_root / 'service'; service_dir.mkdir(mode=0o700)
    data['executor_socket_path'] = str(service_dir / 'executor.sock')
    config = installed_runner.parse_configuration(canonical(data))
    authority._handoff_manifest_digest = lambda: config.manifest.digest()
    # Guard remains closed by default; only this test patches the private guard.
    with pytest.raises(installed_runner.InstalledRunnerError):
        installed_runner._require_supported_gate_runtime(config)
    monkeypatch.setattr(installed_runner, '_require_supported_gate_runtime', lambda _: None)
    monkeypatch.setattr(installed_runner, '_process', lambda: None)  # Synthetic PID1/account boundary.
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    original_statvfs = os.statvfs
    def synthetic_readonly(path):
        result = original_statvfs(path)
        if os.fspath(path) == str(gate_dir):
            values = list(result); values[8] |= os.ST_RDONLY
            return os.statvfs_result(values)
        return result
    monkeypatch.setattr(os, 'statvfs', synthetic_readonly)  # Not real mount acceptance.
    class Secrets:
        def __init__(self, *_, **__): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def resolve_test_secret(self, *_): return 'synthetic-token'
    monkeypatch.setattr(installed_runner, 'ImmutableTestSecretResolver', Secrets)
    ledger_dir = short_root / 'adapter-private'; ledger_dir.mkdir(mode=0o700)
    events = []
    @contextmanager
    def resources(*, manifest, driver_identity_verifier, **_):
        adapter = HostAdapter(ledger_dir / 'ledger.sqlite', manifest, record_store=object(), metadata_store=object(),
            provenance_verifier=object(), backend=object(), baseline_provider=object(), state_provisioner=object(),
            secret_resolver=object(), driver_identity_verifier=driver_identity_verifier)
        events.append('adapter-open')
        try: yield adapter
        finally: adapter.close(); events.append('adapter-close')
    monkeypatch.setattr(installed_runner, 'local_host_adapter_resources', resources)
    launcher_stop = SimpleNamespace(value=None)
    worker_ready, gate_query_done = threading.Event(), threading.Event()
    async def owned_service(factory, **kwargs):
        stop = asyncio.Event()
        async def ready(_address):
            events.append('ready'); worker_ready.set()
            assert await asyncio.to_thread(gate_query_done.wait, 3)
            stop.set()
        await service.serve_owned_host_executor_unix(factory, **kwargs, stop_event=stop,
                                                      on_ready=ready, install_signals=False)
        events.append('service-drained')
        launcher_stop.value.request(0, None)
    monkeypatch.setattr(installed_runner, 'serve_owned_host_executor_unix', owned_service)
    # Real launcher scheduling/receipt/observation code, but no Engine launch or
    # actual gate operation; those remain explicit test substitutions.
    authority.launch = lambda: None
    authority.graceful_stop = lambda: events.append('synthetic-stop')
    def synthetic_gate(**kwargs):
        events.append('gate-thread-start')
        deadline = time.monotonic() + 4
        while not authority._gate_ledger_handoff_ready(deadline):
            assert time.monotonic() < deadline
            threading.Event().wait(.01)
        events.append('handoff-ready')
        assert worker_ready.wait(3)
        assert authority.pending_gate_context(role='candidate', manifest_digest=config.manifest.digest(),
                                               timeout_seconds=2) is None
        events.append('signed-pending-query'); gate_query_done.set()
        while not kwargs['stop_requested'](): threading.Event().wait(.01)
        events.append('gate-thread-stop')
    authority.run_gate_operation = synthetic_gate
    monkeypatch.setattr(launcher_cli, '_load_supervisor', lambda: lambda **_: authority)
    monkeypatch.setattr(launcher_cli, '_load_gate_operation', lambda _: SimpleNamespace(
        manifest=config.manifest, image=object(), application_enrollment_file='/synthetic/app',
        controller_key_file='/synthetic/key', check=lambda: None))
    monkeypatch.setattr(launcher_cli, '_preflight_gate_operation', lambda *_: None)
    def handlers(stop): launcher_stop.value = stop; return []
    monkeypatch.setattr(launcher_cli, '_install_handlers', handlers)
    monkeypatch.setattr(launcher_cli, '_restore_handlers', lambda _: None)
    options = SimpleNamespace(held_ranges_file=None, enrollment_file='/synthetic/enrollment',
        signing_key_file='/synthetic/key', journal_file='/synthetic/journal', wait_seconds=10,
        gate_operation_file='/synthetic/gate', gate_timeout_seconds=8)
    results = []
    def run_launcher():
        try: results.append(launcher_cli._execute(options))
        except BaseException as error: results.append(error)
    thread = threading.Thread(target=run_launcher, name='test-v5-launcher'); thread.start()
    try:
        await asyncio.wait_for(installed_runner.run_installed_executor(config), timeout=8)
        await asyncio.to_thread(thread.join, 5)
        assert not thread.is_alive() and results == [(0, 'stopped')]
        assert events.index('adapter-open') < events.index('ready') < events.index('service-drained')
        assert events.index('handoff-ready') < events.index('signed-pending-query') < events.index('service-drained')
        assert events.index('adapter-close') < events.index('service-drained') < events.index('synthetic-stop')
    finally:
        if launcher_stop.value is not None: launcher_stop.value.request(0, None)
        await asyncio.to_thread(thread.join, 6)
        assert not thread.is_alive()
