import ipaddress
import base64
import copy
import json
import os
import stat
import subprocess
import shutil
import sys
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from hub_staging_service import core
from hub_staging_service import __main__ as worker_http
from hub_staging_service.__main__ import handler, scoped_credentials
from tests.test_source_evidence import make_source
from tests.test_staging_service_core import Backend, job, profile, uid
from tests.test_hub_staging_wire_integration import Wire
from tests.test_staging_service_terminal_receipt import configured, close

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='private credential modes require POSIX')


def reviewed(service):
    from engineering_hub.modular_staging_profile import from_worker_recipe
    from hub_staging_service.profile_binding import profile_digest
    return {'project': 'demo', 'recipe': 'default', 'generation': 1,
            'digest': profile_digest(from_worker_recipe(service.projects['demo']['default']))}


def deposit(service, payload, principal):
    return service.put_source_attempt(payload, principal=principal, recipe='default', reviewed_profile=reviewed(service))


def test_scoped_attempts_deny_legacy_and_other_principal(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', True)
    monkeypatch.setattr(worker_http, '_SCOPED_LIFECYCLE_READY', True)
    service = core.Service(tmp_path, {'demo': profile(), 'other': profile()}, Backend())
    legacy = 'synthetic-legacy-secret'
    entries = (('alice', 'synthetic-alice-secret', frozenset({'demo'}), {'demo': frozenset({'default'})}),
               ('bob', 'synthetic-bob-secret-1', frozenset({'demo'}), {'demo': frozenset({'default'})}),
               ('carol', 'synthetic-carol-secret', frozenset({'other'}), {'other': frozenset({'default'})}),
               ('dave', 'synthetic-dave-secret-1', frozenset({'demo'}), {}))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(service, legacy, {ipaddress.ip_address('127.0.0.1')}, v2_credentials=entries))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    payload = make_source()
    scoped_payload = dict(payload, reviewed_profile=reviewed(service))
    monkeypatch.setattr(service, 'file', lambda _resource, name: name.encode())
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        with httpx.Client(trust_env=False, timeout=5) as client:
            def headers(secret): return {'Authorization': 'Bearer ' + secret}
            url = base + '/v2/source-attempts'
            assert client.post(url, json=payload, headers=headers(legacy)).status_code == 404
            assert client.post(base + '/v1/jobs', json={}, headers=headers(entries[0][1])).status_code == 404
            assert client.get(base + '/v1/runtime', headers=headers(legacy)).status_code == 404
            assert client.post(base + '/v%32/source-attempts', json=payload, headers=headers(legacy)).status_code == 404
            legacy_file = client.get(base + '/v1/resources/' + payload['source_id'] + '/files/a%20b.txt', headers=headers(legacy))
            assert legacy_file.status_code == 200 and legacy_file.content == b'a b.txt'
            assert client.post(url, json=scoped_payload, headers=headers(entries[2][1])).status_code == 404
            assert client.post(url, json=scoped_payload, headers=headers(entries[3][1])).status_code == 404
            response = client.post(url, json=scoped_payload, headers=headers(entries[0][1]))
            assert response.status_code == 200
            assert response.json()['reviewed_profile'] == scoped_payload['reviewed_profile']
            evidence_url = base + '/v2/source-evidence/' + payload['source_id']
            query = {'project': 'demo', 'recipe': 'default', 'digest': payload['source_digest'], 'binding': json.dumps(response.json()['source_binding'])}
            assert client.get(evidence_url, params=query, headers=headers(entries[0][1])).json()['state'] == 'present'
            assert client.get(evidence_url, params=query, headers=headers(entries[1][1])).json()['state'] == 'unknown'
            assert client.get(evidence_url, params=query, headers=headers(entries[3][1])).status_code == 404
            assert client.post(url, json=scoped_payload, headers=headers(entries[1][1])).status_code == 409
            assert client.post(url, json=scoped_payload, headers=headers(entries[0][1])).json()['error'] == 'source_attempt_unknown'
            read = url + '/' + payload['source_id']
            assert client.get(read, params={'project': 'demo', 'recipe': 'default'}, headers=headers(entries[0][1])).json()['state'] == 'historical_observation'
            assert client.get(read, params={'project': 'demo', 'recipe': 'default'}, headers=headers(entries[1][1])).json()['state'] == 'unknown'
            assert client.get(read, params={'project': 'demo', 'recipe': 'default'}, headers=headers(entries[3][1])).status_code == 404
            assert client.post(base + '/v2/jobs', json={}, headers=headers(entries[0][1])).status_code == 404
            assert client.get(base + '/v1/jobs/' + payload['source_id'], headers=headers(entries[0][1])).status_code == 404
        with pytest.raises(core.Rejected, match='source_attempt_reserved'):
            service.put_source(payload)
        service.cleanup_sources(now=10**12)
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
    finally:
        server.shutdown(); server.server_close(); thread.join(5); service.close()
        assert not thread.is_alive()


@pytest.mark.parametrize('profile_ready,attempt,evidence,lifecycle', [(False, True, True, True),
    (True, False, True, True), (True, True, False, True), (True, True, True, False), (False, False, False, False)])
def test_partial_scoped_interlocks_do_not_deposit(tmp_path, monkeypatch, profile_ready, attempt, evidence, lifecycle):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', profile_ready)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', attempt)
    monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', evidence)
    monkeypatch.setattr(worker_http, '_SCOPED_LIFECYCLE_READY', lifecycle)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    # Explicit synthetic migration; no automatic production schema change.
    service.db.executescript("""CREATE INDEX status_unsettled_operations ON operations(state)
                WHERE state NOT IN ('succeeded','failed') OR state IS NULL;
            CREATE INDEX status_unreleased_leases ON scratch_leases(state)
                WHERE state != 'released' OR state IS NULL;""")
    entries = (('alice', 'synthetic-alice-secret', frozenset({'demo'}), {'demo': frozenset({'default'})}),)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(service, 'synthetic-legacy-secret',
                                  {ipaddress.ip_address('127.0.0.1')}, v2_credentials=entries))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        with httpx.Client(trust_env=False, timeout=5, headers={'Authorization': 'Bearer synthetic-alice-secret'}) as client:
            assert client.get(base + '/v2/status').json()['profile_protocol_ready'] is False
            assert client.post(base + '/v2/source-attempts', json=make_source()).status_code == 503
            assert client.post(base + '/v2/jobs', json={}).status_code == 503
            assert client.post(base + '/v2/cleanup', json={}).status_code == 503
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
        assert service.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone() is None
    finally:
        server.shutdown(); server.server_close(); thread.join(5); service.close()
        assert not thread.is_alive()


def test_scoped_uncertain_original_remains_owned_after_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    payload = make_source()
    try:
        monkeypatch.setattr(service, '_legacy_put_source', lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError('interruption')))
        with pytest.raises(RuntimeError): deposit(service, payload, 'alice')
        assert service.source_attempt(payload['source_id'], 'demo', principal='alice', recipe='default')['state'] == 'uncertain'
        assert service.source_attempt(payload['source_id'], 'demo', principal='bob', recipe='default')['state'] == 'unknown'
        with pytest.raises(core.Rejected, match='source_attempt_unknown'):
            deposit(service, payload, 'alice')
        with pytest.raises(core.Rejected, match='source_attempt_unknown'):
            deposit(service, payload, 'bob')
    finally:
        service.close()


def test_owner_loss_after_deposit_prevents_success_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    payload = make_source()
    original = service._legacy_put_source
    def lose_owner(*args, **kwargs):
        receipt = original(*args, **kwargs)
        service.db.execute('DELETE FROM source_attempt_owners WHERE source_id=?', (payload['source_id'],))
        service.db.commit()
        return receipt
    monkeypatch.setattr(service, '_legacy_put_source', lose_owner)
    try:
        with pytest.raises(core.Rejected, match='source_owner_journal_invalid'):
            deposit(service, payload, 'alice')
        assert service.db.execute('SELECT state FROM source_attempts').fetchone()[0] == 'scoped_uncertain'
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
    finally:
        service.close()


def test_scoped_deposit_readback_rejects_corrupted_bytes_without_deletion(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    source = make_source()
    publish = service._legacy_put_source
    def corrupt_after_commit(*args, **kwargs):
        receipt = publish(*args, **kwargs)
        path = service.sources / (source['source_id'] + '.zip')
        data = bytearray(path.read_bytes()); data[0] ^= 1
        path.write_bytes(data)
        return receipt
    monkeypatch.setattr(service, '_legacy_put_source', corrupt_after_commit)
    try:
        with pytest.raises(core.Rejected, match='source_readback_failed'):
            deposit(service, source, 'alice')
        assert service.db.execute('SELECT state FROM source_attempts').fetchone()[0] == 'scoped_uncertain'
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
        assert (service.sources / (source['source_id'] + '.zip')).is_file()
        with pytest.raises(core.Rejected, match='source_attempt_unknown'):
            deposit(service, source, 'alice')
    finally:
        service.close()


def test_scoped_evidence_rejects_root_swap_during_descriptor_read(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', True)
    service = core.Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    source = make_source()
    receipt = deposit(service, source, 'alice')
    original_open = os.open
    moved = tmp_path / 'moved-worker'
    def swap_after_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == source['source_id'] + '.zip' and kwargs.get('dir_fd') is not None:
            service.root.rename(moved)
            service.root.mkdir(mode=0o700)
            service.sources.mkdir(mode=0o700)
        return fd
    monkeypatch.setattr(os, 'open', swap_after_open)
    try:
        result = service.scoped_source_evidence(source['source_id'], 'demo', 'default', source['source_digest'],
                                                receipt['source_binding'], 'alice', {'demo'})
        assert result == {'state': 'unknown', 'authority': 'observation_only'}
        assert (moved / 'sources' / (source['source_id'] + '.zip')).is_file()
    finally:
        service.close()


@pytest.mark.parametrize('failure', ['file_fsync', 'directory_fsync', 'source_commit_after_success'])
def test_scoped_source_publication_uncertainty_keeps_original_attempt_and_file(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    source = make_source()
    events = []
    actual_sync = os.fsync
    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_ino != service.sources.stat().st_ino:
            return actual_sync(fd)
        kind = 'directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file'
        events.append(kind)
        assert service.db.execute('SELECT state FROM source_attempts WHERE source_id=?',
                                  (source['source_id'],)).fetchone()[0] == 'scoped_uncertain'
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
        if failure == kind + '_fsync':
            raise OSError('synthetic sync failure')
        return actual_sync(fd)
    class CommitAfterSuccess:
        def __init__(self, db): self.db, self.source_written, self.failed = db, False, False
        def __getattr__(self, name): return getattr(self.db, name)
        def execute(self, sql, *args):
            result = self.db.execute(sql, *args)
            if sql.startswith('INSERT INTO sources '): self.source_written = True
            return result
        def commit(self):
            self.db.commit()
            if self.source_written and not self.failed:
                self.failed = True
                raise OSError('synthetic lost commit acknowledgement')
    if failure == 'source_commit_after_success':
        service.db = CommitAfterSuccess(service.db)
    monkeypatch.setattr(os, 'fsync', sync)
    try:
        with pytest.raises(OSError): deposit(service, source, 'alice')
        assert events == (['file'] if failure == 'file_fsync' else ['file', 'directory'])
        assert (service.sources / (source['source_id'] + '.zip')).is_file()
        assert service.source_attempt(source['source_id'], 'demo', principal='alice', recipe='default')['state'] == 'uncertain'
        with pytest.raises(core.Rejected, match='source_attempt_unknown'):
            deposit(service, source, 'alice')
        expected_rows = 1 if failure == 'source_commit_after_success' else 0
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == expected_rows
    finally:
        service.close()
    reopened = core.Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    try:
        assert reopened.source_attempt(source['source_id'], 'demo', principal='alice', recipe='default')['state'] == 'uncertain'
        assert (reopened.sources / (source['source_id'] + '.zip')).is_file()
    finally:
        reopened.close()


def test_scoped_source_parent_replacement_is_uncertain_and_never_unlinked(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    source = make_source()
    sync = os.fsync
    displaced = tmp_path / 'worker' / 'displaced-sources'
    def replace_after_sync(fd):
        result = sync(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_ino == service.sources.stat().st_ino:
            service.sources.rename(displaced)
            service.sources.mkdir(mode=0o700)
        return result
    monkeypatch.setattr(os, 'fsync', replace_after_sync)
    try:
        with pytest.raises(core.Rejected, match='source_file_identity_drift'):
            deposit(service, source, 'alice')
        assert (displaced / (source['source_id'] + '.zip')).is_file()
        assert not (service.sources / (source['source_id'] + '.zip')).exists()
        assert service.source_attempt(source['source_id'], 'demo', principal='alice', recipe='default')['state'] == 'unknown'
        assert service.db.execute('SELECT state FROM source_attempts').fetchone()[0] == 'scoped_uncertain'
        with pytest.raises(core.Rejected, match='source_attempt_unknown'):
            deposit(service, source, 'alice')
    finally:
        service.close()


def test_worker_root_replacement_between_intent_and_file_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    source = make_source()
    displaced = tmp_path / 'original-worker'
    publish = service._scoped_publish_source
    def replace_before_open(*args):
        service.root.rename(displaced)
        service.root.mkdir(mode=0o700)
        service.sources.mkdir(mode=0o700)
        return publish(*args)
    monkeypatch.setattr(service, '_scoped_publish_source', replace_before_open)
    try:
        with pytest.raises(core.Rejected, match='source_parent_identity_drift'):
            deposit(service, source, 'alice')
        assert service.db.execute('SELECT state FROM source_attempts').fetchone()[0] == 'scoped_uncertain'
        assert not (service.sources / (source['source_id'] + '.zip')).exists()
        assert not (displaced / 'sources' / (source['source_id'] + '.zip')).exists()
    finally:
        service.close()


def test_source_parent_replaced_after_restart_never_proves_or_collects(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', True)
    root = tmp_path / 'worker'
    service = core.Service(root, {'demo': profile()}, Backend())
    source = make_source()
    receipt = deposit(service, source, 'alice')
    service.close()
    displaced = root / 'original-sources'
    (root / 'sources').rename(displaced)
    (root / 'sources').mkdir(mode=0o700)
    os.link(displaced / (source['source_id'] + '.zip'), root / 'sources' / (source['source_id'] + '.zip'))
    reopened = core.Service(root, {'demo': profile()}, Backend())
    try:
        assert reopened.source_attempt(source['source_id'], 'demo', principal='alice', recipe='default')['state'] == 'unknown'
        assert reopened.scoped_source_evidence(source['source_id'], 'demo', 'default', source['source_digest'],
               receipt['source_binding'], 'alice', {'demo'})['state'] == 'unknown'
        with pytest.raises(core.Rejected, match='source_parent_identity_drift'):
            reopened.cleanup_sources(now=10**12)
        assert (displaced / (source['source_id'] + '.zip')).is_file()
        assert (root / 'sources' / (source['source_id'] + '.zip')).is_file()
    finally:
        reopened.close()


def test_worker_root_replaced_after_restart_is_not_original(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    root = tmp_path / 'worker'
    service = core.Service(root, {'demo': profile()}, Backend())
    source = make_source()
    deposit(service, source, 'alice')
    service.close()
    original = tmp_path / 'original-worker'
    root.rename(original)
    shutil.copytree(original, root)
    reopened = core.Service(root, {'demo': profile()}, Backend())
    try:
        assert reopened.source_attempt(source['source_id'], 'demo', principal='alice', recipe='default')['state'] == 'unknown'
        with pytest.raises(core.Rejected, match='source_parent_identity_drift'):
            reopened.cleanup_sources(now=10**12)
        assert (original / 'sources' / (source['source_id'] + '.zip')).is_file()
    finally:
        reopened.close()


def test_scoped_source_path_swap_never_unlinks_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    source = make_source()
    target = tmp_path / 'unrelated'; target.write_text('preserve')
    saved = tmp_path / 'worker' / 'saved-original.zip'
    sync = os.fsync
    def swap_after_sync(fd):
        result = sync(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_ino == service.sources.stat().st_ino:
            path = service.sources / (source['source_id'] + '.zip')
            path.rename(saved)
            path.symlink_to(target)
        return result
    monkeypatch.setattr(os, 'fsync', swap_after_sync)
    try:
        with pytest.raises(core.Rejected, match='source_file_identity_drift'):
            deposit(service, source, 'alice')
        assert target.read_text() == 'preserve' and saved.is_file()
        assert (service.sources / (source['source_id'] + '.zip')).is_symlink()
        assert service.source_attempt(source['source_id'], 'demo', principal='alice', recipe='default')['state'] == 'uncertain'
    finally:
        service.close()


def test_scoped_source_requires_private_parent_before_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    source = make_source()
    service.sources.chmod(0o755)
    try:
        with pytest.raises(core.Rejected, match='source_parent_identity_drift'):
            deposit(service, source, 'alice')
        assert service.source_attempt(source['source_id'], 'demo', principal='alice', recipe='default')['state'] == 'unknown'
        assert not (service.sources / (source['source_id'] + '.zip')).exists()
    finally:
        service.sources.chmod(0o700)
        service.close()


def test_worker_root_sync_failure_precedes_intent_and_source_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path / 'worker', {'demo': profile()}, Backend())
    source = make_source()
    actual_sync = os.fsync
    def fail_worker_root(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_ino == service.root.stat().st_ino:
            raise OSError('synthetic worker root sync failure')
        return actual_sync(fd)
    monkeypatch.setattr(os, 'fsync', fail_worker_root)
    try:
        with pytest.raises(OSError, match='worker root sync failure'):
            deposit(service, source, 'alice')
        assert service.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_attempts'").fetchone() is None
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
        assert not (service.sources / (source['source_id'] + '.zip')).exists()
    finally:
        service.close()


@pytest.mark.parametrize('stage', ['worker_root_synced', 'before_file', 'file_synced', 'directory_synced',
                                  'source_committed', 'observation_committed'])
def test_scoped_source_process_crash_keeps_original_attempt(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    root = tmp_path / 'worker'
    source = make_source()
    script = r'''
import json, os, stat, sys
from pathlib import Path
from hub_staging_service import core
from tests.test_staging_service_core import Backend, profile
from tests.test_v2_scoped_source_prototype import deposit
root, stage, source = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
core._PROFILE_PROTOCOL_READY = True
core._SOURCE_ATTEMPT_ENABLED = True
service = core.Service(root, {'demo': profile()}, Backend())
actual_sync = os.fsync
def sync(fd):
    result = actual_sync(fd)
    if stat.S_ISDIR(os.fstat(fd).st_mode) and os.fstat(fd).st_ino != service.sources.stat().st_ino:
        if stage == 'worker_root_synced': os._exit(97)
        return result
    kind = 'directory_synced' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file_synced'
    if stage == kind:
        os._exit(97)
    return result
os.fsync = sync
class CrashAfterCommit:
    def __init__(self, db): self.db, self.tag = db, None
    def __getattr__(self, name): return getattr(self.db, name)
    def execute(self, sql, *args):
        result = self.db.execute(sql, *args)
        if sql.startswith('INSERT INTO sources '): self.tag = 'source_committed'
        if sql.startswith('UPDATE source_attempts SET state='): self.tag = 'observation_committed'
        return result
    def commit(self):
        self.db.commit()
        if stage == self.tag: os._exit(97)
service.db = CrashAfterCommit(service.db)
if stage == 'before_file':
    service._legacy_put_source = lambda *_args, **_kw: os._exit(97)
deposit(service, source, 'alice')
raise SystemExit('injected crash did not occur')
'''
    completed = subprocess.run([sys.executable, '-c', script, str(root), stage, json.dumps(source)],
                               capture_output=True, timeout=20, check=False)
    assert completed.returncode == 97, completed.stderr.decode(errors='replace')
    reopened = core.Service(root, {'demo': profile()}, Backend())
    try:
        state = reopened.source_attempt(source['source_id'], 'demo', principal='alice', recipe='default')['state']
        expected_state = ('unknown' if stage == 'worker_root_synced' else
                          'historical_observation' if stage == 'observation_committed' else 'uncertain')
        assert state == expected_state
        assert (reopened.sources / (source['source_id'] + '.zip')).exists() is (stage not in ('worker_root_synced', 'before_file'))
        expected_rows = 1 if stage in ('source_committed', 'observation_committed') else 0
        assert reopened.db.execute('SELECT count(*) FROM sources').fetchone()[0] == expected_rows
        if stage != 'worker_root_synced':
            with pytest.raises(core.Rejected, match='source_attempt_unknown'):
                deposit(reopened, source, 'alice')
    finally:
        reopened.close()


def test_scoped_source_evidence_hides_foreign_missing_mismatch_and_uncertain(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    monkeypatch.setattr(core, '_SOURCE_EVIDENCE_ENABLED', True)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    source = make_source()
    try:
        receipt = deposit(service, source, 'alice')
        binding, digest = receipt['source_binding'], source['source_digest']
        def observe(source_id, principal, wanted=digest, proof=binding):
            return service.scoped_source_evidence(source_id, 'demo', 'default', wanted, proof, principal, {'demo'})
        assert observe(source['source_id'], 'alice')['state'] == 'present'
        assert observe(source['source_id'], 'bob')['state'] == 'unknown'
        assert observe(uid(), 'alice')['state'] == 'unknown'
        wrong = copy.deepcopy(binding); wrong['archive_sha256'] = '0' * 64
        assert observe(source['source_id'], 'alice', '0' * 64, wrong)['state'] == 'unknown'
        for column in ('source_inode', 'source_created'):
            original = service.db.execute(f'SELECT {column} FROM source_attempts WHERE source_id=?',
                                          (source['source_id'],)).fetchone()[0]
            service.db.execute(f'UPDATE source_attempts SET {column}=? WHERE source_id=?', (original + 1, source['source_id']))
            service.db.commit()
            assert observe(source['source_id'], 'alice')['state'] == 'unknown'
            service.db.execute(f'UPDATE source_attempts SET {column}=? WHERE source_id=?', (original, source['source_id']))
            service.db.commit()
        service.db.execute("UPDATE source_attempts SET state='scoped_uncertain' WHERE source_id=?", (source['source_id'],))
        service.db.commit()
        assert observe(source['source_id'], 'alice')['state'] == 'unknown'
        service.db.execute('DELETE FROM source_attempt_owners WHERE source_id=?', (source['source_id'],))
        service.db.commit()
        with pytest.raises(core.Rejected, match='source_owner_journal_invalid'):
            observe(source['source_id'], 'alice')
    finally:
        service.close()


def test_scoped_job_resource_and_cleanup_lifecycle(tmp_path, monkeypatch):
    from engineering_hub.modular_staging_profile import from_worker_recipe
    from hub_staging_service.profile_binding import profile_digest
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path, {'demo': {'recipes': {'default': profile(), 'other': profile()}}}, Backend())
    source = make_source()
    try:
        receipt = deposit(service, source, 'alice')
        request = job(base64.b64decode(source['source_zip_base64']))
        request.pop('source_zip_base64')
        request.update(source_id=source['source_id'], source_commit=source['source_commit'],
                       require_source_binding=True, expected_source_binding=receipt['source_binding'],
                       reviewed_profile={'project': 'demo', 'recipe': 'default', 'generation': 1,
                           'digest': profile_digest(from_worker_recipe(service.projects['demo']['default']))})
        grants = {'demo': {'default', 'other'}}
        changed_generation = copy.deepcopy(request)
        changed_generation['reviewed_profile']['generation'] = 2
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service._submit_v2(changed_generation, principal='alice', projects={'demo'}, recipes=grants)
        changed_recipe = copy.deepcopy(request)
        changed_recipe['recipe'] = 'other'
        changed_recipe['reviewed_profile']['recipe'] = 'other'
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service._submit_v2(changed_recipe, principal='alice', projects={'demo'}, recipes=grants)
        changed_digest = copy.deepcopy(request)
        changed_digest['reviewed_profile']['digest'] = 'sha256:' + '0' * 64
        with pytest.raises(core.Rejected, match='reviewed_profile_mismatch'):
            service._submit_v2(changed_digest, principal='alice', projects={'demo'}, recipes=grants)
        assert service.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        submitted = service._submit_v2(request, principal='alice', projects={'demo'}, recipes=grants)
        service.wait()
        assert service.scoped_status(request['job_id'], 'alice', {'demo'}, grants)['state'] == 'succeeded'
        assert submitted['job_id'] == request['job_id']
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.scoped_status(request['job_id'], 'bob', {'demo'}, grants)
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service._submit_v2(request, principal='bob', projects={'demo'}, recipes=grants)
        preview = request['resource_ids']['preview']
        assert service.scoped_file(preview, 'index.html', 'alice', {'demo'}, grants)
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.scoped_metadata(preview, 'bob', {'demo'}, grants)
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.scoped_metadata(preview, 'alice', {'demo'}, {})
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.legacy_status(request['job_id'])
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.legacy_file(preview, 'index.html')
        clean = {'command_id': uid(), 'idempotency_key': uid(),
                 'resources': [{'resource_id': preview, 'expected_revision': 1}]}
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.cleanup(clean)
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.cleanup(clean, principal='bob', projects={'demo'}, recipes=grants)
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.cleanup(clean, principal='alice', projects={'demo'}, recipes={})
        result = service.cleanup(clean, principal='alice', projects={'demo'}, recipes=grants)
        assert result['state'] == 'succeeded'
        assert service.cleanup(clean, principal='alice', projects={'demo'}, recipes=grants) == result
        assert service.scoped_operation(clean['command_id'], 'alice', {'demo'}, grants) == result
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.scoped_operation(clean['command_id'], 'bob', {'demo'}, grants)
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.scoped_operation(clean['command_id'], 'alice', {'demo'}, {})
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.legacy_operation(clean['command_id'])
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.cleanup(clean)
        remaining = {'command_id': uid(), 'idempotency_key': uid(), 'resources': [
            {'resource_id': request['resource_ids'][kind], 'expected_revision': 1}
            for kind in ('workspace', 'build', 'test_run')]}
        assert service.cleanup(remaining, principal='alice', projects={'demo'}, recipes=grants)['state'] == 'succeeded'
        for table in ('source_attempts', 'source_attempt_owners', 'source_attempt_witnesses'):
            service.db.execute(f'DROP TABLE {table}')
        service.db.commit()
        with pytest.raises(core.Rejected, match='source_owner_journal_invalid'):
            service.cleanup_sources(now=10**12)
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
        service.db.execute('DROP TABLE scoped_jobs')
        service.db.execute('DROP TABLE scoped_cleanups')
        service.db.commit()
        with pytest.raises(core.Rejected, match='scoped_journal_invalid'):
            service.cleanup_sources(now=10**12)
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
        service.db.execute('DELETE FROM operations WHERE job_id=?', (request['job_id'],))
        service.db.commit()
        service.active = request['command_id']
        assert service.legacy_active() is None
        service.active = None
        retained = request['resource_ids']['build']
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.legacy_metadata(retained)
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.legacy_file(retained, 'index.html')
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.legacy_diagnostic(retained, '0' * 64, 'index.html')
        with pytest.raises(core.Rejected, match='scoped_not_found'):
            service.legacy_status(request['job_id'])
    finally:
        service.close()


def receipted_scoped_job(service):
    source = make_source()
    receipt = deposit(service, source, 'alice')
    request = job(base64.b64decode(source['source_zip_base64']))
    request.pop('source_zip_base64')
    request.update(source_id=source['source_id'], source_commit=source['source_commit'],
                   require_source_binding=True, expected_source_binding=receipt['source_binding'],
                   reviewed_profile=reviewed(service))
    grants = {'demo': {'default'}}
    service._submit_v2(request, principal='alice', projects={'demo'}, recipes=grants)
    service.wait()
    assert service.scoped_status(request['job_id'], 'alice', {'demo'}, grants)['state'] == 'succeeded'
    clean = {'command_id': uid(), 'idempotency_key': uid(),
             'resources': [{'resource_id': request['resource_ids']['preview'], 'expected_revision': 1}]}
    return request, clean, grants


def test_scoped_cleanup_receipt_survives_restart_and_original_id_replay(configured):
    service = configured(slots=8)
    request, clean, grants = receipted_scoped_job(service)
    result = service.cleanup(clean, principal='alice', projects={'demo'}, recipes=grants)
    service._receipt_inventory()
    assert service._receipt_fault is None
    close(service)
    reopened = configured(slots=8)
    assert reopened._receipt_fault is None
    assert reopened.cleanup(clean, principal='alice', projects={'demo'}, recipes=grants) == result
    assert reopened.backend.calls == 0
    with pytest.raises(core.Rejected, match='scoped_not_found'):
        reopened.cleanup(clean, principal='bob', projects={'demo'}, recipes=grants)


@pytest.mark.parametrize('damage', ['principal', 'job', 'extra', 'cleanup_owner'])
def test_scoped_cleanup_receipt_detects_ownership_tampering(configured, damage):
    service = configured(slots=8)
    _request, clean, grants = receipted_scoped_job(service)
    service.cleanup(clean, principal='alice', projects={'demo'}, recipes=grants)
    if damage == 'cleanup_owner':
        service.db.execute('DELETE FROM scoped_cleanups WHERE command_id=?', (clean['command_id'],))
    else:
        row = service.db.execute('SELECT payload FROM operations WHERE command_id=?', (clean['command_id'],)).fetchone()
        saved = json.loads(row['payload'])
        if damage == 'principal': saved['_receiver_principal'] = 'bob'
        if damage == 'job': saved['_receiver_job'] = uid()
        if damage == 'extra': saved['unexpected'] = True
        service.db.execute('UPDATE operations SET payload=? WHERE command_id=?', (json.dumps(saved), clean['command_id']))
    service.db.commit()
    service._receipt_inventory()
    assert service._receipt_fault == 'terminal_receipt_recovery_required'
    close(service)
    reopened = configured(slots=8)
    assert reopened._receipt_fault == 'terminal_receipt_recovery_required'


@pytest.mark.parametrize('damage', ['principal', 'extra', 'owner', 'marker_and_table'])
def test_scoped_job_receipt_detects_ownership_tampering(configured, damage):
    service = configured(slots=8)
    request, _clean, _grants = receipted_scoped_job(service)
    if damage == 'owner':
        service.db.execute('DELETE FROM scoped_jobs WHERE job_id=?', (request['job_id'],))
    else:
        row = service.db.execute('SELECT payload FROM operations WHERE job_id=?', (request['job_id'],)).fetchone()
        saved = json.loads(row['payload'])
        if damage == 'principal': saved['_receiver_principal'] = 'bob'
        if damage == 'extra': saved['unexpected'] = True
        if damage == 'marker_and_table':
            saved.pop('_receiver_principal')
            service.db.execute('DROP TABLE scoped_jobs')
        service.db.execute('UPDATE operations SET payload=? WHERE job_id=?', (json.dumps(saved), request['job_id']))
    service.db.commit()
    service._receipt_inventory()
    assert service._receipt_fault == 'terminal_receipt_recovery_required'
    close(service)
    reopened = configured(slots=8)
    assert reopened._receipt_fault == 'terminal_receipt_recovery_required'


def test_scoped_credential_files_require_distinct_identities_and_tokens(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.write_text('synthetic-secret-1'); second.write_text('synthetic-secret-2')
    first.chmod(0o600); second.chmod(0o600)
    records = [{'principal': 'alice', 'token_file': str(first), 'projects': ['demo']},
               {'principal': 'bob', 'token_file': str(second), 'projects': ['demo']}]
    assert len(scoped_credentials(records, {'demo': {}}, 'synthetic-legacy-secret')) == 2
    second.write_text('synthetic-secret-1')
    with pytest.raises(core.Rejected, match='invalid_v2_credentials'):
        scoped_credentials(records, {'demo': {}}, 'synthetic-legacy-secret')


def test_legacy_active_preserved_for_valid_legacy_operation(tmp_path):
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    payload = job()
    try:
        service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
            (payload['command_id'], 'synthetic', payload['job_id'], 'running', json.dumps(payload), '{}'))
        service.db.commit()
        service.active = payload['command_id']
        assert service.legacy_active() == payload['command_id']
        service.db.execute('UPDATE operations SET job_id=NULL WHERE command_id=?', (payload['command_id'],))
        service.db.commit()
        assert service.legacy_active() is None
        service.active = None
    finally:
        service.close()


def test_unscoped_v2_never_advertises_ready_or_accepts_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    from hub_staging_service import runtime_inventory
    monkeypatch.setattr(runtime_inventory, 'collect_runtime', lambda *_args: {'state': 'synthetic'})
    token = 'synthetic-legacy-secret'
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(service, token, {ipaddress.ip_address('127.0.0.1')}))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        with httpx.Client(trust_env=False, timeout=5, headers={'Authorization': 'Bearer ' + token}) as client:
            assert client.get(base + '/v2/status').json()['profile_protocol_ready'] is False
            assert client.post(base + '/v2/source-attempts', json=make_source()).status_code == 404
            assert client.post(base + '/v%32/source-attempts', json=make_source()).status_code == 404
            assert client.get(base + '/v1/status').status_code == 200
            assert client.get(base + '/v1/runtime').json() == {'state': 'synthetic'}
    finally:
        server.shutdown(); server.server_close(); thread.join(5); service.close()
        assert not thread.is_alive()


@pytest.mark.asyncio
async def test_existing_client_reads_complete_unscoped_status_as_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    wire = Wire(tmp_path)
    try:
        status = await wire.controller.client.status_v2()
        assert status['profile_protocol_ready'] is False
        assert status['projects'] == {'engineering-hub': ['static']}
    finally:
        await wire.close()


@pytest.mark.parametrize('damage', ['owner_row', 'witness_row', 'attempt_row', 'owner_table', 'both_owner_tables'])
def test_partial_ownership_loss_never_allows_cleanup_or_legacy_adoption(tmp_path, monkeypatch, damage):
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    service = core.Service(tmp_path, {'demo': profile()}, Backend())
    payload = make_source()
    try:
        deposit(service, payload, 'alice')
        if damage.endswith('_row'):
            table = {'owner_row': 'source_attempt_owners', 'witness_row': 'source_attempt_witnesses',
                     'attempt_row': 'source_attempts'}[damage]
            service.db.execute(f'DELETE FROM {table} WHERE source_id=?', (payload['source_id'],))
        else:
            service.db.execute('DROP TABLE source_attempt_owners')
            if damage == 'both_owner_tables':
                service.db.execute('DROP TABLE source_attempt_witnesses')
        service.db.commit()
        with pytest.raises(core.Rejected, match='source_owner_journal_invalid'):
            service.cleanup_sources(now=10**12)
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
        with pytest.raises(core.Rejected, match='source_owner_journal_invalid'):
            service.put_source(payload)
        request = job()
        request.pop('source_zip_base64')
        request.update(source_id=payload['source_id'], source_digest=payload['source_digest'],
                       source_commit=payload['source_commit'])
        with pytest.raises(core.Rejected, match='source_owner_journal_invalid'):
            service._legacy_submit(request)
    finally:
        service.close()
