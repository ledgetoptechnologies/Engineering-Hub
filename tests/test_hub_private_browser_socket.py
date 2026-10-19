"""Real disposable Linux UDS, not an installed service or host mount boundary."""
import asyncio
import os
from pathlib import Path
import socket
import stat
import sys

import httpx
import pytest
from starlette.responses import JSONResponse

from agent_message_bus.auth import AuthSettings
from engineering_hub.cloudflare_access import CloudflareAccessConfig, CloudflareIdentity
from engineering_hub.private_browser_socket import (
    PrivateBrowserSocketError, private_browser_socket, create_private_browser_client,
)
from engineering_hub.private_gateway import PrivateBrowserGateway, PublicBrowserGateway
from engineering_hub.server import _listener

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux Unix socket boundary')


@pytest.fixture
def endpoint(tmp_path):
    directory = tmp_path / 'socket'
    directory.mkdir(mode=0o700)
    return directory / 'browser.sock'


def test_bound_socket_permissions_inert_client_and_normal_cleanup(endpoint):
    with private_browser_socket(endpoint) as listener:
        assert listener.family == socket.AF_UNIX
        assert not listener.getblocking() and not listener.get_inheritable()
        info = endpoint.lstat()
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid()
        assert stat.S_IMODE(endpoint.parent.stat().st_mode) == 0o700
    assert not endpoint.exists() and listener.fileno() == -1


@pytest.mark.parametrize('kind', ['file', 'symlink', 'dangling', 'fifo', 'socket'])
def test_existing_leaf_is_never_adopted_or_unlinked(endpoint, kind):
    foreign = None
    if kind == 'file':
        endpoint.touch()
    elif kind in {'symlink', 'dangling'}:
        target = endpoint.parent / 'target'
        if kind == 'symlink':
            target.touch()
        endpoint.symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(endpoint, 0o600)
    else:
        foreign = socket.socket(socket.AF_UNIX)
        foreign.bind(str(endpoint))
    before = endpoint.lstat()
    try:
        with pytest.raises(PrivateBrowserSocketError):
            with private_browser_socket(endpoint):
                pytest.fail('Existing object adopted')
        after = endpoint.lstat()
        assert (before.st_dev, before.st_ino, before.st_mode) == (after.st_dev, after.st_ino, after.st_mode)
    finally:
        if foreign is not None:
            foreign.close()


@pytest.mark.parametrize('mode', [0o755, 0o750, 0o770, 0o777])
def test_weak_parent_is_rejected_not_repaired(endpoint, mode):
    endpoint.parent.chmod(mode)
    with pytest.raises(PrivateBrowserSocketError):
        with private_browser_socket(endpoint):
            pytest.fail('Weak directory accepted')
    assert stat.S_IMODE(endpoint.parent.stat().st_mode) == mode
    assert not endpoint.exists()


def test_symlink_parent_and_noncanonical_paths_rejected(endpoint):
    alias = endpoint.parent.parent / 'alias'
    alias.symlink_to(endpoint.parent, target_is_directory=True)
    for path in [alias / endpoint.name, str(endpoint.parent) + '/../socket/browser.sock',
                 Path('relative.sock'), str(endpoint) + '\x00', endpoint.parent / ('x' * 108)]:
        with pytest.raises(PrivateBrowserSocketError):
            with private_browser_socket(path):
                pytest.fail('Invalid path accepted')
    assert not endpoint.exists()


@pytest.mark.parametrize('mode', [0o770, 0o777])
def test_untrusted_writable_ancestor_rejected_without_permission_repair(endpoint, mode):
    ancestor = endpoint.parent.parent / 'shared'
    ancestor.mkdir(mode=0o700)
    ancestor.chmod(mode)
    private = ancestor / 'private'
    private.mkdir(mode=0o700)
    path = private / 'browser.sock'
    with pytest.raises(PrivateBrowserSocketError):
        with private_browser_socket(path):
            pytest.fail('Writable ancestor accepted')
    assert stat.S_IMODE(ancestor.stat().st_mode) == mode
    assert stat.S_IMODE(private.stat().st_mode) == 0o700 and not path.exists()


@pytest.mark.parametrize('kind', ['file', 'symlink', 'socket'])
def test_replaced_leaf_is_preserved_on_cleanup(endpoint, kind):
    replacement = None
    with pytest.raises(PrivateBrowserSocketError):
        with private_browser_socket(endpoint):
            endpoint.unlink()  # Deliberate adversarial fixture mutation only.
            if kind == 'file':
                endpoint.touch()
            elif kind == 'symlink':
                endpoint.symlink_to(endpoint.parent / 'foreign')
            else:
                replacement = socket.socket(socket.AF_UNIX)
                replacement.bind(str(endpoint))
                endpoint.chmod(0o600)
            identity = endpoint.lstat().st_ino
    assert endpoint.lstat().st_ino == identity
    if replacement is not None:
        replacement.close()


def test_context_exception_closes_only_original_socket(endpoint):
    with pytest.raises(RuntimeError, match='synthetic caller'):
        with private_browser_socket(endpoint) as listener:
            raise RuntimeError('synthetic caller')
    assert listener.fileno() == -1 and not endpoint.exists()


def test_changed_parent_preserves_original_and_replacement_namespaces(endpoint):
    moved = endpoint.parent.parent / 'moved'
    with pytest.raises(PrivateBrowserSocketError):
        with private_browser_socket(endpoint) as listener:
            original = endpoint.lstat().st_ino
            endpoint.parent.rename(moved)
            endpoint.parent.mkdir(mode=0o700)
            endpoint.touch()
            replacement = endpoint.lstat().st_ino
    assert listener.fileno() == -1
    assert (moved / endpoint.name).lstat().st_ino == original
    assert endpoint.lstat().st_ino == replacement


def test_forked_child_cleanup_never_unlinks_parent_socket(endpoint):
    context = private_browser_socket(endpoint)
    listener = context.__enter__()
    try:
        original = endpoint.lstat().st_ino
        pid = os.fork()
        if pid == 0:
            try:
                context.__exit__(None, None, None)
                os._exit(0 if endpoint.lstat().st_ino == original else 2)
            except BaseException:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert endpoint.lstat().st_ino == original and listener.fileno() >= 0
    finally:
        context.__exit__(None, None, None)
    assert not endpoint.exists()


def test_client_requires_existing_secure_socket_not_tcp_or_regular_file(endpoint):
    for value in [endpoint, 'http://127.0.0.1:8765', Path('relative.sock')]:
        with pytest.raises(PrivateBrowserSocketError):
            create_private_browser_client(value)
    endpoint.touch()
    endpoint.chmod(0o600)
    with pytest.raises(PrivateBrowserSocketError):
        create_private_browser_client(endpoint)


@pytest.mark.skipif(not hasattr(os, 'geteuid') or os.geteuid() != 0,
                    reason='Synthetic owner-change check requires isolated CHOWN capability')
@pytest.mark.parametrize('kind', ['parent', 'ancestor', 'socket'])
def test_foreign_uid_is_rejected_not_repaired(endpoint, kind):
    if kind == 'socket':
        with pytest.raises(PrivateBrowserSocketError):
            with private_browser_socket(endpoint):
                os.chown(endpoint, 10002, 10002)
                with pytest.raises(PrivateBrowserSocketError):
                    create_private_browser_client(endpoint)
        assert endpoint.lstat().st_uid == 10002
        return
    directory = endpoint.parent if kind == 'parent' else endpoint.parent.parent
    original = directory.stat()
    try:
        os.chown(directory, 10002, 10002)
        with pytest.raises(PrivateBrowserSocketError):
            with private_browser_socket(endpoint):
                pytest.fail('Foreign owner accepted')
        assert directory.stat().st_uid == 10002
    finally:
        # Test-fixture cleanup only: CHOWN-only UID0 cannot traverse a foreign
        # 0700 ancestor. The library itself must never repair that ownership.
        os.chown(directory, original.st_uid, original.st_gid)
    assert not endpoint.exists()


@pytest.mark.asyncio
async def test_real_unix_http_gateway_preserves_browser_headers_and_denies_bearer(endpoint, monkeypatch):
    calls = []
    config = CloudflareAccessConfig('https://fixture-team.cloudflareaccess.com',
                                   'fixture-audience', ('beau@example.com',))
    class Checker:
        async def verify(self, assertion):
            assert assertion == 'synthetic-assertion'
            return CloudflareIdentity('fixture', 'beau@example.com', 'human:cf:fixture', 2**31)
    monkeypatch.setattr('engineering_hub.ingress.CloudflareAccessVerifier', lambda _: Checker())
    async def core(scope, receive, send):
        calls.append(scope)
        headers = dict(scope['headers'])
        assert headers[b'host'] == b'hub.example'
        assert headers[b'origin'] == b'https://hub.example'
        assert headers[b'cookie'] == b'hub_session=original'
        assert headers[b'x-hub-csrf'] == b'original-csrf'
        await JSONResponse({'items': []})(scope, receive, send)
    receiver = PrivateBrowserGateway(core, agent_auth=AuthSettings('hermes', 'codex'),
                                     cloudflare_config=config)
    with private_browser_socket(endpoint) as listener:
        server = _listener(receiver)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                        pytest.fail('Server exited before readiness')
                    await asyncio.sleep(0.001)
            async with create_private_browser_client(endpoint) as client:
                gateway = PublicBrowserGateway(client)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway),
                        base_url='https://hub.example') as browser:
                    headers = {'Cf-Access-Jwt-Assertion': 'synthetic-assertion',
                        'Origin': 'https://hub.example', 'Cookie': 'hub_session=original',
                        'X-Hub-CSRF': 'original-csrf'}
                    response = await browser.get('/api/hub/v1/tasks', headers=headers)
                    assert response.status_code == 200 and response.json() == {'items': []}
                    denied = await browser.get('/api/hub/v1/tasks',
                        headers={**headers, 'Authorization': 'Bearer codex'})
                    assert denied.status_code == 403 and len(calls) == 1
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
    assert not endpoint.exists()
