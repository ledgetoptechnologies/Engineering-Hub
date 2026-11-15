import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from ipaddress import ip_address
from urllib.parse import urlencode

import httpx
import pytest

from hub_staging_service import core
from hub_staging_service.source_provenance import _archive_tree, _object_id, verify
from tests.test_staging_service_core import Backend, profile


def make_source():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        info = zipfile.ZipInfo('index.html')
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        archive.writestr(info, 'source')
    raw = output.getvalue()
    tree = _archive_tree(raw)[0]
    obj = (f'tree {tree}\nauthor Test <test@example.invalid> 1 +0000\n'
           'committer Test <test@example.invalid> 1 +0000\n\nfixture\n').encode()
    return {'source_id': str(uuid.uuid4()), 'project': 'demo',
            'source_digest': hashlib.sha256(raw).hexdigest(),
            'source_zip_base64': base64.b64encode(raw).decode(),
            'source_commit': _object_id(b'commit', obj),
            'source_commit_object_base64': base64.b64encode(obj).decode()}


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', True)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    try:
        yield service
    finally:
        service.close()


def lookup(worker, payload):
    binding = verify(base64.b64decode(payload['source_zip_base64']),
                     payload['source_commit_object_base64'], payload['source_commit'])
    binding['archive_sha256'] = payload['source_digest']
    return worker.source_evidence(payload['source_id'], payload['project'], payload['source_digest'], binding)


@pytest.mark.skipif(os.name != 'posix', reason='descriptor-relative nofollow required')
def test_present_is_exact_nonmutating_observation(worker):
    payload = make_source()
    receipt = worker.put_source(payload)
    changes = worker.db.total_changes
    result = lookup(worker, payload)
    assert result == {'state': 'present', 'authority': 'observation_only',
                      'binding_assurance': 'journal_match_tree_checked', **receipt}
    assert worker.db.total_changes == changes
    assert lookup(worker, dict(payload, source_digest='0' * 64)) == {
        'state': 'conflict', 'authority': 'observation_only'}


@pytest.mark.skipif(os.name != 'posix', reason='descriptor-relative nofollow required')
def test_journal_commit_or_object_hash_change_conflicts_with_original_binding(worker):
    payload = make_source()
    receipt = worker.put_source(payload)
    expected = receipt['source_binding']
    for key in ('commit_oid', 'commit_object_sha256'):
        tampered = dict(expected, **{key: '0' * len(expected[key])})
        worker.db.execute('UPDATE source_bindings SET binding=?', (json.dumps(tampered),))
        worker.db.commit()
        changes = worker.db.total_changes
        assert worker.source_evidence(payload['source_id'], 'demo', payload['source_digest'], expected) == {
            'state': 'conflict', 'authority': 'observation_only'}
        assert worker.db.total_changes == changes


def test_absence_and_orphan_are_unknown_without_mutation(worker):
    payload = make_source()
    path = worker.sources / (payload['source_id'] + '.zip')
    path.write_bytes(base64.b64decode(payload['source_zip_base64']))
    assert lookup(worker, payload) == {'state': 'unknown', 'authority': 'observation_only'}
    assert path.exists() and worker.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0


@pytest.mark.skipif(os.name != 'posix', reason='descriptor-relative nofollow required')
@pytest.mark.parametrize('damage', ['file', 'binding', 'incarnation', 'symlink', 'expired'])
def test_damage_and_expiry_do_not_fabricate_absence_or_mutate(worker, damage, tmp_path):
    payload = make_source()
    worker.put_source(payload)
    path = worker.sources / (payload['source_id'] + '.zip')
    if damage == 'file':
        path.write_bytes(b'x' * path.stat().st_size)
    elif damage == 'binding':
        worker.db.execute('UPDATE source_bindings SET binding=?', ('{}',))
        worker.db.commit()
    elif damage == 'incarnation':
        worker.db.execute('UPDATE source_bindings SET source_inode=source_inode+1')
        worker.db.commit()
    elif damage == 'symlink':
        target = tmp_path / 'target'
        path.rename(target)
        path.symlink_to(target)
    else:
        worker.db.execute('UPDATE sources SET created=?', (time.time() - 90000,))
        worker.db.commit()
        worker.cleanup_sources()
    changes = worker.db.total_changes
    assert lookup(worker, payload) == {'state': 'unknown', 'authority': 'observation_only'}
    assert worker.db.total_changes == changes
    if damage == 'incarnation':
        assert worker.db.execute('SELECT count(*) FROM source_bindings').fetchone()[0] == 1


@pytest.mark.skipif(os.name != 'posix', reason='descriptor-relative nofollow required')
def test_read_serializes_with_same_process_deposit(worker):
    payload = make_source()
    started = threading.Event()
    def read():
        started.set()
        return lookup(worker, payload)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with worker.lock:
            future = executor.submit(read)
            assert started.wait(2) and not future.done()
            worker.put_source(payload)
        assert future.result(timeout=5)['state'] == 'present'


def test_gate_disabled_without_read(worker, monkeypatch):
    monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', False)
    with pytest.raises(core.Rejected, match='source_evidence_unavailable'):
        lookup(worker, make_source())


def test_http_evidence_requires_bearer_and_does_not_expose_on_bad_query(worker, monkeypatch):
    from hub_staging_service.__main__ import handler
    from hub_staging_service import __main__ as worker_http
    from hub_staging_service.modular_staging_profile import from_worker_recipe, profile_digest
    payload = make_source()
    token = 'synthetic-token-for-test-only'
    legacy = 'synthetic-legacy-token'
    entries = (('alice', token, frozenset({'demo'}), {'demo':frozenset({'default'})}),)
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    monkeypatch.setattr(worker_http, '_SCOPED_LIFECYCLE_READY', True)
    profile_value = {'project':'demo', 'recipe':'default', 'generation':1,
                     'digest':profile_digest(from_worker_recipe(worker.projects['demo']['default']))}
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(worker, legacy, {ip_address('127.0.0.1')}, v2_credentials=entries))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}/v2/source-evidence/{payload["source_id"]}'
        binding = verify(base64.b64decode(payload['source_zip_base64']),
                         payload['source_commit_object_base64'], payload['source_commit'])
        query = urlencode({'project': 'demo', 'recipe':'default', 'digest': payload['source_digest'],
                           'binding': json.dumps(binding, separators=(',', ':'))})
        with httpx.Client(trust_env=False, timeout=5) as client:
            assert client.get(base + '?' + query).status_code == 401
            headers = {'Authorization': 'Bearer ' + token}
            assert client.get(base + '?' + query + '&project=demo', headers=headers).status_code == 400
            response = client.get(base + '?' + query, headers=headers)
        assert response.status_code == 200 and response.json() == {
            'state': 'unknown', 'authority': 'observation_only'}
        if os.name == 'posix':
            expected = worker.put_source_attempt(payload, principal='alice', recipe='default', reviewed_profile=profile_value)
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.get(base + '?' + query, headers=headers)
            expected = {key: value for key, value in expected.items() if key not in ('state', 'authority')}
            assert response.json() == {'state': 'present', 'authority': 'observation_only',
                                       'binding_assurance': 'journal_match_tree_checked', **expected}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.skipif(os.name != 'posix', reason='process kill and worker lock require POSIX')
def test_crash_after_source_fsync_before_commit_reports_unknown(tmp_path, monkeypatch):
    payload = make_source()
    source_root = tmp_path / 'worker'
    payload_path = tmp_path / 'input.json'
    marker = tmp_path / 'after-fsync'
    payload_path.write_text(json.dumps(payload))
    script = '''
import json, os, pathlib, sys, time
from hub_staging_service import core
from tests.test_staging_service_core import Backend, profile
service = core.Service(sys.argv[1], {'demo': profile()}, Backend())
original = os.fsync
def blocked(fd):
    original(fd)
    pathlib.Path(sys.argv[3]).write_text('source fsynced')
    while True:
        time.sleep(1)
os.fsync = blocked
service.put_source(json.loads(pathlib.Path(sys.argv[2]).read_text()))
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(source_root), str(payload_path), str(marker)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), 'worker did not reach the source fsync barrier'
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode == -9
        monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', True)
        reopened = core.Service(source_root, {'demo': profile()}, Backend())
        try:
            assert reopened.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
            assert (reopened.sources / (payload['source_id'] + '.zip')).exists()
            assert lookup(reopened, payload) == {'state': 'unknown', 'authority': 'observation_only'}
        finally:
            reopened.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
