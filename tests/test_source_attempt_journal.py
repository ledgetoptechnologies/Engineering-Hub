import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from ipaddress import ip_address

import httpx

import pytest

from hub_staging_service import core
from tests.test_source_evidence import make_source
from tests.test_staging_service_core import Backend, job, profile


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    try:
        yield service
    finally:
        service.close()


def test_feature_off_neither_registers_nor_records(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', False)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    try:
        with pytest.raises(core.Rejected, match='source_attempt_unavailable'):
            service.put_source_attempt(make_source())
        assert service.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone() is None
    finally:
        service.close()


@pytest.mark.parametrize('attempt,profile_ready', [(False, False), (False, True), (True, False)])
def test_either_v2_gate_off_refuses_effect_without_journal(tmp_path, monkeypatch, attempt, profile_ready):
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', attempt)
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', profile_ready)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    try:
        with pytest.raises(core.Rejected, match='source_attempt_unavailable'):
            service.put_source_attempt(make_source())
        assert service.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone() is None
        assert service.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0
    finally:
        service.close()


def test_reopen_feature_off_retains_attempt_and_legacy_data(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    legacy, attempted = make_source(), make_source()
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    legacy_receipt = service.put_source(legacy)
    service.put_source_attempt(attempted)
    before = tuple(service.db.execute('SELECT * FROM source_attempts').fetchone())
    service.close()
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', False)
    reopened = core.Service(tmp_path, {'demo': profile()}, Backend())
    try:
        assert tuple(reopened.db.execute('SELECT * FROM source_attempts').fetchone()) == before
        assert reopened.put_source(legacy) == legacy_receipt
        with pytest.raises(core.Rejected, match='source_attempt_reserved'):
            reopened.put_source(attempted)
        with pytest.raises(core.Rejected, match='source_attempt_unavailable'):
            reopened.source_attempt(attempted['source_id'], 'demo')
    finally:
        reopened.close()


def test_http_attempt_respects_auth_gate_and_never_replays(worker, monkeypatch):
    from hub_staging_service.__main__ import handler
    from hub_staging_service import __main__ as worker_http
    from hub_staging_service.modular_staging_profile import from_worker_recipe, profile_digest
    payload = make_source()
    profile_value = {'project':'demo', 'recipe':'default', 'generation':1,
                     'digest':profile_digest(from_worker_recipe(worker.projects['demo']['default']))}
    payload['reviewed_profile'] = profile_value
    token = 'synthetic-attempt-token'
    legacy = 'synthetic-legacy-token'
    entries = (('alice', token, frozenset({'demo'}), {'demo':frozenset({'default'})}),)
    monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', True)
    monkeypatch.setattr(worker_http, '_SCOPED_LIFECYCLE_READY', True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(worker, legacy, {ip_address('127.0.0.1')}, v2_credentials=entries))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}/v2/source-attempts'
        headers = {'Authorization': 'Bearer ' + token}
        with httpx.Client(trust_env=False, timeout=5) as client:
            assert client.post(base, json=payload).status_code == 401
            monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', False)
            assert client.post(base, json=payload, headers=headers).status_code == 503
            assert worker.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone() is None
            monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
            response = client.post(base, json=payload, headers=headers)
            assert response.status_code == 200 and response.json()['state'] == 'historical_observation'
            repeated = client.post(base, json=payload, headers=headers)
            assert repeated.status_code == 409 and repeated.json()['error'] == 'source_attempt_unknown'
            read = client.get(base + '/' + payload['source_id'], params={'project': 'demo', 'recipe':'default'}, headers=headers)
            assert read.status_code == 200 and read.json()['state'] == 'historical_observation'
            assert read.json()['reviewed_profile'] == profile_value
            assert worker.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def test_success_retains_evidence_and_never_replays(worker):
    payload = make_source()
    result = worker.put_source_attempt(payload)
    assert result['state'] == 'historical_observation' and result['authority'] == 'observation_only'
    assert worker.source_attempt(payload['source_id'], 'demo')['source_binding'] == result['source_binding']
    with pytest.raises(core.Rejected, match='source_attempt_unknown'):
        worker.put_source_attempt(payload)
    assert worker.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 1


def test_interruption_before_effect_retains_unknown_and_never_reposts(worker, monkeypatch):
    payload = make_source()
    monkeypatch.setattr(worker, '_legacy_put_source', lambda _, **kw: (_ for _ in ()).throw(RuntimeError('synthetic interruption')))
    with pytest.raises(RuntimeError):
        worker.put_source_attempt(payload)
    assert worker.source_attempt(payload['source_id'], 'demo')['state'] == 'uncertain'
    assert worker.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0
    with pytest.raises(core.Rejected, match='source_attempt_unknown'):
        worker.put_source_attempt(payload)


def test_lost_ack_after_effect_remains_uncertain_without_repost(worker, monkeypatch):
    payload = make_source()
    original = worker._legacy_put_source
    def lost_response(value, **kw):
        original(value, **kw)
        raise RuntimeError('synthetic lost response')
    monkeypatch.setattr(worker, '_legacy_put_source', lost_response)
    with pytest.raises(RuntimeError):
        worker.put_source_attempt(payload)
    assert worker.source_attempt(payload['source_id'], 'demo')['state'] == 'uncertain'
    assert worker.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 1
    with pytest.raises(core.Rejected, match='source_attempt_unknown'):
        worker.put_source_attempt(payload)
    assert worker.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 1


def test_bound_capacity_fails_without_eviction(worker, monkeypatch):
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_LIMIT', 1)
    first, second = make_source(), make_source()
    worker.put_source_attempt(first)
    with pytest.raises(core.Rejected, match='source_attempt_capacity'):
        worker.put_source_attempt(second)
    assert worker.source_attempt(first['source_id'], 'demo')['state'] == 'historical_observation'
    assert worker.db.execute('SELECT COUNT(*) FROM source_attempts').fetchone()[0] == 1


def test_triggered_journal_is_rejected_before_deposit(worker):
    worker.db.execute(core._SOURCE_ATTEMPT_DDL)
    worker.db.execute('CREATE TRIGGER erase_attempt AFTER INSERT ON source_attempts '
                      'BEGIN DELETE FROM source_attempts; END')
    worker.db.commit()
    with pytest.raises(core.Rejected, match='source_attempt_journal_invalid'):
        worker.put_source_attempt(make_source())
    assert worker.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0


def test_orphan_binding_prevents_attempt_and_is_preserved(worker):
    payload = make_source()
    worker.db.execute('INSERT INTO source_bindings VALUES(?,?,?,?)', (payload['source_id'], 123, 1.0, '{}'))
    worker.db.commit()
    with pytest.raises(core.Rejected, match='source_attempt_conflict'):
        worker.put_source_attempt(payload)
    assert worker.db.execute('SELECT binding FROM source_bindings WHERE source_id=?', (payload['source_id'],)).fetchone()[0] == '{}'
    assert worker.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0


def test_v1_rejects_reserved_id_after_disable_and_cleanup_but_other_v1_still_works(worker, monkeypatch):
    reserved, other = make_source(), make_source()
    worker.put_source_attempt(reserved)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', False)
    with pytest.raises(core.Rejected, match='source_attempt_reserved'):
        worker.put_source(reserved)
    worker.cleanup_sources(now=time.time() + 86401)
    original_cleanup = worker.cleanup_sources
    monkeypatch.setattr(worker, 'cleanup_sources', lambda: pytest.fail('reserved ID triggered source cleanup'))
    with pytest.raises(core.Rejected, match='source_attempt_reserved'):
        worker.put_source(reserved)
    monkeypatch.setattr(worker, 'cleanup_sources', original_cleanup)
    assert worker.put_source(other)['source_id'] == other['source_id']


@pytest.mark.parametrize('corruption', ['trigger', 'wrong_schema', 'view'])
def test_v1_fails_closed_on_corrupt_attempt_schema(worker, corruption):
    if corruption == 'view':
        worker.db.execute('CREATE VIEW source_attempts AS SELECT 1 AS source_id')
    elif corruption == 'wrong_schema':
        worker.db.execute('CREATE TABLE source_attempts (source_id TEXT PRIMARY KEY)')
    else:
        worker.db.execute(core._SOURCE_ATTEMPT_DDL)
        worker.db.execute('CREATE TRIGGER rewrite_attempt AFTER INSERT ON source_attempts BEGIN DELETE FROM source_attempts; END')
    worker.db.commit()
    with pytest.raises(core.Rejected, match='source_attempt_journal_invalid'):
        worker.put_source(make_source())
    assert worker.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0


def test_legacy_retry_without_attempt_table_does_not_need_writer(worker):
    payload = make_source()
    worker.put_source(payload)
    other = sqlite3.connect(worker.root / 'journal.sqlite3', timeout=0, isolation_level=None)
    try:
        other.execute('BEGIN IMMEDIATE')
        assert worker.put_source(payload)['source_id'] == payload['source_id']
    finally:
        other.rollback()
        other.close()


def test_legacy_retry_with_attempt_table_keeps_writer_through_source_check(worker, monkeypatch):
    payload = make_source()
    worker.put_source(payload)
    worker.db.execute(core._SOURCE_ATTEMPT_DDL)
    worker.db.commit()
    original = worker._source
    def check_writer(*args):
        other = sqlite3.connect(worker.root / 'journal.sqlite3', timeout=0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
        finally:
            other.close()
        return original(*args)
    monkeypatch.setattr(worker, '_source', check_writer)
    assert worker.put_source(payload)['source_id'] == payload['source_id']


def test_stale_binding_with_attempt_table_fails_without_helper_commit(worker):
    payload = make_source()
    worker.put_source(payload)
    worker.db.execute(core._SOURCE_ATTEMPT_DDL)
    worker.db.execute('UPDATE source_bindings SET source_inode=source_inode+1 WHERE source_id=?', (payload['source_id'],))
    worker.db.commit()
    before = tuple(worker.db.execute('SELECT * FROM source_bindings WHERE source_id=?', (payload['source_id'],)).fetchone())
    with pytest.raises(core.Rejected, match='source_identity_conflict'):
        worker.put_source(payload)
    assert tuple(worker.db.execute('SELECT * FROM source_bindings WHERE source_id=?', (payload['source_id'],)).fetchone()) == before


def test_v1_job_without_attempt_table_preserves_legacy_stale_binding_behavior(worker):
    source = make_source()
    worker.put_source(source)
    worker.db.execute('UPDATE source_bindings SET source_inode=source_inode+1 WHERE source_id=?', (source['source_id'],))
    worker.db.commit()
    payload = job(__import__('base64').b64decode(source['source_zip_base64']))
    del payload['source_zip_base64']
    payload['source_id'] = source['source_id']
    payload['source_commit'] = source['source_commit']
    receipt = worker.submit(payload)
    assert receipt['job_id'] == payload['job_id']
    worker.wait()
    assert worker.status(payload['job_id'])['state'] == 'succeeded'
    assert worker.db.execute('SELECT 1 FROM source_bindings WHERE source_id=?', (source['source_id'],)).fetchone() is None


def test_v1_job_without_attempt_table_reaches_source_check_while_writer_busy(worker, monkeypatch):
    source = make_source()
    worker.put_source(source)
    payload = job(__import__('base64').b64decode(source['source_zip_base64']))
    del payload['source_zip_base64']
    payload['source_id'] = source['source_id']
    payload['source_commit'] = source['source_commit']
    monkeypatch.setattr(worker, '_source', lambda *args: (_ for _ in ()).throw(RuntimeError('source check reached')))
    other = sqlite3.connect(worker.root / 'journal.sqlite3', timeout=0, isolation_level=None)
    try:
        other.execute('BEGIN IMMEDIATE')
        with pytest.raises(RuntimeError, match='source check reached'):
            worker.submit(payload)
    finally:
        other.rollback()
        other.close()


def test_expired_source_does_not_turn_historical_attempt_into_current_presence(worker):
    payload = make_source()
    worker.put_source_attempt(payload)
    worker.cleanup_sources(now=time.time() + 86401)
    assert worker.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0
    assert worker.source_attempt(payload['source_id'], 'demo')['state'] == 'historical_observation'
    # The distinct evidence API is required to inspect current state.
    from tests.test_source_evidence import lookup
    core._SOURCE_EVIDENCE_ENABLED = True
    try:
        assert lookup(worker, payload)['state'] == 'unknown'
    finally:
        core._SOURCE_EVIDENCE_ENABLED = False


@pytest.mark.skipif(os.name != 'posix', reason='POSIX crash and signal semantics')
@pytest.mark.parametrize('cut', ['before_effect', 'after_file_fsync', 'after_source_commit', 'after_attempt_commit'])
def test_process_kill_retains_original_uncertain_attempt(tmp_path, monkeypatch, cut):
    payload = make_source()
    root, payload_path, marker = tmp_path / 'worker', tmp_path / 'payload.json', tmp_path / 'barrier'
    payload_path.write_text(json.dumps(payload))
    script = r'''
import json, os, pathlib, sys, time
from hub_staging_service import core
from tests.test_staging_service_core import Backend, profile
core._SOURCE_ATTEMPT_ENABLED = True
core._PROFILE_PROTOCOL_READY = True
service = core.Service(sys.argv[1], {'demo': profile()}, Backend())
payload = json.loads(pathlib.Path(sys.argv[2]).read_text())
cut, marker = sys.argv[3], pathlib.Path(sys.argv[4])
def barrier():
    marker.write_text(cut)
    while True: time.sleep(0.05)
original = service._legacy_put_source
if cut == 'before_effect':
    service._legacy_put_source = lambda payload, **kw: barrier()
elif cut == 'after_source_commit':
    def stopped_after_commit(payload, **kw):
        result = original(payload, **kw)
        barrier()
        return result
    service._legacy_put_source = stopped_after_commit
elif cut == 'after_file_fsync':
    original_fsync = os.fsync
    def stopped_after_fsync(fd):
        original_fsync(fd)
        barrier()
    os.fsync = stopped_after_fsync
service.put_source_attempt(payload)
if cut == 'after_attempt_commit': barrier()
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(root), str(payload_path), cut, str(marker)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), f'worker did not reach {cut}: {process.communicate(timeout=2) if process.poll() is not None else "timeout"}'
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode == -9
        monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
        monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
        reopened = core.Service(root, {'demo': profile()}, Backend())
        try:
            expected_state = 'historical_observation' if cut == 'after_attempt_commit' else 'uncertain'
            assert reopened.source_attempt(payload['source_id'], 'demo')['state'] == expected_state
            expected_count = 1 if cut in ('after_source_commit', 'after_attempt_commit') else 0
            assert reopened.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == expected_count
            with pytest.raises(core.Rejected, match='source_attempt_unknown'):
                reopened.put_source_attempt(payload)
            assert reopened.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == expected_count
            if cut == 'after_file_fsync':
                assert (reopened.sources / (payload['source_id'] + '.zip')).is_file()
        finally:
            reopened.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
