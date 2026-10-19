"""Disposable Linux endpoint recovery, not installed service or job recovery."""
import asyncio
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import subprocess
import sys

import httpx
import pytest
from starlette.responses import JSONResponse

from engineering_hub.private_browser_socket import (
    PrivateBrowserSocketError, create_private_browser_client,
)
from engineering_hub.private_gateway import PublicBrowserGateway
from engineering_hub.server import _listener

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux crash/Unix socket boundary')


@pytest.fixture
def endpoint(tmp_path):
    directory = tmp_path / 'recover'
    directory.mkdir(mode=0o700)
    return directory / 'browser.sock'


def recover(path):
    from engineering_hub.recoverable_browser_socket import recoverable_browser_socket
    return recoverable_browser_socket(path)


def identity(path):
    value = path.lstat()
    return value.st_dev, value.st_ino, value.st_mode


def record_path(endpoint):
    return endpoint.with_name(endpoint.name + '.endpoint.json')


def lock_path(endpoint):
    return endpoint.with_name(endpoint.name + '.endpoint.lock')


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')


def read_line(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(8), 'Disposable endpoint subprocess did not become ready'
        raw = process.stdout.readline()
    assert raw, f'Disposable subprocess exited: {process.poll()}'
    return json.loads(raw)


@contextmanager
def process(script, endpoint):
    child = subprocess.Popen([sys.executable, '-u', '-c', script, str(endpoint)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, close_fds=True, start_new_session=True)
    try:
        yield child
    finally:
        # Every process in this group was created by this disposable fixture.
        # Also terminate fork probes if an assertion interrupted the parent.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=8)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


READY = '''
import json, sys
from pathlib import Path
from engineering_hub.recoverable_browser_socket import recoverable_browser_socket
path = Path(sys.argv[1])
with recoverable_browser_socket(path) as listener:
    print(json.dumps({'ready': True, 'inode': path.lstat().st_ino}), flush=True)
    sys.stdin.readline()
'''


def test_normal_close_reopen_keeps_lock_and_prior_record(endpoint):
    with recover(endpoint) as listener:
        assert listener.family == socket.AF_UNIX
        assert not listener.getblocking() and not listener.get_inheritable()
        assert stat.S_IMODE(endpoint.lstat().st_mode) == 0o600
        first_record = json.loads(record_path(endpoint).read_bytes())
        lock_identity = identity(lock_path(endpoint))
    assert not endpoint.exists()
    assert json.loads(record_path(endpoint).read_bytes()) == first_record
    assert identity(lock_path(endpoint)) == lock_identity
    with recover(endpoint) as listener:
        assert listener.fileno() >= 0
        assert stat.S_ISSOCK(endpoint.lstat().st_mode)
        assert json.loads(record_path(endpoint).read_bytes())['generation'] != first_record['generation']
        assert identity(lock_path(endpoint)) == lock_identity
    assert not endpoint.exists()
    assert set(p.name for p in endpoint.parent.iterdir()) == {
        lock_path(endpoint).name, record_path(endpoint).name}


def test_sigkill_owner_allows_exact_endpoint_recovery(endpoint):
    with process(READY, endpoint) as child:
        ready = read_line(child)
        assert ready['ready'] and child.poll() is None
        child.kill()
        assert child.wait(timeout=8) == -signal.SIGKILL
        before = identity(endpoint)
        old_record = json.loads(record_path(endpoint).read_bytes())
        lock_identity = identity(lock_path(endpoint))
        pinned = os.open(endpoint, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with recover(endpoint) as listener:
                assert listener.fileno() >= 0
                assert identity(endpoint) != before
                new_record = json.loads(record_path(endpoint).read_bytes())
                assert new_record['generation'] != old_record['generation']
                assert identity(lock_path(endpoint)) == lock_identity
        finally:
            os.close(pinned)
    assert not endpoint.exists()


def test_other_live_process_denied_without_touching_endpoint(endpoint):
    with process(READY, endpoint) as child:
        assert read_line(child)['ready']
        before = {p.name: (identity(p), p.read_bytes() if p.is_file() else None)
                  for p in endpoint.parent.iterdir()}
        with pytest.raises(PrivateBrowserSocketError):
            with recover(endpoint):
                pytest.fail('Competing live owner admitted')
        after = {p.name: (identity(p), p.read_bytes() if p.is_file() else None)
                 for p in endpoint.parent.iterdir()}
        assert before == after and child.poll() is None
        child.stdin.write('\n')
        child.stdin.flush()
        assert child.wait(timeout=8) == 0
    assert not endpoint.exists()


def test_legacy_socket_without_recovery_record_is_preserved(endpoint):
    legacy = socket.socket(socket.AF_UNIX)
    try:
        legacy.bind(str(endpoint))
        endpoint.chmod(0o600)
        before = identity(endpoint)
        with pytest.raises(PrivateBrowserSocketError):
            with recover(endpoint):
                pytest.fail('Unknown legacy socket adopted')
        assert identity(endpoint) == before
    finally:
        legacy.close()


@pytest.mark.parametrize('kind', ['regular', 'symlink'])
def test_changed_socket_leaf_is_preserved_on_exit_and_future_open(endpoint, kind):
    context = recover(endpoint)
    context.__enter__()
    endpoint.unlink()
    if kind == 'regular':
        endpoint.write_bytes(b'not endpoint-owned')
    else:
        endpoint.symlink_to(endpoint.parent / 'absent')
    before = identity(endpoint)
    with pytest.raises(PrivateBrowserSocketError):
        context.__exit__(None, None, None)
    assert identity(endpoint) == before
    with pytest.raises(PrivateBrowserSocketError):
        with recover(endpoint):
            pytest.fail('Changed endpoint accepted')
    assert identity(endpoint) == before


def test_changed_parent_is_not_cleaned_through_substituted_path(endpoint):
    context = recover(endpoint)
    context.__enter__()
    original = endpoint.parent.with_name('original')
    endpoint.parent.rename(original)
    endpoint.parent.mkdir(mode=0o700)
    endpoint.write_bytes(b'replacement directory')
    old_socket = identity(original / endpoint.name)
    replacement = identity(endpoint)
    with pytest.raises(PrivateBrowserSocketError):
        context.__exit__(None, None, None)
    assert identity(original / endpoint.name) == old_socket
    assert identity(endpoint) == replacement


@pytest.mark.parametrize('case', [
    'missing', 'malformed', 'oversized', 'extra', 'duplicate', 'version-bool',
    'identity-bool', 'generation', 'negative', 'too-large', 'parent', 'lock', 'socket',
])
def test_ambiguous_or_wrong_record_preserves_stale_socket(endpoint, case):
    with process(READY, endpoint) as child:
        assert read_line(child)['ready']
        child.kill()
        child.wait(timeout=8)
    record = record_path(endpoint)
    original = json.loads(record.read_bytes())
    if case == 'missing':
        record.unlink()
    elif case == 'malformed':
        record.write_bytes(b'{')
    elif case == 'oversized':
        record.write_bytes(b'x' * 4097)
    elif case == 'duplicate':
        raw = canonical(original)
        record.write_bytes(b'{"version":1,' + raw[1:])
    else:
        if case == 'extra':
            original['unknown'] = 1
        elif case == 'version-bool':
            original['version'] = True
        elif case == 'identity-bool':
            original['socket']['ino'] = True
        elif case == 'generation':
            original['generation'] = 'not-a-generation'
        elif case == 'negative':
            original['socket']['ctime_ns'] = -1
        elif case == 'too-large':
            original['socket']['ctime_ns'] = 2**64
        else:
            original[case]['ino'] += 1
        record.write_bytes(canonical(original))
    before = identity(endpoint)
    record_identity = identity(record) if record.exists() else None
    raw = record.read_bytes() if record.exists() else None
    with pytest.raises(PrivateBrowserSocketError):
        with recover(endpoint):
            pytest.fail('Ambiguous provenance accepted')
    assert identity(endpoint) == before
    assert (identity(record) if record.exists() else None) == record_identity
    assert (record.read_bytes() if record.exists() else None) == raw


@pytest.mark.parametrize('case', ['parent', 'lock', 'socket'])
def test_changed_actual_identity_preserves_unknown_objects(endpoint, case):
    with process(READY, endpoint) as child:
        assert read_line(child)['ready']
        child.kill()
        child.wait(timeout=8)
    held_socket = None
    if case == 'parent':
        original = endpoint.parent.with_name('old-parent')
        endpoint.parent.rename(original)
        endpoint.parent.mkdir(mode=0o700)
        for path in original.iterdir():
            path.rename(endpoint.parent / path.name)
    elif case == 'lock':
        lock_path(endpoint).rename(endpoint.parent / 'old-lock')
        lock_path(endpoint).touch(mode=0o600)
    else:
        endpoint.rename(endpoint.parent / 'old-socket')
        held_socket = socket.socket(socket.AF_UNIX)
        held_socket.bind(str(endpoint))
        endpoint.chmod(0o600)
    before = {p.name: identity(p) for p in endpoint.parent.iterdir()}
    try:
        with pytest.raises(PrivateBrowserSocketError):
            with recover(endpoint):
                pytest.fail('Substituted identity accepted')
        assert {p.name: identity(p) for p in endpoint.parent.iterdir()} == before
    finally:
        if held_socket is not None:
            held_socket.close()


@pytest.mark.parametrize('case', [
    'record-symlink', 'record-hardlink', 'record-weakmode',
    'lock-missing', 'lock-symlink', 'lock-nonempty', 'lock-weakmode',
    'record-missing',
])
def test_record_and_lock_file_policy_denies_without_repair(endpoint, case):
    with process(READY, endpoint) as child:
        assert read_line(child)['ready']
        child.kill()
        assert child.wait(timeout=8) == -signal.SIGKILL
    record = record_path(endpoint)
    lock = lock_path(endpoint)
    if case == 'record-symlink':
        saved = endpoint.parent / 'original-record'
        record.rename(saved)
        record.symlink_to(saved)
    elif case == 'record-hardlink':
        os.link(record, endpoint.parent / 'second-record-link')
    elif case == 'record-weakmode':
        record.chmod(0o640)
    elif case == 'lock-missing':
        lock.unlink()
    elif case == 'lock-symlink':
        saved = endpoint.parent / 'original-lock'
        lock.rename(saved)
        lock.symlink_to(saved)
    elif case == 'lock-nonempty':
        lock.write_bytes(b'not an empty endpoint lock')
    elif case == 'lock-weakmode':
        lock.chmod(0o640)
    else:
        record.unlink()

    def snapshot():
        result = {}
        for path in endpoint.parent.iterdir():
            info = path.lstat()
            value = None
            if stat.S_ISREG(info.st_mode):
                value = path.read_bytes()
            elif stat.S_ISLNK(info.st_mode):
                value = os.readlink(path)
            result[path.name] = (info.st_dev, info.st_ino, info.st_mode,
                                 info.st_uid, info.st_nlink, info.st_ctime_ns, value)
        return result

    before = snapshot()
    with pytest.raises(PrivateBrowserSocketError):
        with recover(endpoint):
            pytest.fail('Unsafe record/lock policy admitted')
    assert snapshot() == before


@pytest.mark.parametrize('published', [False, True])
def test_crash_publication_boundary_distinguishes_unknown_from_owned(endpoint, published):
    script = '''
import json, sys
from pathlib import Path
import engineering_hub.recoverable_browser_socket as module
original = module._publish_record
def block(lease, value, previous):
    if PUBLISHED:
        original(lease, value, previous)
    print(json.dumps({'ready': True}), flush=True)
    sys.stdin.readline()
module._publish_record = block
with module.recoverable_browser_socket(Path(sys.argv[1])):
    raise AssertionError('Publication probe released unexpectedly')
'''.replace('PUBLISHED', repr(published))
    with process(script, endpoint) as child:
        assert read_line(child)['ready']
        child.kill()
        child.wait(timeout=8)
    before = identity(endpoint)
    unknown_temporary = endpoint.parent / 'browser.sock.endpoint.json.operator.tmp'
    unknown_temporary.write_bytes(b'unknown temporary, not cleanup authority')
    if published:
        with recover(endpoint):
            assert endpoint.exists()
        assert not endpoint.exists()
    else:
        assert not record_path(endpoint).exists()
        with pytest.raises(PrivateBrowserSocketError):
            with recover(endpoint):
                pytest.fail('Unpublished socket adopted')
        assert identity(endpoint) == before
    assert unknown_temporary.read_bytes() == b'unknown temporary, not cleanup authority'


def test_fork_child_closes_inherited_capabilities_without_unlock_or_fd_reuse_damage(endpoint):
    script = '''
import errno, fcntl, json, os, sys, time
from pathlib import Path
import engineering_hub.recoverable_browser_socket as module
read_fd, write_fd = os.pipe()
context = module.recoverable_browser_socket(Path(sys.argv[1]))
listener = context.__enter__()
lease = next(iter(module._leases))
owned = [listener.fileno(), lease.lock_fd, lease.parent_fd]
child = os.fork()
if child == 0:
    try:
        os.close(read_fd)
        closed = []
        for fd in owned:
            try:
                os.fstat(fd)
            except OSError as exc:
                closed.append(exc.errno == errno.EBADF)
            else:
                closed.append(False)
        source = os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC)
        high = fcntl.fcntl(source, fcntl.F_DUPFD_CLOEXEC, 256)
        os.close(source)
        for fd in owned:
            os.dup2(high, fd, inheritable=False)
        context.__exit__(None, None, None)
        retained = [os.fstat(fd).st_rdev == os.fstat(high).st_rdev for fd in owned]
        for fd in owned:
            os.close(fd)
        os.close(high)
        os.write(write_fd, (json.dumps({'ready': True, 'child': os.getpid(),
            'closed': closed, 'retained': retained}) + '\\n').encode())
        os.close(write_fd)
        time.sleep(45)
        os._exit(0)
    except BaseException:
        os._exit(1)
os.close(write_fd)
with os.fdopen(read_fd) as incoming:
    result = incoming.readline()
assert result
print(result.strip(), flush=True)
sys.stdin.readline()
context.__exit__(None, None, None)
'''
    with process(script, endpoint) as owner:
        ready = read_line(owner)
        assert ready['ready'] and ready['closed'] == [True, True, True]
        assert ready['retained'] == [True, True, True]
        os.kill(ready['child'], 0)
        before = identity(endpoint)
        # Closing child copies must not LOCK_UN the still-live owner's OFD.
        with pytest.raises(PrivateBrowserSocketError):
            with recover(endpoint):
                pytest.fail('Fork child released live owner exclusivity')
        assert identity(endpoint) == before
        owner.kill()
        assert owner.wait(timeout=8) == -signal.SIGKILL
        os.kill(ready['child'], 0)
        # The child is alive, but no longer holds an inherited listener or lock.
        with recover(endpoint) as listener:
            assert listener.fileno() >= 0
            os.kill(ready['child'], 0)
    assert not endpoint.exists()


@pytest.mark.asyncio
async def test_actual_httpx_unix_connection_after_recovery(endpoint):
    with process(READY, endpoint) as child:
        assert read_line(child)['ready']
        child.kill()
        child.wait(timeout=8)
    async def app(scope, receive, send):
        await JSONResponse({'ready': 'synthetic'})(scope, receive, send)
    with recover(endpoint) as listener:
        server = _listener(app)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(8):
                while not server.started:
                    if serving.done():
                        await serving
                        pytest.fail('Disposable server failed startup')
                    await asyncio.sleep(0.001)
            async with create_private_browser_client(endpoint) as client:
                response = await client.get('http://fixture.invalid/probe')
                assert response.status_code == 200 and response.json() == {'ready': 'synthetic'}
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 8)
    assert not endpoint.exists()


@pytest.mark.asyncio
async def test_existing_gateway_client_reconnects_once_after_private_process_sigkill(endpoint):
    """Transport-only synthetic private app, not JWT, deployment, or reboot proof."""
    script = '''
import asyncio, json, sys
from pathlib import Path
from starlette.responses import JSONResponse
from engineering_hub.recoverable_browser_socket import recoverable_browser_socket
from engineering_hub.server import _listener
async def main():
    path = Path(sys.argv[1])
    with recoverable_browser_socket(path) as listener:
        record = path.with_name(path.name + '.endpoint.json')
        generation = json.loads(record.read_bytes())['generation']
        async def app(scope, receive, send):
            assert scope['method'] == 'GET'
            assert scope['path'] == '/api/hub/v1/tasks'
            await JSONResponse({'generation': generation})(scope, receive, send)
        server = _listener(app)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(8):
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError('Private server exited before readiness')
                await asyncio.sleep(0.001)
        print(json.dumps({'ready': True, 'generation': generation}), flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
'''
    with process(script, endpoint) as first:
        initial = read_line(first)
        assert initial['ready']
        async with create_private_browser_client(endpoint) as private_client:
            gateway = PublicBrowserGateway(private_client)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway),
                                        base_url='https://hub.example') as browser:
                before = await browser.get('/api/hub/v1/tasks')
                assert before.status_code == 200, before.text
                assert before.json() == {'generation': initial['generation']}
                first.kill()
                assert first.wait(timeout=8) == -signal.SIGKILL
                with process(script, endpoint) as second:
                    replacement = read_line(second)
                    assert replacement['ready']
                    assert replacement['generation'] != initial['generation']
                    # Exactly one application GET, using the original gateway,
                    # original private pool, and original browser client.
                    # A 502/unknown outcome is a failure, not an automatic retry.
                    after = await browser.get('/api/hub/v1/tasks')
                    assert after.status_code == 200, after.text
                    assert after.json() == {'generation': replacement['generation']}
