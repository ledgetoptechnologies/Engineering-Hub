import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
import uuid
import zipfile

from hub_staging_service.core import Service, Rejected, extract, relative, recipe, storage_limits


def uid():
    return str(uuid.uuid4())


def profile():
    return {'image_id': 'sha256:' + 'a' * 64, 'build_argv': ['npm', 'run', 'build'],
            'test_argv': ['npm', 'test'], 'timeout_seconds': 30, 'memory_mb': 128,
            'cpus': 1, 'pids': 32, 'output_bytes': 1048576, 'log_bytes': 65536}


def zipped(name='index.html', data=b'<h1>hello</h1>', mode=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as bundle:
        item = zipfile.ZipInfo(name)
        item.filename = name
        if mode is not None:
            item.create_system = 3
            item.external_attr = mode << 16
        bundle.writestr(item, data)
    return output.getvalue()


def job(raw=None):
    raw = zipped() if raw is None else raw
    return {'job_id': uid(), 'command_id': uid(), 'idempotency_key': uid(), 'project': 'demo',
            'recipe': 'default', 'source_commit': 'b' * 40, 'source_digest': hashlib.sha256(raw).hexdigest(),
            'source_zip_base64': base64.b64encode(raw).decode(),
            'resource_ids': {kind: uid() for kind in ('workspace', 'build', 'test_run', 'preview')}}


class Backend:
    def run(self, paths, profile, command_id):
        for kind in ('build', 'preview'):
            (paths[kind] / 'index.html').write_text('<h1>built</h1>')
        (paths['test_run'] / 'test.json').write_text('{"passed":true}')
        return {'preview_ready': True}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(self.temp.name, {'demo': profile()}, Backend())

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_real_archive_build_registry_and_immutable_file(self):
        payload = job()
        self.service.submit(payload); self.service.wait()
        status = self.service.status(payload['job_id'])
        self.assertEqual(status['state'], 'succeeded')
        self.assertEqual(status['result']['source_commit_verification'], 'declared')
        resource = payload['resource_ids']['preview']
        self.assertEqual(self.service.file(resource, 'index.html'), b'<h1>built</h1>')
        (self.service.resources / resource / 'index.html').write_text('changed')
        with self.assertRaises(Rejected):
            self.service.file(resource, 'index.html')

    def test_repeated_identity_does_not_execute_twice_and_changed_payload_conflicts(self):
        payload = job()
        self.service.submit(payload); self.service.wait()
        original = self.service.status(payload['job_id'])
        self.assertEqual(self.service.submit(payload), original)
        payload['source_commit'] = 'c' * 40
        with self.assertRaises(Rejected) as caught:
            self.service.submit(payload)
        self.assertEqual(caught.exception.status, 409)

    def test_busy_is_not_another_queue(self):
        entered, release = threading.Event(), threading.Event()
        class Blocked(Backend):
            def run(inner, *args):
                entered.set(); release.wait(3)
                return super().run(*args)
        self.service.backend = Blocked()
        self.service.submit(job()); self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(Rejected) as caught:
                self.service.submit(job())
            self.assertEqual(caught.exception.status, 409)
        finally:
            release.set(); self.service.wait()

    def test_restart_running_becomes_held_never_replayed(self):
        payload = job()
        self.service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
            (payload['command_id'], 'digest', payload['job_id'], 'running', json.dumps(payload), '{}'))
        self.service.db.commit(); self.service.close()
        self.service = Service(self.temp.name, {'demo': profile()}, Backend())
        self.assertEqual(self.service.status(payload['job_id'])['state'], 'held')
        with self.assertRaises(Rejected):
            self.service.submit(job())

    def test_deposit_idempotency_project_binding_and_job_without_blob(self):
        payload = job()
        source = {'source_id': uid(), 'project': 'demo', 'source_digest': payload['source_digest'],
                  'source_zip_base64': payload.pop('source_zip_base64')}
        first = self.service.put_source(source)
        self.assertEqual(self.service.put_source(source), first)
        payload['source_id'] = source['source_id']
        self.service.submit(payload); self.service.wait()
        self.assertEqual(self.service.status(payload['job_id'])['state'], 'succeeded')
        self.service.cleanup_sources(now=10**12)
        self.assertTrue((self.service.sources / (source['source_id'] + '.zip')).exists())

    def test_expired_unreferenced_deposit_collected_unknown_files_untouched(self):
        payload = job()
        source = {'source_id': uid(), 'project': 'demo', 'source_digest': payload['source_digest'],
                  'source_zip_base64': payload['source_zip_base64']}
        self.service.put_source(source)
        unknown = self.service.sources / 'unknown'
        unknown.write_text('preserve')
        self.service.cleanup_sources(now=10**12)
        self.assertFalse((self.service.sources / (source['source_id'] + '.zip')).exists())
        self.assertTrue(unknown.exists())

    def deposited_job(self, source=None):
        payload = job()
        source = source or {'source_id': uid(), 'project': 'demo', 'source_digest': payload['source_digest'],
                            'source_zip_base64': payload['source_zip_base64']}
        self.service.put_source(source)
        del payload['source_zip_base64']
        payload['source_id'] = source['source_id']
        self.service.submit(payload); self.service.wait()
        return payload, source

    def delete_job_resources(self, payload):
        command = {'command_id': uid(), 'idempotency_key': uid(),
                   'resources': [{'resource_id': value, 'expected_revision': 1}
                                 for value in payload['resource_ids'].values()]}
        return command, self.service.cleanup(command)

    def test_expired_retired_source_collected_but_job_and_cleanup_receipts_survive(self):
        from unittest.mock import patch
        payload, source = self.deposited_job()
        original_job = self.service.status(payload['job_id'])
        command, original_cleanup = self.delete_job_resources(payload)
        unknown = self.service.sources / 'unowned.zip'
        unknown.write_bytes(b'preserve')
        self.service.cleanup_sources(now=10**12)
        assert not (self.service.sources / (source['source_id'] + '.zip')).exists()
        assert unknown.read_bytes() == b'preserve'
        with patch.object(self.service.backend, 'run', side_effect=AssertionError('no replay')):
            assert self.service.submit(payload) == original_job
            assert self.service.cleanup(command) == original_cleanup
            assert self.service.operation(command['command_id']) == original_cleanup
        self.service.cleanup_sources(now=10**12)
        assert self.service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0

    def test_source_retained_until_every_referencing_job_has_all_resources_deleted(self):
        first, source = self.deposited_job()
        second, _ = self.deposited_job(source)
        self.delete_job_resources(first)
        self.service.cleanup_sources(now=10**12)
        assert (self.service.sources / (source['source_id'] + '.zip')).exists()
        self.delete_job_resources(second)
        self.service.cleanup_sources(now=10**12)
        assert not (self.service.sources / (source['source_id'] + '.zip')).exists()

    def test_held_missing_or_retained_resource_facts_prevent_source_collection(self):
        payload, source = self.deposited_job()
        self.delete_job_resources(payload)
        path = self.service.sources / (source['source_id'] + '.zip')
        for state in ('accepted', 'running', 'held'):
            self.service.db.execute('UPDATE operations SET state=? WHERE job_id=?', (state, payload['job_id']))
            self.service.db.commit()
            self.service.cleanup_sources(now=10**12)
            assert path.exists()
        self.service.db.execute("UPDATE operations SET state='succeeded' WHERE job_id=?", (payload['job_id'],))
        identity = payload['resource_ids']['preview']
        for state, refs in [('ready', '[]'), ('deleting', '[]'), ('delete_failed', '[]'), ('deleted', '["pinned"]')]:
            self.service.db.execute('UPDATE resources SET state=?,refs=? WHERE id=?', (state, refs, identity))
            self.service.db.commit()
            self.service.cleanup_sources(now=10**12)
            assert path.exists()
        self.service.db.execute('DELETE FROM resources WHERE id=?', (identity,))
        self.service.db.commit()
        self.service.cleanup_sources(now=10**12)
        assert path.exists()

    def test_expired_failed_job_source_collected_only_after_confirmed_cleanup(self):
        payload, source = self.deposited_job()
        self.service.db.execute("UPDATE operations SET state='failed' WHERE job_id=?", (payload['job_id'],))
        self.service.db.commit()
        self.service.cleanup_sources(now=10**12)
        assert (self.service.sources / (source['source_id'] + '.zip')).exists()
        self.delete_job_resources(payload)
        self.service.cleanup_sources(now=10**12)
        assert not (self.service.sources / (source['source_id'] + '.zip')).exists()

    def test_expired_source_identity_drift_does_not_delete_replacement(self):
        payload = job()
        source = {'source_id': uid(), 'project': 'demo', 'source_digest': payload['source_digest'],
                  'source_zip_base64': payload['source_zip_base64']}
        self.service.put_source(source)
        path = self.service.sources / (source['source_id'] + '.zip')
        # Preserve original inode at another path so replacement cannot reuse it.
        path.rename(path.with_suffix('.saved'))
        path.write_bytes(b'unknown replacement')
        with self.assertRaises(Rejected):
            self.service.cleanup_sources(now=10**12)
        assert path.read_bytes() == b'unknown replacement'

    def test_expired_source_collection_runs_on_new_deposit(self):
        payload, source = self.deposited_job()
        self.delete_job_resources(payload)
        self.service.db.execute('UPDATE sources SET created=0 WHERE id=?', (source['source_id'],))
        self.service.db.commit()
        replacement = dict(source, source_id=uid())
        self.service.put_source(replacement)
        assert not (self.service.sources / (source['source_id'] + '.zip')).exists()
        assert (self.service.sources / (replacement['source_id'] + '.zip')).exists()

    def test_digest_and_profile_denial_before_acceptance(self):
        for change in ({'source_digest': '0' * 64}, {'project': 'foreign'}, {'recipe': 'shell'}):
            payload = job(); payload.update(change)
            with self.assertRaises(Rejected):
                self.service.submit(payload)
        self.assertEqual(self.service.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0], 0)

    def test_cleanup_tombstone_idempotency_and_revision_scope(self):
        payload = job()
        self.service.submit(payload); self.service.wait()
        resource = payload['resource_ids']['preview']
        cleanup = {'command_id': uid(), 'idempotency_key': uid(),
                   'resources': [{'resource_id': resource, 'expected_revision': 2}]}
        with self.assertRaises(Rejected):
            self.service.cleanup(cleanup)
        cleanup['resources'][0]['expected_revision'] = 1
        first = self.service.cleanup(cleanup)
        self.assertEqual(self.service.cleanup(cleanup), first)
        self.assertEqual(self.service.metadata(resource)['state'], 'deleted')
        self.assertFalse((self.service.resources / resource).exists())
        self.assertTrue((self.service.resources / payload['resource_ids']['workspace']).exists())

    def test_original_cleanup_receipt_can_be_read_after_lost_ack_and_restart(self):
        from unittest.mock import patch
        payload = job()
        self.service.submit(payload); self.service.wait()
        resource = payload['resource_ids']['preview']
        cleanup = {'command_id': uid(), 'idempotency_key': uid(),
                   'resources': [{'resource_id': resource, 'expected_revision': 1}]}
        original = self.service.cleanup(cleanup)  # Caller loses this acknowledgment.
        self.service.close()
        self.service = Service(self.temp.name, {'demo': profile()}, Backend())
        with patch('hub_staging_service.core.shutil.rmtree', side_effect=AssertionError('GET must not delete')):
            assert self.service.operation(cleanup['command_id']) == original
            assert self.service.operation(cleanup['command_id']) == original
        assert self.service.metadata(resource)['state'] == 'deleted'

    def test_operation_missing_invalid_or_job_identity_is_not_a_cleanup_receipt(self):
        with self.assertRaises(Rejected) as error:
            self.service.operation(uid())
        assert error.exception.status == 404
        with self.assertRaises(Rejected):
            self.service.operation('../not-a-command')
        payload = job()
        self.service.submit(payload); self.service.wait()
        with self.assertRaises(Rejected) as error:
            self.service.operation(payload['command_id'])
        assert error.exception.code == 'operation_not_cleanup'
        assert error.exception.status == 409

    def test_cleanup_foreign_resource_and_references_refused(self):
        cleanup = {'command_id': uid(), 'idempotency_key': uid(),
                   'resources': [{'resource_id': uid(), 'expected_revision': 1}]}
        with self.assertRaises(Rejected):
            self.service.cleanup(cleanup)
        payload = job(); self.service.submit(payload); self.service.wait()
        resource = payload['resource_ids']['preview']
        self.service.db.execute('UPDATE resources SET refs=? WHERE id=?', ('["held_campaign"]', resource))
        self.service.db.commit()
        cleanup['resources'][0]['resource_id'] = resource
        with self.assertRaises(Rejected):
            self.service.cleanup(cleanup)

    def test_partial_cleanup_failure_is_held_not_retried(self):
        from unittest.mock import patch
        payload = job(); self.service.submit(payload); self.service.wait()
        resource = payload['resource_ids']['preview']
        cleanup = {'command_id': uid(), 'idempotency_key': uid(),
                   'resources': [{'resource_id': resource, 'expected_revision': 1}]}
        with patch('hub_staging_service.core.shutil.rmtree', side_effect=OSError('private detail')) as remove:
            with self.assertRaises(Rejected):
                self.service.cleanup(cleanup)
            self.assertEqual(self.service.cleanup(cleanup)['state'], 'held')
            self.assertEqual(remove.call_count, 1)
        self.assertEqual(self.service.metadata(resource)['state'], 'delete_failed')
        with self.assertRaises(Rejected):
            self.service.submit(job())

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink semantics')
    def test_cleanup_symlink_cannot_reach_foreign_file(self):
        payload = job(); self.service.submit(payload); self.service.wait()
        resource = payload['resource_ids']['preview']
        foreign = Path(self.temp.name) / 'keep.txt'
        foreign.write_text('keep')
        (self.service.resources / resource / 'link').symlink_to(foreign)
        cleanup = {'command_id': uid(), 'idempotency_key': uid(),
                   'resources': [{'resource_id': resource, 'expected_revision': 1}]}
        with self.assertRaises(Rejected):
            self.service.cleanup(cleanup)
        self.assertEqual(foreign.read_text(), 'keep')

    @unittest.skipUnless(os.name == 'posix', 'POSIX advisory lock')
    def test_second_process_owner_refused_before_restart_transition(self):
        with self.assertRaises(Rejected):
            Service(self.temp.name, {'demo': profile()}, Backend())


class GuardTests(unittest.TestCase):
    def test_storage_limits_are_exact_bounded_integers_and_default_compatible(self):
        assert storage_limits() == (1024**3, 512 * 1024**2)
        assert storage_limits(64 * 1024**2, 4 * 1024**3) == (64 * 1024**2, 4 * 1024**3)
        for capacity, floor in [(True, 1024**3), (1024**3, False), (63 * 1024**2, 1024**3),
                                (1024**3, 511 * 1024**2), ('1000000000', 1024**3),
                                (1024**3, 1.5 * 1024**3)]:
            with self.assertRaises(Rejected):
                storage_limits(capacity, floor)

    def test_configured_free_floor_controls_admission_without_changing_default(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            service = Service(temporary, {'demo': profile()}, Backend(),
                              free_floor_bytes=4 * 1024**3)
            try:
                with patch('hub_staging_service.core.shutil.disk_usage') as disk:
                    disk.return_value.free = 4 * 1024**3 + 100
                    service._capacity(100)
                    with self.assertRaises(Rejected):
                        service._capacity(101)
            finally:
                service.close()

    def test_traversal_absolute_windows_and_special_zip_entries(self):
        for name in ('../escape', '/absolute', 'C:/windows', 'a\\b', 'a/../b'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary, self.assertRaises(Rejected):
                extract(zipped(name), Path(temporary))
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFCHR | 0o600, stat.S_IFIFO | 0o600):
            with tempfile.TemporaryDirectory() as temporary, self.assertRaises(Rejected):
                extract(zipped('special', b'/outside', mode), Path(temporary))

    def test_expansion_and_duplicate_paths_refused(self):
        import hub_staging_service.core as core
        from unittest.mock import patch
        with patch.object(core, 'MAX_EXPANDED', 3), self.assertRaises(Rejected):
            extract(zipped('big', b'1234'))
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as bundle:
            bundle.writestr('Case', 'a'); bundle.writestr('case', 'b')
        with self.assertRaises(Rejected):
            extract(output.getvalue())

    def test_recipe_rejects_mutable_image_and_unbounded_resources(self):
        for key, value in (('image_id', 'node:latest'), ('memory_mb', 4096), ('cpus', 4), ('build_argv', 'npm run build')):
            candidate = profile(); candidate[key] = value
            with self.assertRaises(Rejected):
                recipe(candidate)


if __name__ == '__main__':
    unittest.main()
