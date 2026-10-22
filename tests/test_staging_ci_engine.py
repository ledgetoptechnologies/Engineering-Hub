"""Pure mocked engine commands; no daemon, host files or systemd activation."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux-only rootless helper')


@pytest.fixture
def engine():
    if sys.platform != 'linux':
        pytest.skip('Linux')
    path = Path(__file__).parents[1] / 'scripts/staging_ci_engine.py'
    spec = importlib.util.spec_from_file_location('ci_engine_under_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def selected(engine):
    return engine.Layout(1000, Path('/home/bkoltz'), Path('/run/user/1000/engineering-hub-ci'),
                         Path('/home/bkoltz/engineering-hub-ci'))


def state(engine):
    return dict(LoadState='loaded', ActiveState='active', SubState='running',
        InvocationID='a' * 32, MainPID='2345',
        ControlGroup='/user.slice/user-1000.slice/user@1000.service/app.slice/' + engine.UNIT,
        CPUQuotaPerSecUSec='1s', MemoryMax=str(engine.MEMORY), MemorySwapMax='0',
        TasksMax='384', Delegate='yes', Restart='no', SendSIGKILL='no',
        TimeoutStopUSec='20min', Transient='yes', Type='exec')


def test_root_and_other_user_are_refused(engine, monkeypatch):
    monkeypatch.setattr(engine.os, 'getuid', lambda: 0)
    with pytest.raises(engine.EngineError): engine.layout()
    monkeypatch.setattr(engine.os, 'getuid', lambda: 1000)
    monkeypatch.setattr(engine.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(engine.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_name='someone', pw_dir='/home/other'))
    with pytest.raises(engine.EngineError): engine.layout()


def test_clean_environment_and_fixed_command_do_not_inherit_remote_context(engine, selected, monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'tcp://production:2375')
    monkeypatch.setenv('HTTPS_PROXY', 'secret-proxy')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/other/socket')
    assert set(engine.environment(selected)) == {'HOME', 'PATH', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'LC_ALL'}
    command = engine.command(selected)
    assert command[:2] == ['/usr/bin/env', '-i']
    assert 'DOCKERD_ROOTLESS_ROOTLESSKIT_NET=none' in command
    assert 'DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=true' in command
    assert '--host=unix:///run/user/1000/engineering-hub-ci/docker.sock' in command
    assert '--iptables=false' in command and '--ip-forward=false' in command
    assert not any('2375' in value or 'eh-pilot' in value or 'secret' in value for value in command)
    assert engine.docker(selected, 'info')[:5] == ['/usr/bin/docker', '--config', str(selected.cli_config), '--host', 'unix://' + str(selected.socket)]


def test_runner_has_clean_env_timeout_and_no_shell(engine, selected, monkeypatch):
    calls = []
    def completed(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='ok', stderr='')
    monkeypatch.setattr(engine.subprocess, 'run', completed)
    assert engine.run(selected, ['/usr/bin/systemctl', '--user', 'show']) == 'ok'
    assert calls[0][1]['env'] == engine.environment(selected)
    assert calls[0][1]['timeout'] == 10 and 'shell' not in calls[0][1]


@pytest.mark.parametrize('field,value', [
    ('MemoryMax', 'infinity'), ('MemorySwapMax', 'infinity'), ('TasksMax', 'infinity'),
    ('Delegate', 'no'), ('Restart', 'always'), ('SendSIGKILL', 'yes'),
    ('TimeoutStopUSec', '90s'), ('CPUQuotaPerSecUSec', 'infinity'),
    ('Transient', 'no'), ('Type', 'simple'), ('ActiveState', 'failed'),
    ('InvocationID', 'not-uuid'), ('MainPID', '0'),
])
def test_unit_cap_or_identity_drift_refused_before_execution_check(engine, selected, monkeypatch, field, value):
    data = state(engine)
    data[field] = value
    monkeypatch.setattr(engine, '_execution', lambda _: pytest.fail('execution read'))
    with pytest.raises(engine.EngineError): engine.verified_unit(selected, data)


def test_exact_dbus_exec_argv_required(engine, selected, monkeypatch):
    command = engine.command(selected)
    monkeypatch.setattr(engine, '_json', lambda *_a: {'type': 'a(sasbttttuii)', 'data': [['/usr/bin/env', command]]})
    engine._execution(selected)
    monkeypatch.setattr(engine, '_json', lambda *_a: {'data': [['/usr/bin/env', command + ['--debug']]]})
    with pytest.raises(engine.EngineError): engine._execution(selected)


def test_controller_cgroup_limits_are_checked(engine, selected, monkeypatch):
    data = state(engine)
    values = {'memory.max': str(engine.MEMORY), 'memory.swap.max': '0', 'pids.max': '384', 'cpu.max': '100000 100000'}
    monkeypatch.setattr(Path, 'resolve', lambda self: self)
    monkeypatch.setattr(Path, 'read_text', lambda self: values[self.name])
    engine._cgroup(selected, data)
    values['cpu.max'] = 'max 100000'
    with pytest.raises(engine.EngineError): engine._cgroup(selected, data)


def test_unrelated_control_group_refused(engine, selected):
    data = state(engine)
    data['ControlGroup'] = '/system.slice/docker.service'
    with pytest.raises(engine.EngineError): engine._cgroup(selected, data)


def test_start_refuses_existing_or_failed_unit_without_restarting(engine, selected, monkeypatch):
    monkeypatch.setattr(engine, '_directory', lambda *_a, **_k: None)
    monkeypatch.setattr(engine, 'unit_state', lambda _: state(engine))
    monkeypatch.setattr(engine, 'run', lambda *_a, **_k: pytest.fail('activation'))
    with pytest.raises(engine.EngineError): engine.start(selected)


def test_start_refuses_low_disk_before_directory_writes(engine, selected, monkeypatch):
    monkeypatch.setattr(engine, '_directory', lambda *_a, **_k: None)
    monkeypatch.setattr(engine, 'unit_state', lambda _: {'LoadState': 'not-found', 'ActiveState': 'inactive'})
    monkeypatch.setattr(engine.shutil, 'disk_usage', lambda _: SimpleNamespace(free=7 * 1024**3))
    monkeypatch.setattr(engine, 'directories', lambda *_a, **_k: pytest.fail('directory write'))
    with pytest.raises(engine.EngineError): engine.start(selected)


def result(engine):
    return {'schema': 'engineering-hub.development-ci-engine.v1', 'state': 'ready',
        'unit': engine.UNIT, 'engine_id': 'engine-identity-123', 'invocation_id': 'a' * 32}


@pytest.mark.parametrize('mismatch', ['engine', 'invocation'])
def test_stop_requires_exact_pins_before_command(engine, selected, monkeypatch, mismatch):
    monkeypatch.setattr(engine, 'inspect', lambda _: result(engine))
    monkeypatch.setattr(engine, 'run', lambda *_a, **_k: pytest.fail('command'))
    with pytest.raises(engine.EngineError):
        engine._stop_locked(selected, 0, engine_id='wrong' if mismatch == 'engine' else 'engine-identity-123',
                    invocation_id='wrong' if mismatch == 'invocation' else 'a' * 32)


def test_stop_never_interrupts_a_running_container(engine, selected, monkeypatch):
    monkeypatch.setattr(engine, 'inspect', lambda _: result(engine))
    calls = []
    def running(_value, argv):
        calls.append(argv)
        return 'b' * 64
    monkeypatch.setattr(engine, 'run', running)
    with pytest.raises(engine.EngineError):
        engine._stop_locked(selected, 0, engine_id='engine-identity-123', invocation_id='a' * 32)
    assert len(calls) == 1 and calls[0][-3:] == ['ps', '--quiet', '--no-trunc']


def test_stop_proves_terminal_before_removing_own_marker(engine, tmp_path, monkeypatch):
    selected = engine.Layout(1000, tmp_path, tmp_path, tmp_path / 'private')
    monkeypatch.setattr(engine, 'inspect', lambda _: result(engine))
    monkeypatch.setattr(engine, 'unit_state', lambda _: state(engine))
    monkeypatch.setattr(engine, '_pid_start', lambda _: '1234')
    monkeypatch.setattr(engine, '_terminal', lambda *_a: True)
    calls = []
    monkeypatch.setattr(engine, 'run', lambda _, argv: calls.append(argv) or '')
    parent = engine.os.open(tmp_path, engine.os.O_RDONLY | engine.os.O_DIRECTORY)
    try:
        stopped = engine._stop_locked(selected, parent, engine_id='engine-identity-123', invocation_id='a' * 32)
    finally:
        engine.os.close(parent)
    assert calls[-1] == ['/usr/bin/systemctl', '--user', 'stop', '--no-block', engine.UNIT]
    assert stopped['state'] == 'stopped' and stopped['terminal_verified'] is True
    assert stopped['retained_data_root'] == str(selected.data)
    assert not (tmp_path / 'STOPPING.json').exists()


def test_stop_unknown_retains_marker_without_force_kill(engine, tmp_path, monkeypatch):
    selected = engine.Layout(1000, tmp_path, tmp_path, tmp_path / 'private')
    monkeypatch.setattr(engine, 'inspect', lambda _: result(engine))
    monkeypatch.setattr(engine, 'unit_state', lambda _: state(engine))
    monkeypatch.setattr(engine, '_pid_start', lambda _: '1234')
    monkeypatch.setattr(engine, '_terminal', lambda *_a: False)
    clock = iter([0, 0, 30, 30])
    monkeypatch.setattr(engine.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(engine.time, 'sleep', lambda _: None)
    calls = []
    monkeypatch.setattr(engine, 'run', lambda _, argv: calls.append(argv) or '')
    parent = engine.os.open(tmp_path, engine.os.O_RDONLY | engine.os.O_DIRECTORY)
    try:
        stopped = engine._stop_locked(selected, parent, engine_id='engine-identity-123', invocation_id='a' * 32)
    finally:
        engine.os.close(parent)
    assert stopped['state'] == 'stop_requested' and stopped['terminal_verified'] is False
    assert (tmp_path / 'STOPPING.json').exists()
    marker = json.loads((tmp_path / 'STOPPING.json').read_text())
    assert marker['control_group'] == '/sys/fs/cgroup' + state(engine)['ControlGroup']
    assert len(calls) == 2 and all('kill' not in command for command in calls)


@pytest.mark.parametrize('events,expected', [
    ('populated 0\nfrozen 0\n', True),
    ('populated 1\nfrozen 0\n', False),
    ('frozen 0\n', False),
    ('populated 0\npopulated 0\n', False),
    ('populated unknown\n', False),
])
def test_original_cgroup_population_must_be_zero(engine, tmp_path, events, expected):
    (tmp_path / 'cgroup.events').write_text(events)
    assert engine._cgroup_empty(tmp_path) is expected


def test_original_cgroup_absence_is_terminal_but_missing_events_is_not(engine, tmp_path):
    assert engine._cgroup_empty(tmp_path / 'absent') is True
    with pytest.raises(FileNotFoundError):
        engine._cgroup_empty(tmp_path)


def test_cgroup_symlink_or_invalid_event_bytes_fail_closed(engine, tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'cgroup.events').write_text('populated 0\n')
    alias = tmp_path / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        engine._cgroup_empty(alias)
    (target / 'cgroup.events').write_bytes(b'populated 0\n\xff')
    with pytest.raises(engine.EngineError):
        engine._cgroup_empty(target)


def test_pid_and_socket_gone_do_not_prove_empty_original_cgroup(engine, selected, monkeypatch):
    inactive = {**state(engine), 'ActiveState': 'inactive', 'MainPID': '0'}
    monkeypatch.setattr(engine, 'unit_state', lambda _: inactive)
    monkeypatch.setattr(engine, '_pid_start', lambda _: None)
    monkeypatch.setattr(engine, '_present', lambda _: False)
    original = Path('/sys/fs/cgroup') / state(engine)['ControlGroup'].lstrip('/')
    calls = []
    monkeypatch.setattr(engine, '_cgroup_empty', lambda path: calls.append(path) or False)
    assert engine._terminal(selected, 2345, '1234', 'a' * 32, original) is False
    assert calls == [original]
    monkeypatch.setattr(engine, '_cgroup_empty', lambda path: path == original)
    assert engine._terminal(selected, 2345, '1234', 'a' * 32, original) is True


@pytest.mark.parametrize('marker', ['HELD.json', 'STOPPING.json'])
def test_stop_does_not_clear_or_ignore_admission_markers(engine, tmp_path, monkeypatch, marker):
    selected = engine.Layout(568, tmp_path, tmp_path, tmp_path / 'private')
    (tmp_path / marker).write_text('synthetic retained intent')
    monkeypatch.setattr(engine, 'inspect', lambda _: pytest.fail('adoption'))
    with pytest.raises(engine.EngineError):
        engine._stop_locked(selected, 0, engine_id='engine-identity-123', invocation_id='a' * 32)
    assert (tmp_path / marker).read_text() == 'synthetic retained intent'


def test_admission_lock_is_same_private_name_and_nonblocking(engine, tmp_path, monkeypatch):
    selected = engine.Layout(engine.os.getuid(), tmp_path, tmp_path, tmp_path / 'private')
    monkeypatch.setattr(engine, '_directory', lambda *_a, **_k: None)
    with engine.admission_lock(selected):
        with pytest.raises(BlockingIOError):
            with engine.admission_lock(selected):
                pytest.fail('Concurrent admission')
    info = (tmp_path / 'admission.lock').stat()
    assert engine.stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    with engine.admission_lock(selected):
        pass


def test_no_cli_output_leaks_implementation_errors(engine, monkeypatch, capsys):
    monkeypatch.setattr(engine, 'layout', lambda: (_ for _ in ()).throw(RuntimeError('SECRET-ERROR')))
    assert engine.main(['inspect']) == 2
    output = capsys.readouterr().err
    assert 'SECRET' not in output and json.loads(output)['state'] == 'unavailable'
