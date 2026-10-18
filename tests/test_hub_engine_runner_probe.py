"""Fake probe backend tests; actual Unix-socket/runner verification is separate."""
import importlib.util
import builtins
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts import hub_engine_runner_probe as probe


@dataclass(frozen=True)
class FakeRunnerObservation:
    runner_id: str
    daemon_identity: str
    platform: object


def arguments(**changes):
    values = {'runner-id': 'enrolled-runner', 'daemon-id': 'enrolled-daemon',
              'architecture': 'amd64', 'socket': '/run/docker.sock',
              'api-version': '1.47', 'generation-root': '/private/generations'}
    values.update(changes)
    return [item for key, value in values.items() if value is not None for item in ('--' + key, value)]


@pytest.fixture
def fake(monkeypatch):
    calls = []
    failures = {}
    monkeypatch.setattr(probe.sys, 'platform', 'linux')
    def binding(*args):
        calls.append(('binding', args))
        return SimpleNamespace(runner_id=args[0], daemon_identity=args[1], platform=args[2])
    class FakeBackend:
        def __init__(self, bound):
            calls.append(('constructor', bound))
            self.bound = bound
            if failures.get('constructor'):
                raise RuntimeError('secret-constructor-value')
        def inspect_runner(self, timeout):
            calls.append(('inspect_runner', timeout))
            if failures.get('inspect'):
                raise RuntimeError('secret-response-value /private/path')
            if failures.get('wrong_observation'):
                return FakeRunnerObservation('untrusted', self.bound.daemon_identity, self.bound.platform)
            return FakeRunnerObservation(self.bound.runner_id, self.bound.daemon_identity, self.bound.platform)
        def close(self):
            calls.append(('close',))
            if failures.get('close'):
                raise RuntimeError('secret-close-value')
        def __getattr__(self, name):
            raise AssertionError('Non-runner operation attempted')
    monkeypatch.setattr(probe, '_backend_types', lambda: (binding, FakeBackend, FakeRunnerObservation))
    return calls, failures


def test_exact_runner_inspect_only_then_close(fake, capsys):
    calls, _ = fake
    assert probe.main(arguments()) == 0
    output = json.loads(capsys.readouterr().out)
    assert [call[0] for call in calls] == ['binding', 'constructor', 'inspect_runner', 'close']
    assert calls[2] == ('inspect_runner', 5000)
    assert output['ok'] and output['read_only'] and output['runner_only']
    for field in ('image_verified', 'profile_verified', 'deployment_verified', 'rollback_verified',
                  'driver_implementation_verified', 'variant_independently_verified'):
        assert output[field] is False
    assert '/private' not in json.dumps(output) and '/run' not in json.dumps(output)


@pytest.mark.parametrize('extra', [
    ['--runner-id', 'other-runner'], ['--socket=/other.sock'],
    ['--api-version', '1.50'], ['--timeout-ms=1000', '--timeout-ms', '5000'],
])
def test_duplicate_options_deny_without_backend_or_value_echo(fake, capsys, extra):
    calls, _ = fake
    assert probe.main(arguments() + extra) == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {
        'ok': False, 'code': 'invalid_probe_arguments', 'runner_only': True}


def test_arm_variant_and_bounded_explicit_timeout(fake, capsys):
    calls, _ = fake
    assert probe.main(arguments(architecture='arm64', variant='v8', **{'timeout-ms': '10000'})) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['platform'] == {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'}
    assert calls[2] == ('inspect_runner', 10000)


@pytest.mark.parametrize('changes', [
    {'runner-id': ''}, {'runner-id': 'secret with spaces'}, {'daemon-id': '../private'},
    {'daemon-id': 'x' * 129}, {'socket': 'docker.sock'}, {'socket': '/'},
    {'socket': '/run/../docker.sock'}, {'socket': '/run/secret\nvalue'},
    {'socket': 'C:\\private\\docker.sock'}, {'generation-root': '/run/docker.sock'},
    {'architecture': 'x86_64'}, {'architecture': 'windows'}, {'variant': 'v8'},
    {'api-version': '1.40'}, {'api-version': '1.041'}, {'api-version': '2.1'},
    {'timeout-ms': '0'}, {'timeout-ms': '99'}, {'timeout-ms': '10001'},
    {'timeout-ms': '5000.0'}, {'timeout-ms': 'True'}, {'timeout-ms': '05000'},
])
def test_strict_input_denies_before_backend_no_reflection(fake, capsys, changes):
    calls, _ = fake
    assert probe.main(arguments(**changes)) == 2
    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out) == {'ok': False, 'code': 'invalid_probe_arguments', 'runner_only': True}
    assert calls == []


@pytest.mark.parametrize('extra', [['--unexpected', 'sensitive-value'], ['--runner', 'sensitive-value']])
def test_unknown_or_abbreviated_flags_do_not_reflect_input(fake, capsys, extra):
    calls, _ = fake
    assert probe.main(arguments() + extra) == 2
    output = capsys.readouterr()
    assert 'sensitive-value' not in output.out + output.err
    assert calls == []


def test_missing_required_input_is_sanitized(fake, capsys):
    assert probe.main([]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)['code'] == 'invalid_probe_arguments' and output.err == ''
    assert fake[0] == []


def test_nonlinux_fails_closed_without_backend(fake, monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, 'platform', 'win32')
    assert probe.main(arguments()) == 1
    assert json.loads(capsys.readouterr().out)['code'] == 'linux_host_required'
    assert fake[0] == []


@pytest.mark.parametrize('failure', ['constructor', 'inspect', 'close', 'wrong_observation'])
def test_failure_is_sanitized_and_constructed_backend_closed(fake, capsys, failure):
    calls, failures = fake
    failures[failure] = True
    assert probe.main(arguments()) == 1
    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out) == {'ok': False, 'code': 'runner_probe_failed', 'runner_only': True}
    assert 'secret' not in captured.out and '/private' not in captured.out
    assert calls[-1][0] == ('constructor' if failure == 'constructor' else 'close')


def test_import_is_inert(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('import attempted external work')
    monkeypatch.setattr(probe, '_backend_types', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    original_import = builtins.__import__
    def no_backend_import(name, *args, **kwargs):
        if name.startswith('hub_container_executor'):
            raise AssertionError('import attempted Linux backend initialization')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_backend_import)
    path = Path(probe.__file__)
    spec = importlib.util.spec_from_file_location('hub_engine_runner_probe_import_only', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)
