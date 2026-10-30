from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import pytest

from hub_staging_service import runtime_inventory as inventory


NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def container(**changes):
    return {'id': 'a' * 64, 'name': 'example-web', 'image': 'example/image:latest',
            'state': 'running', **changes}


def wire(*rows):
    return b'\n'.join(json.dumps(row).encode() for row in rows)


class Backend:
    def __init__(self, raw=b'', error=None):
        self.raw, self.error, self.calls = raw, error, []

    def _checked_output(self, parts, **kwargs):
        self.calls.append((parts, kwargs))
        if self.error:
            raise self.error
        return self.raw


def host_snapshot(now=NOW):
    def capture(command):
        if command == inventory.HOST_DOCKER_COMMAND:
            return wire(container())
        assert command == inventory.HOST_SERVICES_COMMAND
        return json.dumps([{'unit': 'viewer-staging.service', 'active': 'active',
                            'sub': 'running', 'description': 'NEVER_EXPOSE'}]).encode()
    return inventory.collect_host(now=now, capture=capture)


def test_worker_uses_fixed_bounded_command_and_never_claims_ownership():
    backend = Backend(wire(container(name='eh-stage-untrusted-label')))
    result = inventory.collect_runtime(backend, now=NOW)
    assert backend.calls == [(inventory.DOCKER_ARGS, {'limit': 131072, 'timeout': 8})]
    assert result['containers'][0] == {**container(name='eh-stage-untrusted-label'),
                                       'scope': 'worker', 'managed': False}
    assert result['sources'] == [
        {'id': 'worker', 'state': 'observed', 'observed_at': '2026-10-25T12:00:00Z'},
        {'id': 'host', 'state': 'not_configured', 'observed_at': None}]
    assert 'Command' not in inventory.DOCKER_FORMAT
    assert '.Labels' not in inventory.DOCKER_FORMAT
    assert '{{json .}}' not in inventory.DOCKER_FORMAT


def test_failed_worker_is_unavailable_not_healthy_empty():
    result = inventory.collect_runtime(Backend(error=RuntimeError('SECRET')), now=NOW)
    assert result['sources'][0]['state'] == 'unavailable'
    assert result['containers'] == []
    assert 'SECRET' not in json.dumps(result)


def test_successful_empty_worker_is_distinct_from_unavailable():
    result = inventory.collect_runtime(Backend(), now=NOW)
    assert result['sources'][0]['state'] == 'observed'
    assert result['containers'] == []


@pytest.mark.parametrize('changes', [
    {'id': 'a' * 63}, {'id': 'not-an-id'}, {'name': '../escape'},
    {'name': '<script>alert(1)</script>'}, {'name': 'x' * 129},
    {'image': 'x\nsecret'}, {'image': 'x' * 513}, {'state': 'exited'},
    {'state': 'running\nsecret'}, {'Command': 'password=SECRET'}, {'id': 5}])
def test_invalid_container_fields_reject_entire_observation(changes):
    result = inventory.collect_runtime(Backend(wire(container(**changes))), now=NOW)
    assert result['sources'][0]['state'] == 'unavailable'
    assert result['containers'] == []


def test_duplicate_keys_ids_oversize_and_count_are_rejected():
    bad = [b'{"id":"a","id":"b"}', wire(container(), container()),
           b' ' * (inventory.MAX_BYTES + 1),
           wire(*(container(id=f'{n:064x}', name=f'job-{n}') for n in range(257)))]
    for raw in bad:
        with pytest.raises(ValueError):
            inventory.parse_containers(raw, 'worker')


def test_host_collection_keeps_only_runtime_metadata():
    result = host_snapshot()
    assert result['sources'][0]['state'] == 'observed'
    assert result['services'] == [{'name': 'viewer-staging.service', 'state': 'running', 'scope': 'host'}]
    assert 'NEVER_EXPOSE' not in json.dumps(result)
    assert result['containers'][0]['managed'] is False


def test_host_partial_failure_discards_partial_data():
    def capture(command):
        if command == inventory.HOST_DOCKER_COMMAND:
            return wire(container())
        raise RuntimeError('SECRET')
    result = inventory.collect_host(now=NOW, capture=capture)
    assert result['sources'] == [{'id': 'host', 'state': 'unavailable', 'observed_at': None}]
    assert result['containers'] == result['services'] == []


@pytest.mark.parametrize('row', [
    {'unit': '../x.service', 'active': 'active', 'sub': 'running'},
    {'unit': 'x.service', 'active': 'inactive', 'sub': 'dead'},
    {'unit': 'x.socket', 'active': 'active', 'sub': 'running'},
    {'unit': 'x\nSECRET.service', 'active': 'active', 'sub': 'running'}])
def test_malformed_services_rejected(row):
    with pytest.raises(ValueError):
        inventory.parse_services(json.dumps([row]).encode())


def test_duplicate_services_rejected():
    row = {'unit': 'x.service', 'active': 'active', 'sub': 'running'}
    with pytest.raises(ValueError):
        inventory.parse_services(json.dumps([row, row]).encode())


def test_current_host_snapshot_joins_without_paths_or_authority(monkeypatch):
    monkeypatch.setattr(inventory, '_read_host', lambda path: host_snapshot())
    result = inventory.collect_runtime(Backend(), '/private/path/never-expose', now=NOW)
    assert result['sources'][1]['state'] == 'observed'
    assert len(result['containers']) == len(result['services']) == 1
    assert '/private/path' not in json.dumps(result)
    assert result['containers'][0]['managed'] is False


def test_stale_host_snapshot_has_no_current_rows(monkeypatch):
    monkeypatch.setattr(inventory, '_read_host', lambda path: host_snapshot(NOW - timedelta(seconds=91)))
    result = inventory.collect_runtime(Backend(), '/snapshot', now=NOW)
    assert result['sources'][1] == {'id': 'host', 'state': 'stale',
                                   'observed_at': '2026-10-25T11:58:29Z'}
    assert result['containers'] == result['services'] == []


def test_future_host_snapshot_unavailable(monkeypatch):
    monkeypatch.setattr(inventory, '_read_host', lambda path: host_snapshot(NOW + timedelta(seconds=11)))
    result = inventory.collect_runtime(Backend(), '/snapshot', now=NOW)
    assert result['sources'][1]['state'] == 'unavailable'


@pytest.mark.parametrize('mutator', [
    lambda s: s.update(secret='SECRET'),
    lambda s: s['containers'][0].update(managed=True),
    lambda s: s['containers'][0].update(command='SECRET'),
    lambda s: s['containers'][0].update(scope='worker'),
    lambda s: s['services'][0].update(name='bad/path.service'),
    lambda s: s['sources'][0].update(state='healthy'),
    lambda s: s.update(observed_at='2026-10-25T12:00:00'),
])
def test_host_snapshot_shape_is_strict(monkeypatch, mutator):
    snapshot = host_snapshot()
    mutator(snapshot)
    monkeypatch.setattr(inventory, '_read_host', lambda path: snapshot)
    result = inventory.collect_runtime(Backend(), '/snapshot', now=NOW)
    assert result['sources'][1]['state'] == 'unavailable'
    assert result['containers'] == result['services'] == []


def test_combined_inventory_limit_refuses_second_source(monkeypatch):
    monkeypatch.setattr(inventory, '_read_host', lambda path: host_snapshot())
    raw = wire(*(container(id=f'{n:064x}', name=f'job-{n}') for n in range(256)))
    result = inventory.collect_runtime(Backend(raw), '/snapshot', now=NOW)
    assert result['sources'][0]['state'] == 'observed'
    assert result['sources'][1]['state'] == 'unavailable'
    assert len(result['containers']) == 256
    assert result['services'] == []


def test_capture_refuses_arbitrary_command_before_process(monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('must not start'))
    with pytest.raises(ValueError, match='fixed_inventory_command_required'):
        inventory.capture_fixed(('/bin/sh', '-c', 'arbitrary'))


class CapturedProcess:
    def __init__(self, raw, running=False):
        self.stdout = io.BytesIO(raw)
        self.returncode = None if running else 0
        self.killed = False
        self.wait_count = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout):
        self.wait_count += 1
        return self.returncode


def test_capture_is_bounded_and_sanitizes_child_environment(monkeypatch):
    child = CapturedProcess(b'x' * (inventory.MAX_BYTES + 1))
    calls = []
    def start(command, **kwargs):
        calls.append((command, kwargs))
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    with pytest.raises(ValueError, match='inventory_unavailable'):
        inventory.capture_fixed(inventory.HOST_DOCKER_COMMAND)
    assert calls[0][0] == inventory.HOST_DOCKER_COMMAND
    assert calls[0][1]['stderr'] == subprocess.DEVNULL
    assert 'DOCKER_HOST' not in calls[0][1]['env']
    assert calls[0][1]['env']['HOME'] == '/nonexistent'


def test_capture_deadline_kills_only_retained_child(monkeypatch):
    child = CapturedProcess(b'', running=True)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: child)
    ticks = iter([0, 9])
    monkeypatch.setattr(inventory.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(ValueError, match='inventory_unavailable'):
        inventory.capture_fixed(inventory.HOST_DOCKER_COMMAND)
    assert child.killed


@pytest.mark.parametrize('failure', ['constructor', 'start'])
def test_reader_creation_failure_kills_reaps_and_closes_child(monkeypatch, failure):
    child = CapturedProcess(b'NO_LEAK', running=True)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: child)

    class FailingReader:
        def __init__(self, **kwargs):
            if failure == 'constructor':
                raise RuntimeError('reader unavailable')

        def start(self):
            raise RuntimeError('reader unavailable')

        def join(self, **kwargs):
            pytest.fail('unstarted reader must not be joined')

    monkeypatch.setattr(inventory.threading, 'Thread', FailingReader)
    with pytest.raises(ValueError, match='inventory_unavailable'):
        inventory.capture_fixed(inventory.HOST_DOCKER_COMMAND)
    assert child.killed
    assert child.wait_count == 1
    assert child.stdout.closed


def test_wait_failure_still_joins_started_reader(monkeypatch):
    child = CapturedProcess(b'')
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: child)
    joined = []

    class StartedReader:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def join(self, **kwargs):
            joined.append(True)

        def is_alive(self):
            return False

    def failed_wait(timeout):
        child.wait_count += 1
        raise subprocess.TimeoutExpired('fixed inventory', timeout)

    monkeypatch.setattr(inventory.threading, 'Thread', StartedReader)
    child.wait = failed_wait
    with pytest.raises(ValueError, match='inventory_unavailable'):
        inventory.capture_fixed(inventory.HOST_DOCKER_COMMAND)
    assert child.wait_count == 2
    assert joined == [True]
    assert child.stdout.closed


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: -1)() != 0,
                    reason='root POSIX descriptor/protection acceptance')
def test_secure_snapshot_real_write_read_and_replace():
    # /tmp is intentionally not accepted as a secure ancestor; use root's home.
    with tempfile.TemporaryDirectory(prefix='eh-runtime-test-', dir='/root') as name:
        path = Path(name) / 'runtime.json'
        snapshot = host_snapshot(datetime.now(timezone.utc))
        inventory.publish_host(snapshot, path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        assert inventory._read_host(path) == snapshot
        inventory.publish_host(snapshot, path)
        assert list(Path(name).iterdir()) == [path]


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: -1)() != 0,
                    reason='root POSIX publication protection')
def test_collector_will_not_overwrite_unrelated_root_owned_file():
    with tempfile.TemporaryDirectory(prefix='eh-runtime-test-', dir='/root') as name:
        path = Path(name) / 'runtime.json'
        path.write_text('UNRELATED_DATA')
        path.chmod(0o644)
        with pytest.raises((ValueError, OSError)):
            inventory.publish_host(host_snapshot(datetime.now(timezone.utc)), path)
        assert path.read_text() == 'UNRELATED_DATA'
        assert list(Path(name).iterdir()) == [path]


@pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor protections')
def test_writable_ancestor_is_rejected(tmp_path):
    parent = tmp_path / 'untrusted'
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    with pytest.raises((ValueError, OSError)):
        inventory._secure_directory(parent)


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: -1)() != 0,
                    reason='root POSIX snapshot protection')
@pytest.mark.parametrize('kind', ['symlink', 'world-writable', 'hardlink', 'fifo', 'wrong-owner'])
def test_unsafe_snapshot_files_are_rejected(kind):
    with tempfile.TemporaryDirectory(prefix='eh-runtime-test-', dir='/root') as name:
        path = Path(name) / 'runtime.json'
        path.write_text(json.dumps(host_snapshot()))
        path.chmod(0o644)
        if kind == 'symlink':
            other = Path(name) / 'other.json'
            path.rename(other)
            path.symlink_to(other)
        elif kind == 'world-writable':
            path.chmod(0o666)
        elif kind == 'hardlink':
            os.link(path, Path(name) / 'alias')
        elif kind == 'fifo':
            path.unlink()
            os.mkfifo(path)
        else:
            os.chown(path, 10001, 10001)
        with pytest.raises((ValueError, OSError)):
            inventory._read_host(path)


def test_collector_script_has_no_endpoint_or_arbitrary_command_options():
    source = Path('src/hub_staging_service/collector.py').read_text()
    assert "add_argument('--output'" in source
    assert "add_argument('--once'" in source
    assert "sleep(30)" in source
    assert 'Popen' not in source
    assert 'shell=True' not in source



def test_collector_wheel_entrypoint_uses_single_packaged_implementation():
    import tomllib
    from hub_staging_service import collector

    package = tomllib.loads(Path('services/staging/pyproject.toml').read_text())
    assert package['project']['scripts']['hub-staging-runtime-collector'] == 'hub_staging_service.collector:main'
    assert 'hub_staging_service' in package['tool']['setuptools']['packages']
    assert callable(collector.main)
    launcher = Path('scripts/collect_staging_runtime.py').read_text()
    assert 'from hub_staging_service.collector import main' in launcher
    assert 'collect_host' not in launcher and 'publish_host' not in launcher

@pytest.mark.parametrize(('failure', 'expected'), [
    ('docker_capture', 'staging_runtime_docker_capture_failed'),
    ('docker_parse', 'staging_runtime_docker_parse_failed'),
    ('service_capture', 'staging_runtime_service_capture_failed'),
    ('service_parse', 'staging_runtime_service_parse_failed'),
])
def test_host_failure_diagnostic_is_fixed_and_snapshot_fail_closed(failure, expected):
    secret = 'SECRET /private/path credential-and-service-name'
    messages = []
    def capture(command):
        if command == inventory.HOST_DOCKER_COMMAND:
            if failure == 'docker_capture':
                raise RuntimeError(secret)
            return secret.encode() if failure == 'docker_parse' else wire(container())
        if failure == 'service_capture':
            raise RuntimeError(secret)
        return secret.encode() if failure == 'service_parse' else b'[]'
    result = inventory.collect_host(now=NOW, capture=capture, diagnostic=messages.append)
    assert messages == [expected]
    assert result == {'schema': inventory.SCHEMA, 'observed_at': inventory._timestamp(NOW),
                      'sources': [inventory._source('host', 'unavailable')],
                      'containers': [], 'services': []}
    assert secret not in json.dumps(result) + str(messages)


def test_host_success_has_no_failure_diagnostic_and_diagnostic_failure_is_safe():
    messages = []
    result = inventory.collect_host(now=NOW, capture=lambda command: b'' if command == inventory.HOST_DOCKER_COMMAND else b'[]',
                                    diagnostic=messages.append)
    assert result['sources'][0]['state'] == 'observed'
    assert messages == []
    def broken(_):
        raise RuntimeError('SECRET')
    result = inventory.collect_host(now=NOW, capture=lambda command: (_ for _ in ()).throw(ValueError('SECRET')),
                                    diagnostic=broken)
    assert result['sources'][0]['state'] == 'unavailable'
    assert 'SECRET' not in json.dumps(result)


def test_collector_emits_only_fixed_stage_to_stderr(monkeypatch, capsys):
    from hub_staging_service import collector
    monkeypatch.setattr(collector.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(sys, 'argv', ['collector', '--output', '/synthetic/path', '--once'])
    monkeypatch.setattr(collector, 'publish_host', lambda snapshot, path: None)
    def collect(*, diagnostic):
        diagnostic('staging_runtime_docker_capture_failed')
        return {}
    monkeypatch.setattr(collector, 'collect_host', collect)
    assert collector.main() == 0
    assert capsys.readouterr().err == 'staging_runtime_docker_capture_failed\n'


def test_host_snapshot_size_failure_has_fixed_diagnostic(monkeypatch):
    monkeypatch.setattr(inventory, 'MAX_BYTES', 8)
    codes = []
    result = inventory.collect_host(now=NOW, capture=lambda command: b'' if command == inventory.HOST_DOCKER_COMMAND else b'[]',
                                    diagnostic=codes.append)
    assert codes == ['staging_runtime_snapshot_size_failed']
    assert result['sources'] == [inventory._source('host', 'unavailable')]
    assert result['containers'] == result['services'] == []


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: -1)() != 0,
                    reason='requires root POSIX protected temporary directory')
def test_collector_main_publishes_real_fail_closed_snapshot(monkeypatch, capsys):
    from hub_staging_service import collector
    secret = 'SECRET /private/path credential-and-service-name'
    with tempfile.TemporaryDirectory(prefix='eh-runtime-test-', dir='/root') as name:
        path = Path(name) / 'snapshot.json'
        monkeypatch.setattr(sys, 'argv', ['collector', '--output', str(path), '--once'])
        def capture(command):
            assert command == inventory.HOST_DOCKER_COMMAND
            raise RuntimeError(secret)
        def real_collect(*, diagnostic):
            return inventory.collect_host(now=NOW, capture=capture, diagnostic=diagnostic)
        monkeypatch.setattr(collector, 'collect_host', real_collect)
        assert collector.main() == 0
        output = capsys.readouterr()
        assert output.out == ''
        assert output.err == 'staging_runtime_docker_capture_failed\n'
        snapshot = json.loads(path.read_text())
        assert snapshot['sources'] == [inventory._source('host', 'unavailable')]
        assert snapshot['containers'] == snapshot['services'] == []
        assert secret not in output.err + path.read_text()

@pytest.mark.parametrize('kind', ['spawn', 'timeout', 'overflow', 'reader', 'exit'])
def test_capture_failure_subcodes_are_fixed_and_snapshot_stays_unavailable(monkeypatch, kind):
    secret = 'SECRET-command-output-and-path'
    child = CapturedProcess(b'x' * 5 if kind == 'overflow' else b'', running=kind == 'timeout')
    if kind == 'exit':
        child.returncode = 17
    if kind == 'reader':
        class BrokenStream(io.BytesIO):
            def read(self, size):
                raise OSError(secret)
        child.stdout = BrokenStream()
    def start(*args, **kwargs):
        if kind == 'spawn':
            raise OSError(secret)
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    if kind == 'timeout':
        ticks = iter([0, 9])
        monkeypatch.setattr(inventory.time, 'monotonic', lambda: next(ticks))
    messages = []
    def capture(command):
        return inventory.capture_fixed(command, maximum=4)
    result = inventory.collect_host(now=NOW, capture=capture, diagnostic=messages.append)
    assert messages == ['staging_runtime_docker_capture_failed_' + kind]
    assert result['sources'] == [inventory._source('host', 'unavailable')]
    assert result['containers'] == result['services'] == []
    assert secret not in str(messages) + json.dumps(result)


def test_capture_failure_service_subcode_and_untrusted_error_fallback():
    messages = []
    def capture(command):
        if command == inventory.HOST_DOCKER_COMMAND:
            return b''
        raise inventory._CaptureFailure('exit')
    result = inventory.collect_host(now=NOW, capture=capture, diagnostic=messages.append)
    assert messages == ['staging_runtime_service_capture_failed_exit']
    assert result['sources'] == [inventory._source('host', 'unavailable')]
    messages.clear()
    def untrusted(command):
        raise RuntimeError('SECRET')
    inventory.collect_host(now=NOW, capture=untrusted, diagnostic=messages.append)
    assert messages == ['staging_runtime_docker_capture_failed']
