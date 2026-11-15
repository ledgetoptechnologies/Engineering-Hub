import io
import json
import os
import uuid

import pytest

from agent_message_bus.agent_session_client import ClientError
from engineering_hub import tracking_client, tracking_outbox
from tests.test_tracking_recovery import ORIGIN, SECOND, TASK, response, setup


class Input:
    def __init__(self, value): self.buffer = io.BytesIO(json.dumps(value).encode())


def test_private_inventory_projects_states_and_recovery_without_secrets(tmp_path, monkeypatch, capsys):
    outbox, _, source = setup(tmp_path)
    attempt = tracking_outbox.read(outbox, tracking_outbox.stage_recovery(outbox, source, ORIGIN, SECOND))
    tracking_outbox.record_acceptance(outbox, attempt, response(attempt))
    unbound_id = tracking_outbox.stage(outbox, None, None, None, 'propose-draft',
        {'project': 'project-alpha', 'title': 'Line\nBidi\u202eTitle', 'details': 'SECRET_DETAILS',
         'client_request_id': 'SECRET_REQUEST'})
    broken = str(uuid.uuid4()); (outbox / (broken + '.tracking')).write_bytes(b'not-json')
    (outbox / 'invalid.tracking').write_bytes(b'bad')
    async def forbidden(*args, **kwargs):
        raise AssertionError('inventory attempted network')
    monkeypatch.setattr(tracking_client, '_run', forbidden)
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({'limit': 25}))
    assert tracking_client.main(['--outbox-dir', str(outbox), 'inventory-staged']) == 0
    raw = capsys.readouterr().out
    assert 'SECRET_DETAILS' not in raw and 'SECRET_REQUEST' not in raw and source['arguments']['intake_id'] not in raw
    page = json.loads(raw); items = {item['record_id']: item for item in page['items']}
    assert page['trust'] == 'private_untrusted_local_data' and not page['execution_authorization']
    assert page['unrecognized_tracking_entries'] == 1 and page['next_after'] is None
    assert items[source['id']]['state'] == 'unconfirmed'
    assert items[attempt['id']]['state'] == 'hub_accepted' and items[attempt['id']]['task_id'] == TASK
    assert items[attempt['id']]['source_record_id'] == source['id']
    assert items[unbound_id]['state'] == 'unbound' and items[unbound_id]['project'] == 'project-alpha'
    assert items[unbound_id]['title'] == 'LineBidiTitle'
    assert items[broken] == {'record_id': broken, 'state': 'unreadable'}


def test_inventory_pagination_does_not_skip_unreadable_or_raced_missing(tmp_path, monkeypatch):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    ids = [str(uuid.UUID(int=i)) for i in range(1, 71)]
    for identity in reversed(ids):
        (directory / (identity + '.tracking')).touch()
    actual = tracking_outbox.read
    calls = []
    def missing(directory, identity):
        calls.append(identity)
        if identity == ids[2]: raise FileNotFoundError()
        return actual(directory, identity)
    monkeypatch.setattr(tracking_outbox, 'read', missing)
    found = []; cursor = None
    while True:
        page = tracking_outbox.inventory_page(directory, limit=17, after=cursor)
        assert len(page['items']) <= 17
        found += [item['record_id'] for item in page['items']]
        assert all(item['state'] == 'unreadable' for item in page['items'])
        cursor = page['next_after']
        if cursor is None: break
        assert cursor == page['items'][-1]['record_id']
    assert found == ids and calls == ids
    for bad in [0, 51, True, '2']:
        with pytest.raises(ClientError): tracking_outbox.inventory_page(directory, limit=bad)
    with pytest.raises(ClientError): tracking_outbox.inventory_page(directory, after='../bad')


def test_inventory_cap_counts_every_directory_entry_and_reports_fallback(tmp_path, monkeypatch, capsys):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    class Entry:
        name = 'unrelated'
    class Entries:
        def __enter__(self): return iter([Entry()] * 4097)
        def __exit__(self, *args): pass
    monkeypatch.setattr(tracking_outbox.os, 'scandir', lambda directory: Entries())
    monkeypatch.setattr(tracking_client.sys, 'stdin', Input({}))
    assert tracking_client.main(['--outbox-dir', str(directory), 'inventory-staged']) == 1
    result = capsys.readouterr()
    assert result.out == '' and 'too_many_entries' in result.err and 'list-staged' in result.err and 'inspect-staged' in result.err


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink creation')
def test_inventory_reports_symlink_unreadable_without_opening(tmp_path, monkeypatch):
    directory = tmp_path / 'outbox'; directory.mkdir(mode=0o700)
    identity = str(uuid.uuid4()); (directory / (identity + '.tracking')).symlink_to(tmp_path / 'outside')
    monkeypatch.setattr(tracking_outbox, 'read', lambda *args: (_ for _ in ()).throw(AssertionError('followed symlink')))
    assert tracking_outbox.inventory_page(directory)['items'] == [{'record_id': identity, 'state': 'unreadable'}]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX FIFO and O_NONBLOCK semantics')
@pytest.mark.parametrize('kind', ['tracking', 'binding', 'accepted'])
def test_inventory_fifo_replacement_race_never_blocks(tmp_path, monkeypatch, kind):
    from pathlib import Path
    outbox, _, source = setup(tmp_path)
    if kind == 'accepted':
        tracking_outbox.record_acceptance(outbox, source, response(source))
    target = outbox / (source['id'] + '.' + kind)
    original_info = target.lstat()
    fifo = outbox / ('race-' + kind)
    os.mkfifo(fifo)
    os.replace(fifo, target)
    original_lstat, original_open = Path.lstat, os.open
    observed = []
    def raced_lstat(path):
        if path == target: return original_info
        return original_lstat(path)
    def guarded_open(path, flags, *args, **kwargs):
        if Path(path) == target:
            assert flags & os.O_NONBLOCK
            observed.append(True)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', raced_lstat)
    monkeypatch.setattr(tracking_outbox.os, 'open', guarded_open)
    item = tracking_outbox.inventory_page(outbox)['items'][0]
    assert observed and item == {'record_id': source['id'], 'state': 'unreadable'}
