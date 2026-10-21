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

        def serve_one_receipt(self):
            self.perform('receipt')

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
        'journal_file': ARGS[5], 'create_journal': True}), 'launch', 'receipt',
        ('wait', 60), 'stop', 'close', ('restore', [('synthetic', 'old')])]
    assert capsys.readouterr().out == 'receipt_served\nstopped\n'


@pytest.mark.parametrize('stop_at', ['handlers', 'construct'])
def test_stop_before_launch_nonzero_no_stopped_claim(module, monkeypatch, capsys, stop_at):
    calls = harness(module, monkeypatch, stop_at=stop_at)
    assert module.main(ARGS + ['--execute', '--create-journal']) == 130
    assert 'launch' not in calls and 'stop' not in calls and 'receipt' not in calls
    assert calls[-1][0] == 'restore'
    assert capsys.readouterr().out == 'interrupted_before_launch\n'


@pytest.mark.parametrize('stop_at', ['launch', 'receipt'])
def test_flag_only_stop_at_native_boundaries_is_once(module, monkeypatch, stop_at):
    calls = harness(module, monkeypatch, stop_at=stop_at)
    assert module.main(ARGS + ['--execute', '--create-journal']) == 0
    assert calls.count('launch') == calls.count('stop') == calls.count('close') == 1
    assert calls.count('receipt') == (0 if stop_at == 'launch' else 1)
    assert not any(isinstance(item, tuple) and item[0] == 'wait' for item in calls)


@pytest.mark.parametrize('failure', ['construct', 'launch', 'receipt', 'stop'])
def test_native_exception_no_retry_no_automatic_cleanup_and_redacted(module, monkeypatch, capsys, failure):
    calls = harness(module, monkeypatch, failure=failure, stop_at='receipt')
    assert module.main(ARGS + ['--execute', '--create-journal']) == 2
    assert calls.count('launch') <= 1 and calls.count('receipt') <= 1 and calls.count('stop') <= 1
    if failure in ('construct', 'launch', 'receipt'):
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
    assert len(handler.body) == 1 and isinstance(handler.body[0], ast.Assign)
    assert ast.unparse(handler.body[0]) == 'self.requested = True'


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
