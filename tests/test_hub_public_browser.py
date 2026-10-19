"""Standalone public CLI contracts without launching services or private state."""
import asyncio
import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from engineering_hub import public_browser as module


@pytest.fixture(autouse=True)
def preserve_pytest_loop_policy(monkeypatch):
    # pytest-asyncio installs an idle policy loop after asynchronous tests.
    # Standalone asyncio.run() clears that reference without owning/closing
    # it. Exercise the CLI's own loop in a Runner that does not replace that
    # caller-owned policy loop; the real installed command is tested in Docker.
    def run(awaitable):
        with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
            return runner.run(awaitable)
    monkeypatch.setattr(asyncio, 'run', run)


@pytest.mark.parametrize('path', [None, b'/socket/browser.sock', '', 'relative.sock',
    '//socket/browser.sock', '/socket/../browser.sock', '/socket//browser.sock',
    '/socket/browser.sock/', '/', '/socket/.hidden', '/socket/a\x00b',
    '/socket/' + 'a' * 65, '/' + 'a' * 100 + '/browser.sock', '/socket/\ud800.sock'])
def test_invalid_socket_never_starts_runtime(monkeypatch, path):
    monkeypatch.setitem(sys.modules, 'engineering_hub.split_server', SimpleNamespace())
    with pytest.raises(ValueError, match='explicit normalized absolute'):
        asyncio.run(module.run(path))


@pytest.mark.parametrize('port', [None, True, '8771', 0, -1, 65536])
def test_invalid_port_never_imports_runner(monkeypatch, port):
    monkeypatch.setitem(sys.modules, 'engineering_hub.split_server', SimpleNamespace())
    with pytest.raises(ValueError, match='port must be between'):
        asyncio.run(module.run('/socket/browser.sock', port))


def test_run_delegates_only_dedicated_client_after_runner_owns_startup(monkeypatch):
    calls = []
    sentinel = object()

    def client(path):
        assert calls == [('runner', 8771)]
        calls.append(('client', path))
        return sentinel

    async def serve(factory, *, browser_port):
        assert calls == []
        calls.append(('runner', browser_port))
        assert factory() is sentinel

    monkeypatch.setitem(sys.modules, 'engineering_hub.private_browser_socket',
                        SimpleNamespace(create_private_browser_client=client))
    monkeypatch.setitem(sys.modules, 'engineering_hub.split_server',
                        SimpleNamespace(serve_public_browser=serve))
    asyncio.run(module.run('/socket/browser.sock'))
    assert calls == [('runner', 8771), ('client', '/socket/browser.sock')]


@pytest.mark.parametrize('args', [[], ['--socket', 'relative'],
    ['--socket', '/socket/browser.sock', '--host', 'synthetic-secret'],
    ['--socket', '/socket/browser.sock', '--port', 'synthetic-secret'],
    ['--socket', '/socket/browser.sock', '--port', '0'],
    ['--socket', '/socket/browser.sock', '--factory', 'synthetic-secret']])
def test_cli_invalid_arguments_are_inert_and_do_not_echo_values(monkeypatch, capsys, args):
    monkeypatch.setattr(module, 'run', lambda *a: pytest.fail('No runtime on invalid args'))
    with pytest.raises(SystemExit) as caught:
        module.main(args)
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert 'synthetic-secret' not in output.out + output.err


def test_main_uses_explicit_arguments_not_private_environment(monkeypatch, capsys):
    calls = []

    async def run(path, port):
        calls.append((path, port))

    monkeypatch.setattr(module, 'run', run)
    for key in ('HUB_DB', 'CODEX_TOKEN', 'HERMES_TOKEN', 'CLAUDE_TOKEN', 'HUB_GITHUB_CLIENT_SECRET'):
        monkeypatch.setenv(key, 'synthetic-private-env-not-a-configuration-source')
    assert module.main(['--socket', '/socket/browser.sock', '--port', '8772']) == 0
    assert calls == [('/socket/browser.sock', 8772)]
    assert capsys.readouterr().out == ''


def test_startup_failure_does_not_disclose_exception_or_path(monkeypatch, capsys):
    async def run(*args):
        raise RuntimeError('synthetic-private-secret at /socket/browser.sock')

    monkeypatch.setattr(module, 'run', run)
    assert module.main(['--socket', '/socket/browser.sock']) == 1
    result = capsys.readouterr()
    assert result.out == ''
    assert result.err == 'Engineering Hub public browser unavailable.\n'


def test_help_and_import_do_not_start_runtime(monkeypatch, capsys):
    monkeypatch.setattr(asyncio, 'run', lambda *a: pytest.fail('No service on import/help'))
    importlib.reload(module)
    with pytest.raises(SystemExit) as caught:
        module.main(['--help'])
    assert caught.value.code == 0
    assert '--socket' in capsys.readouterr().out


def test_environment_is_not_consulted_when_importing_or_parsing(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('No environment configuration lookup')

    monkeypatch.setattr(os, 'getenv', forbidden)
    importlib.reload(module)
    assert module._socket_path('/socket/browser.sock') == '/socket/browser.sock'
    assert module._port(8771) == 8771


def test_public_runner_import_does_not_load_private_application_dependencies():
    script = '''
import sys
sys.path.insert(0, sys.argv[1])
blocked = {'engineering_hub.app', 'engineering_hub.frontends',
           'engineering_hub.repository', 'engineering_hub.pilot_ownership'}
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise AssertionError('Public runner imported private application')
sys.meta_path.insert(0, Guard())
import engineering_hub.public_browser
from engineering_hub.split_server import serve_public_browser
from engineering_hub.private_browser_socket import create_private_browser_client
assert not blocked.intersection(sys.modules)
print('public-only imports verified')
'''
    result = subprocess.run([sys.executable, '-c', script,
        str(Path(__file__).parents[1] / 'src')], capture_output=True, text=True,
        timeout=15, check=True)
    assert result.stdout.strip() == 'public-only imports verified'


def test_private_compatibility_export_is_the_same_cleanup_function():
    from engineering_hub.lifecycle import finish_owned
    from engineering_hub.owned_tasks import finish_owned as compatibility_export
    assert finish_owned is compatibility_export
