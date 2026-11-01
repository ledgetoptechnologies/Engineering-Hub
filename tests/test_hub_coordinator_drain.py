"""Actual peer-authenticated local drain report, synthetic retained process."""
import os
import threading
import time
from types import SimpleNamespace
from uuid import uuid4
import pytest

pytest.importorskip('fcntl')
from hub_container_executor import coordinator_drain as drain, launch_supervisor as launch, runtime_identity
from tests.test_hub_container_executor_launch_supervisor import synthetic_issuer, short_root, root_journal, current_ready


@pytest.mark.parametrize('wrong_digest', [False, True])
@pytest.mark.parametrize('role', ['supervised-local-coordinator-v2', 'supervised-local-coordinator-v3'])
def test_local_drain_is_peer_bound_durable_and_one_shot(synthetic_issuer, wrong_digest, role):
    authority, path, checks = synthetic_issuer
    authority.profile = SimpleNamespace(role=role)
    authority.release = runtime_identity.CoordinatorReleaseEnrollment(**{
        name:getattr(authority.release,name) for name in runtime_identity.CoordinatorReleaseEnrollment.__dataclass_fields__})
    authority._coordinator_admission = SimpleNamespace(config_digest='sha256:'+'a'*64, session_digest='sha256:'+'b'*64, timeout_ms=2000)
    authority._journal.reserve_issuance(authority._launch_id, 'a'*64)
    authority._journal.issuance_state(authority._launch_id, 'sent', 'b'*64)
    authority._journal.state(authority._launch_id, 'term_reserved', authority._record())
    current_ready(authority); authority._coordinator_stop_requested = True
    results = []
    def serve():
        try: authority._serve_one_coordinator_drain(time.monotonic()+3); results.append('served')
        except BaseException as error: results.append(error)
    thread = threading.Thread(target=serve); thread.start()
    binding = (authority._launch_id, authority._consumer.boot, authority._consumer.pid,
               authority._consumer.start, authority._container_id)
    try:
        options = dict(socket_path=str(path), issuer_uid=os.geteuid(), issuer_gid=os.getegid(),
            release=authority.release, binding=binding, owner_epoch=str(uuid4()),
            config_digest='sha256:'+('c' if wrong_digest else 'a')*64,
            session_digest='sha256:'+'b'*64, timeout_ms=2000)
        if wrong_digest:
            with pytest.raises(RuntimeError, match='local drain unavailable'): drain.report_local_drain(**options)
        else:
            assert drain.report_local_drain(**options) is True
        thread.join(4); assert not thread.is_alive() and len(results) == 1
        if wrong_digest:
            assert isinstance(results[0], launch.LaunchSupervisorError) and authority._held
        else:
            assert results == ['served'] and checks
            with authority._journal.transaction() as db:
                state, raw = db.execute('SELECT state,data FROM launch WHERE id=?', (authority._launch_id,)).fetchone()
            assert state == 'drain_recorded' and options['owner_epoch'] in raw
            with pytest.raises(launch.LaunchSupervisorError):
                authority._journal.record_coordinator_drain(authority._launch_id, {})
    finally:
        thread.join(4); assert not thread.is_alive()


@pytest.mark.parametrize('exit_nonzero', [False, True])
def test_coordinator_stop_signals_then_records_live_peer_before_exit(synthetic_issuer, monkeypatch, exit_nonzero):
    """Real peer/receipt/journal, simulated Engine signal and process exit."""
    authority, path, _ = synthetic_issuer
    authority.profile = SimpleNamespace(role='supervised-local-coordinator-v2')
    authority.release = runtime_identity.CoordinatorReleaseEnrollment(**{
        name:getattr(authority.release,name) for name in runtime_identity.CoordinatorReleaseEnrollment.__dataclass_fields__})
    authority._coordinator_admission = SimpleNamespace(config_digest='sha256:'+'a'*64, session_digest='sha256:'+'b'*64, timeout_ms=2000)
    authority._coordinator_stop_grace = 1
    authority._journal.reserve_issuance(authority._launch_id, 'a'*64)
    authority._journal.issuance_state(authority._launch_id, 'sent', 'b'*64)
    current_ready(authority)
    signalled = threading.Event(); drain_done = threading.Event(); events = []; results = []
    class Engine:
        def json(self, method, path, deadline, **kwargs):
            assert method == 'POST' and path.endswith('/kill?signal=SIGTERM')
            events.append('signal'); signalled.set(); return None, None
        def close(self): pass
    authority._engine = Engine()
    original_wait = authority._wait_stopped
    if exit_nonzero:
        authority._inspect = lambda deadline, running: {'Running':False, 'Status':'exited', 'Pid':0,
                                                       'ExitCode':1, 'OOMKilled':False, 'Error':''}
    def wait_stopped(deadline, *, require_exit_zero):
        assert require_exit_zero
        events.append('exited')
        if exit_nonzero: original_wait(deadline, require_exit_zero=True)
    authority._wait_stopped = wait_stopped
    authority._remove_owned_socket = lambda: events.append('socket-removed')
    original_drain = authority._serve_one_coordinator_drain
    def serve_drain(deadline): original_drain(deadline); drain_done.set()
    authority._serve_one_coordinator_drain = serve_drain
    real_select = launch.select.select
    def fake_select(read, write, error, timeout=None):
        if drain_done.is_set() and read == [authority._consumer.fd]: return (read, [], [])
        return real_select(read, write, error, timeout)
    monkeypatch.setattr(launch.select, 'select', fake_select)
    def stop():
        try: authority.graceful_stop_coordinator(timeout_ms=9000); results.append('stopped')
        except BaseException as error: results.append(error)
    thread = threading.Thread(target=stop); thread.start()
    try:
        assert signalled.wait(2), (events, results)
        assert drain.report_local_drain(socket_path=str(path), issuer_uid=os.geteuid(), issuer_gid=os.getegid(),
            release=authority.release, binding=(authority._launch_id, authority._consumer.boot, authority._consumer.pid,
                authority._consumer.start, authority._container_id), owner_epoch=str(uuid4()),
            config_digest=authority._coordinator_admission.config_digest,
            session_digest=authority._coordinator_admission.session_digest, timeout_ms=2000)
        thread.join(4); assert not thread.is_alive()
        if exit_nonzero:
            assert len(results) == 1 and isinstance(results[0], launch.LaunchSupervisorError) and authority._held
            assert events == ['signal', 'exited']
        else:
            assert results == ['stopped'] and events == ['signal', 'exited', 'socket-removed']
        with authority._journal.transaction() as db:
            state, raw = db.execute('SELECT state,data FROM launch WHERE id=?', (authority._launch_id,)).fetchone()
        assert state == ('held' if exit_nonzero else 'stopped') and 'local_drain' in raw
    finally:
        thread.join(4); assert not thread.is_alive()


def test_coordinator_exit_must_be_zero_but_legacy_semantics_remain(synthetic_issuer):
    authority, _, _ = synthetic_issuer
    authority._inspect = lambda deadline, running: {'Running':False, 'Status':'exited', 'Pid':0,
                                                   'ExitCode':1, 'OOMKilled':False, 'Error':''}
    authority._wait_stopped(time.monotonic()+1)
    with pytest.raises(launch.LaunchSupervisorError):
        authority._wait_stopped(time.monotonic()+1, require_exit_zero=True)


@pytest.mark.parametrize('timeout_ms,admitted', [(86_415_000, True), (86_414_999, False)])
def test_maximum_declared_grace_includes_exchange_and_accounting(synthetic_issuer, timeout_ms, admitted):
    authority, _, _ = synthetic_issuer
    authority.profile = SimpleNamespace(role='supervised-local-coordinator-v2')
    authority._coordinator_stop_grace = 86400
    authority._coordinator_admission = SimpleNamespace(timeout_ms=10000)
    current_ready(authority)
    reached = []
    def stop_before_effect(deadline):
        reached.append(True)
        raise RuntimeError('synthetic no-effect stop')
    authority._validate_live = stop_before_effect
    with pytest.raises(launch.LaunchSupervisorError):
        authority.graceful_stop_coordinator(timeout_ms=timeout_ms)
    assert bool(reached) is admitted
