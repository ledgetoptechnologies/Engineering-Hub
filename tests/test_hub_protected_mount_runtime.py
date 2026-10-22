"""Explicit disposable mount/runtime acceptance, not NAS or migration evidence."""
import asyncio
import os
from pathlib import Path
import sys

import httpx
import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or
    os.environ.get('EH_DISPOSABLE_PRIVATE_RUNTIME_TEST') != '1',
    reason='Requires explicit dedicated disposable mount invocation')


@pytest.mark.asyncio
async def test_fixed_mount_owned_restart_preserves_tasks_and_shared_state(tmp_path, monkeypatch):
    from agent_message_bus.auth import AuthSettings
    from agent_message_bus.database import MessageRepository
    from engineering_hub.app import Settings
    from engineering_hub import owned_inactive_hub as service, protected_layout as layout
    from engineering_hub.pilot_ownership import PilotOwner
    from engineering_hub.repository import HubRepository
    from tests.test_hub_gateway_queued_pilot import signed_access, BASE, ORIGIN

    root = Path('/var/lib/hub-private')
    rows = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    assert any(row[4] == str(root) and row[row.index('-') + 1] == 'tmpfs' for row in rows)
    assert not list(root.iterdir()), 'Never adopt existing files in this disposable fixture'
    database = layout.database_path()
    # Explicit synthetic enrollment only, with the existing TEST-only filesystem
    # allowance. Production owner guards and installed selectors are unchanged.
    with PilotOwner(database, allow_ephemeral_test_storage=True) as owner:
        repo = HubRepository(database, pilot_owner=owner)
        original = repo.create_task('human:1', 'engineering-hub', 'Retained synthetic task', 'seed')
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'hub.sqlite3').write_bytes(b'old shared ledger must not be opened')
    (shared / 'settings.json').write_text('untouched shared settings')
    bus_root = shared / 'agent-message-bus'
    bus_root.mkdir()
    bus_db = bus_root / 'messages.sqlite3'
    messages = MessageRepository(bus_db)
    message = messages.insert(sender='hermes', recipient='codex', message_type='info', body={'synthetic': True})
    (bus_root / 'audit.jsonl').touch()
    (bus_root / 'files').mkdir()
    paths = {'AGENT_BUS_DB': bus_db, 'AGENT_BUS_AUDIT': bus_root / 'audit.jsonl',
             'AGENT_BUS_FILE_ROOT': bus_root / 'files'}
    auth, assertion = signed_access(monkeypatch)
    selected = Settings(database, AuthSettings('test-hermes', 'test-codex',
        claude_token='test-claude', allowed_ips=frozenset({'127.0.0.1'})), auth,
        require_owned_ledger=True)
    monkeypatch.delenv('HUB_BUS_STARTUP_CLEANUP', raising=False)
    monkeypatch.setattr(service, 'PilotOwner', lambda path, **kwargs:
        PilotOwner(path, allow_ephemeral_test_storage=True, **kwargs))
    for iteration in range(2):
        layout.validate_existing_boundary(database, shared_root=shared, shared_paths=paths)
        stop = asyncio.Event()
        ready = asyncio.get_running_loop().create_future()
        serving = asyncio.create_task(service.serve_owned_inactive_hub(selected, bus_paths=paths,
            agent_host='127.0.0.1', agent_port=0, browser_host='127.0.0.1', browser_port=0,
            install_signals=False, stop_event=stop, on_ready=lambda *addresses: ready.set_result(addresses)))
        try:
            done, _ = await asyncio.wait((ready, serving), timeout=8, return_when=asyncio.FIRST_COMPLETED)
            if serving in done:
                await serving
            assert ready in done
            agent, browser = ready.result()
            async with httpx.AsyncClient(base_url=f'http://{agent[0]}:{agent[1]}', trust_env=False,
                headers={'Authorization': 'Bearer test-codex', 'Host': 'hub.example'}) as client:
                result = await client.get(BASE + '/tasks/' + original['id'])
                assert result.status_code == 200
                assert result.json()['title'] == original['title']
                overview = (await client.get(BASE + '/overview')).json()
                assert overview['autonomy']['configured'] is False
                assert overview['autonomy']['running'] is False
                assert message.id in (await client.get('/api/messages')).text
            async with httpx.AsyncClient(base_url=f'http://{browser[0]}:{browser[1]}', trust_env=False,
                headers={'Host': 'hub.example', 'CF-Access-Jwt-Assertion': assertion}) as human:
                response = await human.post(BASE + '/auth/bootstrap', headers={'Origin': ORIGIN})
                assert response.status_code == 200
                human.headers.update({'Origin': ORIGIN, 'X-Hub-CSRF': response.json()['csrf_token'],
                    'Cookie': 'hub_session=' + human.cookies.get('hub_session')})
                note = await human.post(BASE + '/notes', json={'project': 'engineering-hub',
                    'path': 'protected/restart.md', 'title': 'Synthetic private mount test',
                    'content': 'Retained note ' + str(iteration), 'expected_version': iteration})
                assert note.status_code == 200
                assert note.json()['version'] == iteration + 1
        finally:
            stop.set()
            await asyncio.wait_for(serving, 8)
        assert (shared / 'hub.sqlite3').read_bytes() == b'old shared ledger must not be opened'
        assert (shared / 'settings.json').read_text() == 'untouched shared settings'
        assert messages.get(message.id).status == 'pending'
    # Both reopens consumed the same authoritative ledger; no fallback existed.
    with PilotOwner(database, allow_ephemeral_test_storage=True, require_existing=True) as owner:
        assert HubRepository(database, pilot_owner=owner).get_task(original['id'])['title'] == original['title']
