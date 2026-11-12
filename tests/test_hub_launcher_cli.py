"""Mocked source orchestration only, NOT host/kernel/receipt/runtime proof.

Load the stdlib-only CLI file directly on Windows: importing the executor package
itself requires optional Linux providers. No real native supervisor is loaded.
"""
import importlib.util
import ast
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def module(monkeypatch):
    path = Path(__file__).parents[1] / 'hub_container_executor' / 'launcher_cli.py'
    spec = importlib.util.spec_from_file_location('synthetic_launcher_cli', path)
    value = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, value)
    spec.loader.exec_module(value)
    return value


ARGS = ['--enrollment-file', '/protected/enrollment.json', '--signing-key-file',
        '/protected/existing.key', '--journal-file', '/fresh/journal.sqlite3',
        '--wait-seconds', '60']


def harness(module, monkeypatch, *, failure=None, stop_at='wait', close_failure=False):
    calls = []
    state = SimpleNamespace(stop=None)

    class Supervisor:
        def __init__(self, **kwargs):
            calls.append(('construct', kwargs))
            if failure == 'construct':
                raise RuntimeError('SECRET constructor error')
            if stop_at == 'construct':
                state.stop.request(0, None)

        def perform(self, name):
            calls.append(name)
            if stop_at == name:
                state.stop.request(0, None)
                state.stop.request(0, None)
            if failure == name:
                raise RuntimeError('SECRET native error')

        def launch(self):
            self.perform('launch')

        def wait_for_receipt_request(self, *, timeout_ms, stop_requested):
            assert timeout_ms == 60000
            self.perform('readiness')
            return not stop_requested()

        def serve_one_receipt(self):
            self.perform('receipt')

        def write_approval_proposal(self, path, *, timeout_ms):
            assert path == '/protected/proposal.json' and timeout_ms == 10000
            self.perform('proposal')

        def wait_for_stop_with_observations(self, *, timeout_ms, stop_requested):
            assert type(timeout_ms) is int and 1000 <= timeout_ms <= 3600000
            self.perform('observation-wait')
            return wait(state.stop, timeout_ms // 1000)

        def graceful_stop(self):
            self.perform('stop')

        def close(self):
            calls.append('close')
            if close_failure:
                raise RuntimeError('SECRET close error')

    def install(stop):
        state.stop = stop
        calls.append('handlers')
        if stop_at == 'handlers':
            stop.request(0, None)
        return [('synthetic', 'old')]

    def wait(stop, seconds):
        calls.append(('wait', seconds))
        if stop_at == 'wait':
            stop.request(0, None)
        return stop.requested

    monkeypatch.setattr(module, '_host_available', lambda: True)
    monkeypatch.setattr(module, '_load_supervisor', lambda: Supervisor)
    monkeypatch.setattr(module, '_install_handlers', install)
    monkeypatch.setattr(module, '_restore_handlers', lambda previous: calls.append(('restore', previous)))
    monkeypatch.setattr(module, '_wait_for_stop', wait)
    return calls


def test_dry_default_never_constructs_or_installs_handlers(module, monkeypatch, capsys):
    calls = harness(module, monkeypatch)
    assert module.main(ARGS + ['--create-journal']) == 0
    assert calls == []
    assert capsys.readouterr().out == 'not_executed\n'


def test_explicit_proposal_is_written_after_receipt_before_observation_service(module, monkeypatch, capsys):
    calls = harness(module, monkeypatch)
    assert module.main(ARGS + ['--execute', '--create-journal',
                               '--approval-proposal-file', '/protected/proposal.json']) == 0
    assert calls.index('receipt') < calls.index('proposal') < calls.index('observation-wait')
    assert capsys.readouterr().out == 'receipt_served\napproval_proposal_ready\nstopped\n'


def test_help_never_loads_native_constructor_or_installs_handlers(module, monkeypatch, capsys):
    calls = harness(module, monkeypatch)
    with pytest.raises(SystemExit) as outcome:
        module.main(['--help'])
    assert outcome.value.code == 0 and calls == []
    assert '--wait-seconds' in capsys.readouterr().out


@pytest.mark.parametrize('seconds', ['1', '3600'])
def test_wait_bound_endpoints_pass_exactly_to_wait(module, monkeypatch, seconds):
    calls = harness(module, monkeypatch)
    args = list(ARGS)
    args[7] = seconds
    assert module.main(args + ['--execute', '--create-journal']) == 0
    assert ('wait', int(seconds)) in calls


@pytest.mark.parametrize('change', ['journal-consent', 'platform', 'relative', 'nul', 'same-path',
                                    'wait-zero', 'wait-huge', 'unknown', 'abbreviated'])
def test_invalid_cli_denies_before_native_construction(module, monkeypatch, capsys, change):
    calls = harness(module, monkeypatch)
    args = ARGS + ['--execute', '--create-journal']
    if change == 'journal-consent': args.remove('--create-journal')
    elif change == 'platform': monkeypatch.setattr(module, '_host_available', lambda: False)
    elif change == 'relative': args[1] = 'relative'
    elif change == 'nul': args[1] = '/SECRET\x00'
    elif change == 'same-path': args[5] = args[1]
    elif change == 'wait-zero': args[7] = '0'
    elif change == 'wait-huge': args[7] = '3601'
    elif change == 'unknown': args += ['--unknown', 'SECRET']
    else: args[-1] = '--create-j'
    assert module.main(args) == 2
    assert calls == []
    output = capsys.readouterr()
    assert output.err == 'invalid_operator_request\n'
    assert 'SECRET' not in output.out + output.err


def test_exact_order_one_launch_receipt_wait_explicit_stop_close_restore(module, monkeypatch, capsys):
    calls = harness(module, monkeypatch)
    assert module.main(ARGS + ['--execute', '--create-journal']) == 0
    assert calls == ['handlers', ('construct', {
        'enrollment_file': ARGS[1], 'signing_key_file': ARGS[3],
        'journal_file': ARGS[5], 'create_journal': True}), 'launch', 'readiness', 'receipt',
        'observation-wait', ('wait', 60), 'stop', 'close', ('restore', [('synthetic', 'old')])]
    assert capsys.readouterr().out == 'receipt_served\nstopped\n'


@pytest.mark.parametrize('stop_at', ['handlers', 'construct'])
def test_stop_before_launch_nonzero_no_stopped_claim(module, monkeypatch, capsys, stop_at):
    calls = harness(module, monkeypatch, stop_at=stop_at)
    assert module.main(ARGS + ['--execute', '--create-journal']) == 130
    assert 'launch' not in calls and 'stop' not in calls and 'receipt' not in calls
    assert calls[-1][0] == 'restore'
    assert capsys.readouterr().out == 'interrupted_before_launch\n'


@pytest.mark.parametrize('stop_at', ['launch', 'readiness', 'receipt'])
def test_flag_only_stop_at_native_boundaries_is_once(module, monkeypatch, stop_at):
    calls = harness(module, monkeypatch, stop_at=stop_at)
    assert module.main(ARGS + ['--execute', '--create-journal']) == 0
    assert calls.count('launch') == calls.count('stop') == calls.count('close') == 1
    assert calls.count('receipt') == (1 if stop_at == 'receipt' else 0)
    assert not any(isinstance(item, tuple) and item[0] == 'wait' for item in calls)


@pytest.mark.parametrize('failure', ['construct', 'launch', 'readiness', 'receipt', 'observation-wait', 'stop'])
def test_native_exception_no_retry_no_automatic_cleanup_and_redacted(module, monkeypatch, capsys, failure):
    calls = harness(module, monkeypatch, failure=failure,
                    stop_at='wait' if failure == 'observation-wait' else 'receipt')
    assert module.main(ARGS + ['--execute', '--create-journal']) == 2
    assert calls.count('launch') <= 1 and calls.count('receipt') <= 1 and calls.count('stop') <= 1
    if failure in ('construct', 'launch', 'readiness', 'receipt', 'observation-wait'):
        assert 'stop' not in calls
    assert calls.count('close') == (0 if failure == 'construct' else 1)
    assert calls[-1][0] == 'restore'
    output = capsys.readouterr()
    assert 'SECRET' not in output.out + output.err
    assert 'stopped\n' not in output.out


def test_wait_expiry_closes_only_does_not_stop(module, monkeypatch, capsys):
    calls = harness(module, monkeypatch, stop_at=None)
    assert module.main(ARGS + ['--execute', '--create-journal']) == 2
    assert 'stop' not in calls and 'close' in calls
    assert capsys.readouterr().out == 'receipt_served\nwait_expired_preserve_evidence\n'


@pytest.mark.parametrize('primary', [None, 'receipt'])
def test_close_failure_nonzero_preserves_primary_outcome(module, monkeypatch, capsys, primary):
    harness(module, monkeypatch, failure=primary, close_failure=True)
    assert module.main(ARGS + ['--execute', '--create-journal']) == 2
    output = capsys.readouterr().out
    assert output.endswith(('unavailable_preserve_evidence\n' if primary else
                            'close_failed_preserve_evidence\n'))
    assert 'SECRET' not in output


def test_actual_flag_handler_and_saved_handler_restoration(module, monkeypatch):
    saved = {1: object(), 2: object(), 3: object()}
    installed = dict(saved)
    monkeypatch.setattr(module.signal, 'SIGUSR1', 1, raising=False)
    monkeypatch.setattr(module.signal, 'SIGTERM', 2)
    monkeypatch.setattr(module.signal, 'SIGINT', 3)
    monkeypatch.setattr(module.signal, 'getsignal', installed.__getitem__)
    monkeypatch.setattr(module.signal, 'signal', installed.__setitem__)
    stop = module._Stop()
    previous = module._install_handlers(stop)
    installed[1](1, None)
    installed[2](2, None)
    assert stop.requested is True
    module._restore_handlers(previous)
    assert installed == saved


def test_partial_signal_install_failure_restores_previous_handlers(module, monkeypatch):
    saved = {1: object(), 2: object(), 3: object()}
    installed = dict(saved)
    monkeypatch.setattr(module.signal, 'SIGUSR1', 1, raising=False)
    monkeypatch.setattr(module.signal, 'SIGTERM', 2)
    monkeypatch.setattr(module.signal, 'SIGINT', 3)
    monkeypatch.setattr(module.signal, 'getsignal', installed.__getitem__)

    def install(number, handler):
        if number == 2:
            raise RuntimeError('SECRET install error')
        installed[number] = handler

    monkeypatch.setattr(module.signal, 'signal', install)
    with pytest.raises(RuntimeError):
        module._install_handlers(module._Stop())
    assert installed == saved


@pytest.mark.parametrize('primary', [None, 'launch'])
def test_restore_failure_nonzero_and_never_replaces_primary_hold(module, monkeypatch, capsys, primary):
    harness(module, monkeypatch, failure=primary)

    def fail_restore(previous):
        raise RuntimeError('SECRET restoration error')

    monkeypatch.setattr(module, '_restore_handlers', fail_restore)
    assert module.main(ARGS + ['--execute', '--create-journal']) == 2
    output = capsys.readouterr().out
    assert output.endswith('unavailable_preserve_evidence\n' if primary else 'handler_restore_failed\n')
    assert 'SECRET' not in output


def test_signal_handler_source_is_only_flag_assignment(module):
    tree = ast.parse(Path(module.__file__).read_text())
    stop = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == '_Stop')
    handler = next(item for item in stop.body if isinstance(item, ast.FunctionDef) and item.name == 'request')
    assert [ast.unparse(item) for item in handler.body] == [
        'self.requested = True', 'self.requests += 1']


def test_wait_is_bounded_without_native_calls(module, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert module._wait_for_stop(module._Stop(), 1) is False
    assert 1 <= clock[0] < 1.1


@pytest.mark.parametrize('platform,uid,euid,expected', [
    ('win32', 0, 0, False), ('linux', 1000, 0, False),
    ('linux', 0, 1000, False), ('linux', 0, 0, True)])
def test_host_platform_root_and_kernel_facilities(module, monkeypatch, platform, uid, euid, expected):
    monkeypatch.setattr(module.sys, 'platform', platform)
    monkeypatch.setattr(module.os, 'getuid', lambda: uid, raising=False)
    monkeypatch.setattr(module.os, 'geteuid', lambda: euid, raising=False)
    monkeypatch.setattr(module.os, 'pidfd_open', object(), raising=False)
    monkeypatch.setattr(module.socket, 'SO_PEERCRED', 17, raising=False)
    monkeypatch.setattr(module.signal, 'SIGUSR1', 10, raising=False)
    assert module._host_available() is expected


@pytest.mark.parametrize('missing', ['pidfd', 'peercred', 'signal', 'mainthread'])
def test_host_rejects_missing_kernel_or_signal_facilities(module, monkeypatch, missing):
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    monkeypatch.setattr(module.os, 'getuid', lambda: 0, raising=False)
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(module.os, 'pidfd_open', object(), raising=False)
    monkeypatch.setattr(module.socket, 'SO_PEERCRED', 17, raising=False)
    monkeypatch.setattr(module.signal, 'SIGUSR1', 10, raising=False)
    if missing == 'pidfd': monkeypatch.delattr(module.os, 'pidfd_open')
    elif missing == 'peercred': monkeypatch.delattr(module.socket, 'SO_PEERCRED')
    elif missing == 'signal': monkeypatch.delattr(module.signal, 'SIGUSR1')
    else: monkeypatch.setattr(module.threading, 'current_thread', lambda: object())
    assert module._host_available() is False


@pytest.mark.parametrize('outcome', ['complete', 'wait_timeout', 'policy_changed', 'record_failed',
                                     'invalid_scope', 'scope_mismatch'])
def test_explicit_scope_policy_lifecycle_never_signals_daemon(module, monkeypatch, capsys, outcome):
    calls = []
    stop_ref = SimpleNamespace(value=None)
    class Scope:
        _processes = (SimpleNamespace(fd=1),)
        def close(self): calls.append('scope-close')
    scope = Scope()
    class Policy:
        scope = 'dedicated/daemon'
        wait_seconds = 5
        members = ()
        def check(self, supervisor, observed=None):
            calls.append('policy-check-scoped' if observed else 'policy-check')
            if ((outcome == 'policy_changed' and calls.count('policy-check-scoped') == 3)
                    or (outcome == 'scope_mismatch' and observed is not None)):
                raise ValueError()
    class Supervisor:
        def __init__(self, **kwargs):
            self._engine = SimpleNamespace(process=SimpleNamespace(check=lambda: calls.append('daemon-check')))
            calls.append('construct')
        def launch(self): calls.append('launch')
        def preflight_daemon_scope(self, scope_path, *, timeout_ms):
            assert scope_path == 'dedicated/daemon' and timeout_ms == 10000
            calls.append('preflight')
            if outcome == 'invalid_scope': raise ValueError()
            return scope
        def verify_preflight_daemon_scope(self, value, *, timeout_ms):
            assert value is scope and timeout_ms == 10000
            calls.append('postlaunch-verify')
        def wait_for_receipt_request(self, **kwargs): calls.append('readiness'); return True
        def serve_one_receipt(self): calls.append('receipt')
        def wait_for_stop_with_observations(self, **kwargs):
            calls.append('wait'); stop_ref.value.request(0, None); return True
        def graceful_stop(self): calls.append('consumer-stop')
        def record_daemon_scope_emptiness(self, value):
            assert value is scope
            calls.append('record')
            if outcome == 'record_failed': raise ValueError()
        def close(self): calls.append('supervisor-close')
    def install(stop): stop_ref.value = stop; return []
    monkeypatch.setattr(module, '_install_handlers', install)
    monkeypatch.setattr(module, '_restore_handlers', lambda _: calls.append('restore'))
    monkeypatch.setattr(module, '_host_available', lambda: True)
    monkeypatch.setattr(module, '_load_supervisor', lambda: Supervisor)
    monkeypatch.setattr(module, '_load_daemon_scope_policy', lambda path: Policy())
    def wait_scope(value, stop, seconds, initial):
        assert value is scope and seconds == 5 and initial == 1
        calls.append('daemon-wait')
        return outcome != 'wait_timeout'
    monkeypatch.setattr(module, '_wait_for_daemon_exit', wait_scope)
    status = module.main(ARGS + ['--execute', '--create-journal', '--daemon-scope-policy', '/protected/scope.json'])
    assert status == (0 if outcome == 'complete' else 2)
    if outcome in ('invalid_scope', 'scope_mismatch'):
        assert 'launch' not in calls and 'consumer-stop' not in calls and 'record' not in calls
    else:
        assert calls.index('policy-check') < calls.index('preflight') < calls.index('launch') < calls.index('postlaunch-verify') < calls.index('receipt')
        assert calls.index('consumer-stop') < calls.index('daemon-wait') < calls.index('scope-close') < calls.index('supervisor-close')
        assert ('record' in calls) == (outcome in ('complete', 'record_failed'))
    assert 'daemon_scope_observed_no_effect_outcome' in capsys.readouterr().out if outcome == 'complete' else True


def test_repeated_stop_cancels_scope_wait_without_signal(module, monkeypatch):
    stop = module._Stop()
    stop.request(0, None)
    initial = stop.requests
    stop.request(0, None)
    scope = SimpleNamespace(_processes=(SimpleNamespace(fd=0),))
    assert module._wait_for_daemon_exit(scope, stop, 5, initial) is False


@pytest.mark.parametrize('outcome', ['complete','second_worker_fails','post_enrollment_mismatch','wait_timeout'])
def test_hierarchical_policy_retains_and_closes_exact_workers(module, monkeypatch, outcome):
    calls = []
    stop_ref = SimpleNamespace(value=None)
    daemon = SimpleNamespace(pid=10,start=11,fd=10,check=lambda: None)
    class Worker:
        def __init__(self,pid,start): self.pid,self.start,self.fd = pid,start,pid
        def close(self): calls.append(('worker-close',self.pid))
    class Scope:
        def __init__(self,processes): self._processes = processes
        def close(self): calls.append('scope-close')
    class Policy:
        scope='root/worker'
        wait_seconds=5
        members=((10,11),(20,21),(30,31))
        def check(self,supervisor,scope=None):
            calls.append('policy-scoped' if scope else 'policy')
            if scope is not None and outcome == 'post_enrollment_mismatch': raise ValueError()
    class Supervisor:
        def __init__(self,**kwargs): self._engine=SimpleNamespace(process=daemon)
        def preflight_worker_scope(self,path,*,processes,timeout_ms):
            calls.append(('preflight',tuple(x.pid for x in processes)))
            assert path == 'root/worker' and timeout_ms == 10000
            return Scope(processes)
        def launch(self): calls.append('launch')
        def verify_preflight_daemon_scope(self,scope,*,timeout_ms): calls.append('verify-live')
        def wait_for_receipt_request(self,**kwargs): return True
        def serve_one_receipt(self): pass
        def wait_for_stop_with_observations(self,**kwargs):
            stop_ref.value.request(0,None); return True
        def graceful_stop(self): calls.append('consumer-stop')
        def record_daemon_scope_emptiness(self,scope): calls.append('record')
        def close(self): calls.append('supervisor-close')
    def install(stop): stop_ref.value=stop; return []
    def open_worker(pid,ticks):
        calls.append(('open',pid,ticks))
        if outcome=='second_worker_fails' and pid==30: raise ValueError()
        return Worker(pid,ticks)
    monkeypatch.setattr(module,'_host_available',lambda: True)
    monkeypatch.setattr(module,'_install_handlers',install)
    monkeypatch.setattr(module,'_restore_handlers',lambda previous: None)
    monkeypatch.setattr(module,'_load_supervisor',lambda: Supervisor)
    monkeypatch.setattr(module,'_load_daemon_scope_policy',lambda path: Policy())
    monkeypatch.setattr(module,'_open_worker_process',open_worker)
    def wait(scope,stop,seconds,initial):
        assert tuple(p.pid for p in scope._processes)==(10,20,30)
        calls.append('wait-all')
        return outcome != 'wait_timeout'
    monkeypatch.setattr(module,'_wait_for_daemon_exit',wait)
    code = module.main(ARGS + ['--execute','--create-journal','--daemon-scope-policy','/protected/workers.json'])
    assert code == (0 if outcome=='complete' else 2)
    assert ('worker-close',20) in calls
    assert ('worker-close',30) in calls if outcome!='second_worker_fails' else ('worker-close',30) not in calls
    if outcome in ('second_worker_fails','post_enrollment_mismatch'):
        assert 'launch' not in calls and 'record' not in calls
    else:
        assert calls.index('consumer-stop') < calls.index('wait-all')
        assert ('record' in calls) == (outcome=='complete')
    assert calls[-1] == 'supervisor-close'


def test_worker_wait_removes_exited_pidfd_without_busy_polling(module, monkeypatch):
    import select
    stop=module._Stop(); stop.request(0,None)
    scope=SimpleNamespace(_processes=(SimpleNamespace(fd=10),SimpleNamespace(fd=20)))
    seen=[]
    class Poll:
        def __init__(self): self.registered=set()
        def register(self,fd,mask): self.registered.add(fd)
        def unregister(self,fd): self.registered.remove(fd)
        def poll(self,timeout):
            seen.append(set(self.registered))
            return [(10 if len(seen)==1 else 20, select.POLLIN)]
    monkeypatch.setattr(select,'poll',Poll)
    assert module._wait_for_daemon_exit(scope,stop,5,stop.requests) is True
    assert seen == [{10,20},{20}]


def test_invalid_pidfd_poll_event_fails_closed(module, monkeypatch):
    import select
    stop=module._Stop(); stop.request(0,None)
    scope=SimpleNamespace(_processes=(SimpleNamespace(fd=10),))
    class Poll:
        def register(self,fd,mask): pass
        def poll(self,timeout): return [(10,select.POLLNVAL)]
    monkeypatch.setattr(select,'poll',Poll)
    with pytest.raises(RuntimeError):
        module._wait_for_daemon_exit(scope,stop,5,stop.requests)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux pidfd required')
def test_worker_wait_accepts_real_high_numbered_pidfd(module):
    import fcntl, os, resource, subprocess
    limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    if limit != resource.RLIM_INFINITY and limit <= 1100:
        pytest.skip('descriptor limit does not allow a high-fd regression')
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.2)'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    original = high = None
    try:
        original = os.pidfd_open(child.pid, 0)
        high = fcntl.fcntl(original, fcntl.F_DUPFD_CLOEXEC, 1100)
        assert high >= 1100
        stop = module._Stop(); stop.request(0,None)
        scope = SimpleNamespace(_processes=(SimpleNamespace(fd=high),))
        assert module._wait_for_daemon_exit(scope,stop,5,stop.requests) is True
        assert child.wait(timeout=2) == 0
    finally:
        if high is not None: os.close(high)
        if original is not None: os.close(original)
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=2)


def test_worker_exit_observed_after_deadline_is_not_success(module, monkeypatch):
    import select
    stop=module._Stop(); stop.request(0,None)
    scope=SimpleNamespace(_processes=(SimpleNamespace(fd=10),))
    clock=iter([10.0,10.0,15.1])
    monkeypatch.setattr(module.time,'monotonic',lambda: next(clock))
    class Poll:
        def register(self,fd,mask): pass
        def unregister(self,fd): pass
        def poll(self,timeout): return [(10,select.POLLIN)]
    monkeypatch.setattr(select,'poll',Poll)
    assert module._wait_for_daemon_exit(scope,stop,5,stop.requests) is False
