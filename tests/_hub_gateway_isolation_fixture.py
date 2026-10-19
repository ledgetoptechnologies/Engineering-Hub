"""Offline two-container fixture, never a deployment or host acceptance runner.

Only the private role constructs a real Hub owner/core/native queue. Deployment
effects and health evidence are synthetic. Shared same-UID socket access does
not authenticate peers or prove resilience against a hostile public container.
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
from pathlib import Path
import stat
import sys

BASE = '/api/hub/v1'
PRIVATE_ROOT = Path('/private')
PRIVATE_DATABASE = PRIVATE_ROOT / 'fixture/private/hub.sqlite3'
PRIVATE_CANARY = PRIVATE_ROOT / 'adapter-canary'
PRIVATE_RUNTIME = PRIVATE_ROOT / 'runtime.json'
PUBLIC_SESSION = Path('/tmp/hub-isolation-browser.json')


def emit(value):
    raw = json.dumps(value, separators=(',', ':'))
    if len(raw) > 16384:
        raise RuntimeError('Fixture response exceeded bound')
    print(raw, flush=True)


def load(path):
    raw = path.read_bytes()
    if len(raw) > 16384:
        raise RuntimeError('Fixture record exceeded bound')
    return json.loads(raw)


def save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, separators=(',', ':'))


def namespaces():
    return {'pid_namespace': os.readlink('/proc/self/ns/pid'),
            'mount_namespace': os.readlink('/proc/self/ns/mnt')}


def ready_path(role):
    return Path('/tmp/hub-isolation-' + role + '.json')


def socket_volume_inventory():
    def only_entry(directory, name):
        with os.scandir(directory) as entries:
            first = next(entries, None)
            assert first is not None and first.name == name
            assert next(entries, None) is None

    root = Path('/socket')
    only_entry(root, 'private')
    parent = root / 'private'
    parent_info = parent.lstat()
    assert stat.S_ISDIR(parent_info.st_mode)
    assert parent_info.st_uid == os.getuid()
    assert stat.S_IMODE(parent_info.st_mode) == 0o700
    only_entry(parent, 'browser.sock')
    leaf = (parent / 'browser.sock').lstat()
    assert stat.S_ISSOCK(leaf.st_mode)
    assert leaf.st_uid == os.getuid() and leaf.st_nlink == 1
    assert stat.S_IMODE(leaf.st_mode) == 0o600
    return True


def checked(response, expected=200):
    # Never include authentication response bodies, cookies, or credentials in
    # diagnostics. All values in this fixture are synthetic nonetheless.
    if response.status_code != expected:
        raise RuntimeError('Fixture HTTP status ' + str(response.status_code))
    return response.json()


async def private(args):
    import secrets
    import pytest
    from agent_message_bus.auth import AuthSettings
    from engineering_hub.app import Settings, create_app
    from engineering_hub.events import NativeHubEventFeed
    from engineering_hub.frontends import HubFrontends
    from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
    from engineering_hub.split_server import serve_private_coordinator
    from tests.test_hub_gateway_queued_pilot import signed_access
    from tests.test_hub_queued_pilot import rig as pilot_rig, machine_job
    from tests.test_hub_container_pilot import ACTIONS

    root = PRIVATE_ROOT / 'fixture'
    root.mkdir(mode=0o700)
    tokens = {agent: secrets.token_urlsafe(32) for agent in ('hermes', 'codex', 'claude')}
    canary = secrets.token_bytes(48)
    fd = os.open(PRIVATE_CANARY, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(canary)
    generator = None
    patcher = pytest.MonkeyPatch()
    config, assertion = signed_access(patcher)
    # Private keys exist only inside signed_access's private process; its
    # generated public assertion is the sole credential sent to the browser.
    save(PRIVATE_RUNTIME, {'tokens': tokens, 'assertion': assertion, 'case': args.case})
    auth = AuthSettings(tokens['hermes'], tokens['codex'], claude_token=tokens['claude'],
                        allowed_ips=frozenset({'127.0.0.1'}))

    def factory():
        nonlocal generator
        generator = pilot_rig.__wrapped__(root)
        rig = next(generator)
        if args.case == 'rollback':
            rig.client.outcomes['smoke_container'] = 'failed'
        feed = NativeHubEventFeed(rig.repo.path)
        runtime = QueuedPilotRuntime(rig.bridge, feed)
        original = rig.bridge.run_once

        async def record_completion():
            result = await original()
            if result is not None:
                actions = [command.action for command in rig.client.submissions]
                expected = ACTIONS if args.case == 'rollback' else ACTIONS[:2]
                assert actions == expected
                assert machine_job(rig) is None
                assert feed.available and runtime.status['state'] == 'active'
                run = rig.repo.list_runs()[0]
                assert run['status'] == ('rolled_back' if args.case == 'rollback' else 'succeeded')
                save(PRIVATE_ROOT / ('result-' + result['id'] + '.json'), {
                    'job_id': result['id'], 'state': result['state'],
                    'run_status': run['status'], 'actions': actions,
                    'native_feed': feed.available, 'machine_released': True})
            return result

        rig.bridge.run_once = record_completion
        core = create_app(Settings(rig.repo.path, auth, config), repository=rig.repo,
            event_feed=feed, work_queue=rig.queue, queued_pilot_runtime=runtime)
        return HubFrontends(core)

    def on_ready(agent, _):
        assert PRIVATE_DATABASE.is_file() and PRIVATE_DATABASE.stat().st_size > 0
        assert PRIVATE_CANARY.stat().st_size == 48
        save(ready_path('private'), {'ready': True, 'port': agent[1],
            'private_paths_present': True,
            'socket_volume_only_endpoint': socket_volume_inventory(), **namespaces()})
        emit({'ready': True, 'role': 'private'})

    try:
        await serve_private_coordinator(factory, socket_path=Path(args.socket),
            agent_host='127.0.0.1', agent_port=0, on_ready=on_ready)
    finally:
        if generator is not None:
            generator.close()
        patcher.undo()


async def public(args):
    from engineering_hub.private_browser_socket import create_private_browser_client
    from engineering_hub.split_server import serve_public_browser
    from engineering_hub.repository import HubRepository
    from engineering_hub.pilot_ownership import PilotOwner

    def forbidden(*_, **__):
        raise AssertionError('Public role must never construct private state')

    # Executable tripwires, not alternate repository implementations. The
    # public runner must remain solely a dedicated UDS HTTP client.
    HubRepository.__init__ = forbidden
    PilotOwner.__init__ = forbidden

    def on_ready(address):
        save(ready_path('public'), {'ready': True, 'port': address[1], **namespaces()})
        emit({'ready': True, 'role': 'public'})

    await serve_public_browser(lambda: create_private_browser_client(Path(args.socket)),
                               browser_port=0, on_ready=on_ready)


async def browser_start(args):
    import httpx
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise RuntimeError('Fixture input exceeded bound')
    assertion = json.loads(raw)['assertion']
    async with httpx.AsyncClient(base_url='http://127.0.0.1:' + str(args.port),
            trust_env=False, follow_redirects=False, headers={
                'Host': 'hub.example', 'Cf-Access-Jwt-Assertion': assertion}) as browser:
        response = await browser.get(BASE + '/auth/login')
        if response.status_code != 303:
            raise RuntimeError('Fixture login failed')
        cookie = browser.cookies.get('hub_session')
        if not cookie:
            raise RuntimeError('Fixture session missing')
        browser.headers['Cookie'] = 'hub_session=' + cookie
        human = checked(await browser.get(BASE + '/auth/me'))
        browser.headers.update({'Origin': 'https://hub.example', 'X-Hub-CSRF': human['csrf_token']})
        task = checked(await browser.post(BASE + '/tasks', json={
            'project': 'engineering-hub', 'title': 'Isolated synthetic ' + args.case,
            'client_request_id': 'isolated-browser-' + args.case}), 201)
        save(PUBLIC_SESSION, {'assertion': assertion, 'cookie': cookie})
        emit({'task_id': task['id'], 'revision': task['revision']})


async def agents(args):
    import httpx
    from tests.test_hub_queued_pilot import ACTION
    from tests.test_hub_container_pilot import COMMIT, IMAGE

    state = load(PRIVATE_RUNTIME)
    assert state['case'] == args.case
    async with httpx.AsyncClient(base_url='http://127.0.0.1:' + str(args.port),
            trust_env=False, headers={'Host': 'hub.example',
                'Authorization': 'Bearer ' + state['tokens']['codex']}) as client:
        path = BASE + '/tasks/' + args.task
        task = checked(await client.get(path))
        task = checked(await client.post(path + '/claim', json={'expected_revision': task['revision']}))
        task = checked(await client.post(path + '/candidate', json={
            'expected_revision': task['revision'], 'commit': COMMIT}))
        task = checked(await client.post(path + '/review',
            headers={'Authorization': 'Bearer ' + state['tokens']['claude']}, json={
                'expected_revision': task['revision'], 'commit': COMMIT, 'verdict': 'approve'}))
        job = checked(await client.post(BASE + '/work/jobs', json={
            'request_id': 'isolated-agents-' + args.case, 'action_key': ACTION,
            'environment': 'staging', 'target': {'task_id': task['id'],
                'source_commit': COMMIT, 'artifact_digest': IMAGE}}), 201)
        # Bounded test observation, not a scheduler/inbox/model watcher. Work is
        # executed by the private core's real native-event queue runtime.
        async with asyncio.timeout(15):
            while True:
                seen = checked(await client.get(BASE + '/work/jobs/' + job['id']))
                proof = PRIVATE_ROOT / ('result-' + job['id'] + '.json')
                if seen['state'] in {'succeeded', 'failed'} and proof.exists():
                    break
                await asyncio.sleep(0.01)
        expected = 'failed' if args.case == 'rollback' else 'succeeded'
        assert seen['state'] == expected
        result = load(proof)
        assert result['state'] == expected and result['native_feed'] and result['machine_released']
        emit(result)


async def browser_finish(args):
    import httpx
    state = load(PUBLIC_SESSION)
    async with httpx.AsyncClient(base_url='http://127.0.0.1:' + str(args.port),
            trust_env=False, headers={'Host': 'hub.example',
                'Cf-Access-Jwt-Assertion': state['assertion'],
                'Cookie': 'hub_session=' + state['cookie']}) as client:
        job = checked(await client.get(BASE + '/work/jobs/' + args.job))
        expected = 'failed' if args.case == 'rollback' else 'succeeded'
        assert job['state'] == expected
        emit({'verified': True, 'state': job['state'], 'job_id': job['id']})


def public_probe():
    socket_volume_only_endpoint = socket_volume_inventory()
    try:
        fd = os.open('/socket/private/public-write-probe',
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except OSError as error:
        assert error.errno == errno.EROFS
    else:
        os.close(fd)
        raise AssertionError('Public socket volume unexpectedly writable')
    for path in (PRIVATE_DATABASE, PRIVATE_CANARY, PRIVATE_RUNTIME):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            pass
        else:
            os.close(fd)
            raise AssertionError('Private canonical path visible to public container')
    names = {'CODEX_TOKEN', 'HERMES_TOKEN', 'CLAUDE_TOKEN', 'AGENT_TOKEN', 'GITHUB_TOKEN',
             'GH_TOKEN', 'CLOUDFLARE_API_TOKEN', 'CF_API_TOKEN', 'OPENAI_API_KEY'}
    assert not names.intersection(os.environ)
    # Numeric PIDs can coincide in distinct namespaces. Check actual visible
    # command lines for the private fixture role, not a coincident PID number.
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            command = (path / 'cmdline').read_bytes().split(b'\x00')
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        assert b'private' not in command
    emit({'private_paths_absent': True, 'private_role_visible': False,
          'no_agent_env': True, 'socket_volume_only_endpoint': socket_volume_only_endpoint,
          'public_socket_mount_readonly': True, **namespaces()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=['private', 'public', 'ready', 'assertion',
        'browser-start', 'agents', 'browser-finish', 'public-probe'])
    parser.add_argument('--socket', default='/socket/private/browser.sock')
    parser.add_argument('--case', choices=['healthy', 'rollback'], default='healthy')
    parser.add_argument('--role', dest='ready_role', choices=['private', 'public'])
    parser.add_argument('--port', type=int)
    parser.add_argument('--task')
    parser.add_argument('--job')
    args = parser.parse_args()
    if args.role == 'ready':
        emit(load(ready_path(args.ready_role)))
    elif args.role == 'assertion':
        emit({'assertion': load(PRIVATE_RUNTIME)['assertion']})
    elif args.role == 'public-probe':
        public_probe()
    else:
        asyncio.run({'private': private, 'public': public, 'browser-start': browser_start,
            'agents': agents, 'browser-finish': browser_finish}[args.role](args))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Offline isolation fixture failed', file=sys.stderr, flush=True)
        raise SystemExit(1) from None
