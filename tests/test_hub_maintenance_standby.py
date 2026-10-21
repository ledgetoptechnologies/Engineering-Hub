"""Pure controls; actual Linux PID1/TrueNAS acceptance remains separate."""
import json
from unittest.mock import Mock

import pytest

from engineering_hub import maintenance_standby as module


@pytest.mark.parametrize('arguments', [[], ['--host', 'PRIVATE'], ['--stand'], ['--standby', 'PRIVATE']])
def test_explicit_mode_only(arguments, monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr(module, 'standby', run)
    with pytest.raises(SystemExit) as error:
        module.main(arguments)
    assert error.value.code == 2
    run.assert_not_called()
    assert 'PRIVATE' not in capsys.readouterr().err


def test_explicit_invocation_calls_only_standby(monkeypatch):
    run = Mock(return_value=0)
    monkeypatch.setattr(module, 'standby', run)
    assert module.main(['--standby']) == 0
    run.assert_called_once_with()


def test_wait_signals_and_no_host_access(monkeypatch, capsys):
    old = object()
    handlers = {}

    def install(signum, handler):
        handlers.setdefault(signum, handler)
        return old

    signals = Mock(side_effect=install)
    monkeypatch.setattr(module.signal, 'signal', signals)
    monkeypatch.setattr('builtins.open', Mock(side_effect=AssertionError('unexpected file open')))
    def stop(delay):
        assert delay == 0.1
        handlers[module.signal.SIGTERM](module.signal.SIGTERM, None)
        handlers[module.signal.SIGINT](module.signal.SIGINT, None)
    sleep = Mock(side_effect=stop)
    monkeypatch.setattr(module.time, 'sleep', sleep)
    assert module.standby() == 0
    sleep.assert_called_once_with(0.1)
    assert signals.call_count == 4
    assert signals.call_args_list[-2].args == (module.signal.SIGTERM, old)
    assert signals.call_args_list[-1].args == (module.signal.SIGINT, old)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]['all_writers_stopped'] == 'not_established'
    assert records[0]['database_opened'] is False
    assert records[0]['owner_constructed'] is False
    assert records[0]['network_listener_started'] is False
    assert records[1]['status'] == 'maintenance_standby_stopped'


def test_wait_failure_restores_handlers(monkeypatch, capsys):
    signals = Mock()
    monkeypatch.setattr(module.time, 'sleep', Mock(side_effect=RuntimeError('wait unavailable')))
    monkeypatch.setattr(module.signal, 'signal', signals)
    with pytest.raises(RuntimeError):
        module.standby()
    assert signals.call_count == 4
