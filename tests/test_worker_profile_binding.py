import copy
import pytest

from engineering_hub.modular_staging_profile import from_worker_recipe, profile_digest
from hub_staging_service.profile_binding import checked_recipe_snapshot
from tests.test_staging_profile_task_binding import PROFILE


def values():
    recipe = {k: copy.deepcopy(v) for k, v in PROFILE.items() if k not in {'schema', 'cpu_millis'}}
    recipe['cpus'] = PROFILE['cpu_millis'] / 1000
    expected = {'project': '3d-viewer', 'recipe': 'static', 'generation': 1,
                'digest': profile_digest(from_worker_recipe(recipe))}
    return expected, recipe


def test_snapshot_matches_exact_effective_profile_and_is_detached():
    expected, recipe = values()
    snapshot = checked_recipe_snapshot('3d-viewer', 'static', expected, recipe)
    recipe['build_argv'][0] = 'modified'
    assert snapshot['build_argv'][0] != 'modified'
    assert profile_digest(from_worker_recipe(snapshot)) == expected['digest']


@pytest.mark.parametrize('change', ['project', 'recipe', 'generation', 'digest', 'extra'])
def test_tuple_is_closed_and_matches_reviewed_identity(change):
    expected, recipe = values()
    if change == 'project': expected['project'] = 'elsewhere'
    elif change == 'recipe': expected['recipe'] = 'other'
    elif change == 'generation': expected['generation'] = True
    elif change == 'digest': expected['digest'] = 'sha256:' + '0' * 64
    else: expected['extra'] = 1
    with pytest.raises(ValueError):
        checked_recipe_snapshot('3d-viewer', 'static', expected, recipe)


@pytest.mark.parametrize('change', ['argv', 'image', 'cpu', 'extra', 'tuple_argv'])
def test_effective_recipe_drift_is_not_accepted(change):
    expected, recipe = values()
    if change == 'argv': recipe['test_argv'].append('--different')
    elif change == 'image': recipe['image_id'] = 'sha256:' + '0' * 64
    elif change == 'cpu': recipe['cpus'] = 0.1234
    elif change == 'tuple_argv': recipe['test_argv'] = tuple(recipe['test_argv'])
    else: recipe['unexpected_runtime_setting'] = True
    with pytest.raises(ValueError):
        checked_recipe_snapshot('3d-viewer', 'static', expected, recipe)

@pytest.mark.parametrize('failure', [False, True])
def test_worker_protocol_checks_tuple_snapshots_and_journals(tmp_path, failure, monkeypatch):
    from hub_staging_service import core
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    monkeypatch.setattr(core, '_SOURCE_ATTEMPT_ENABLED', True)
    import base64
    import threading
    from hub_staging_service.core import Service, Rejected
    from hub_staging_service.source_provenance import _archive_tree, _object_id
    from tests.test_staging_service_core import Backend, job, profile, uid, zipped
    raw = zipped(mode=0o100644)
    tree = _archive_tree(raw)[0]
    obj = (f'tree {tree}\nauthor Test <test@example.invalid> 1 +0000\n'
           f'committer Test <test@example.invalid> 1 +0000\n\nfixture\n').encode()
    commit = _object_id(b'commit', obj)
    entered, proceed = threading.Event(), threading.Event()
    seen = []
    class SnapshotBackend(Backend):
        def run(self, paths, recipe, command_id):
            entered.set()
            assert proceed.wait(5)
            seen.append(recipe['build_argv'][0])
            if failure:
                raise Rejected('stage_test_failed')
            return super().run(paths, recipe, command_id)
    service = Service(tmp_path/'worker', {'demo':profile()}, SnapshotBackend())
    try:
        payload = job(raw)
        source_id = uid()
        deposit = {'profile_protocol':'hub-staging-profile.v2', 'source_id':source_id, 'project':'demo',
            'source_digest':payload['source_digest'], 'source_zip_base64':payload.pop('source_zip_base64'),
            'source_commit':commit, 'source_commit_object_base64':base64.b64encode(obj).decode()}
        monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', False)
        with pytest.raises(Rejected, match='profile_protocol_unavailable'):
            service.put_source(deposit)
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
        monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
        receipt = service.put_source(deposit)
        effective = service.projects['demo']['default']
        expected = {'project':'demo', 'recipe':'default', 'generation':1,
                    'digest':profile_digest(from_worker_recipe(effective))}
        payload.update(source_id=source_id, source_commit=commit, require_source_binding=True,
                       expected_source_binding=receipt['source_binding'], reviewed_profile=expected)
        monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', False)
        with pytest.raises(Rejected, match='profile_protocol_unavailable'):
            service.submit(payload)
        assert service.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
        wrong = copy.deepcopy(payload); wrong['reviewed_profile']['digest'] = 'sha256:'+'0'*64
        with pytest.raises(Rejected, match='reviewed_profile_mismatch'):
            service.submit(wrong)
        assert service.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
        service.submit(payload)
        assert entered.wait(5)
        service.projects['demo']['default']['build_argv'][0] = 'changed-after-admission'
        proceed.set()
        service.wait()
        result = service.status(payload['job_id'])
        assert result['state'] == ('failed' if failure else 'succeeded') and seen == ['npm']
        assert result['result']['profile_digest'] == expected['digest']
        # A changed worker recipe blocks POST replay; GET still reveals the
        # original immutable operation and no backend runs a second time.
        with pytest.raises(Rejected, match='reviewed_profile_mismatch'):
            service.submit(payload)
        assert service.status(payload['job_id']) == result and seen == ['npm']
        row = service.db.execute('SELECT payload FROM operations WHERE job_id=?', (payload['job_id'],)).fetchone()
        import json
        assert json.loads(row[0])['reviewed_profile'] == expected
    finally:
        proceed.set()
        service.close()



def test_worker_rejects_new_name_only_legacy_job(tmp_path, monkeypatch):
    from hub_staging_service import core
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    from hub_staging_service.core import Service, Rejected
    from tests.test_staging_service_core import Backend, job, profile
    service = Service(tmp_path/'worker', {'demo':profile()}, Backend())
    try:
        with pytest.raises(Rejected, match='job_schema'):
            service._submit_v2(job())
        assert service.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    finally:
        service.close()


def test_worker_reads_legacy_held_and_terminal_without_replaying(tmp_path):
    import json
    from hub_staging_service.core import Service
    from tests.test_staging_service_core import Backend, job, profile
    root = tmp_path/'worker'
    service = Service(root, {'demo':profile()}, Backend())
    old_held, old_done = job(), job()
    for payload, state in ((old_held, 'held'), (old_done, 'succeeded')):
        service.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?)',
            (payload['command_id'], 'old-fixture-identity', payload['job_id'], state, json.dumps(payload), '{}'))
    service.db.commit()
    service.close()
    reopened = Service(root, {'demo':profile()}, Backend())
    try:
        assert reopened.status(old_held['job_id'])['state'] == 'held'
        assert reopened.status(old_done['job_id'])['state'] == 'succeeded'
        assert reopened.active is None and reopened.thread is None
        assert reopened.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 2
    finally:
        reopened.close()


def test_worker_gate_denies_direct_jobs_without_disabling_retained_reads(tmp_path):
    from hub_staging_service.core import Service, Rejected
    from tests.test_staging_service_core import Backend, job, profile
    service = Service(tmp_path/'worker', {'demo':profile()}, Backend())
    try:
        with pytest.raises(Rejected, match='profile_protocol_unavailable'):
            service._submit_v2(job())
        assert service.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    finally:
        service.close()

def test_worker_only_artifact_import_and_golden_vector(tmp_path):
    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    import hub_staging_service
    expected, effective = values()
    source = Path(hub_staging_service.__file__).parent
    package = tmp_path / 'hub_staging_service'
    package.mkdir()
    for name in ('__init__.py', 'profile_binding.py', 'modular_staging_profile.py'):
        shutil.copyfile(source / name, package / name)
    script = '''import json, sys
sys.path.insert(0, sys.argv[1])
from hub_staging_service.profile_binding import checked_recipe_snapshot
from hub_staging_service.modular_staging_profile import from_worker_recipe, profile_digest
expected, recipe = json.loads(sys.argv[2])
result = checked_recipe_snapshot("3d-viewer", "static", expected, recipe)
assert not any(x == "engineering_hub" or x.startswith("engineering_hub.") for x in sys.modules)
print(profile_digest(from_worker_recipe(result)))
'''
    proc = subprocess.run([sys.executable, '-I', '-c', script, str(tmp_path), json.dumps([expected, effective])],
                          capture_output=True, text=True, check=True, timeout=10)
    assert proc.stdout.strip() == expected['digest']


def test_old_source_protocol_denied_before_deposit(tmp_path, monkeypatch):
    from hub_staging_service import core
    from hub_staging_service.core import Service, Rejected
    from tests.test_staging_service_core import Backend, job, profile
    monkeypatch.setattr(core, '_PROFILE_PROTOCOL_READY', True)
    service = Service(tmp_path/'worker', {'demo':profile()}, Backend())
    try:
        old = job()
        with pytest.raises(Rejected, match='source_schema'):
            service._put_source_v2({'source_id':old['job_id'], 'project':'demo',
                                'source_digest':old['source_digest'], 'source_zip_base64':old['source_zip_base64']})
        assert service.db.execute('SELECT count(*) FROM sources').fetchone()[0] == 0
    finally:
        service.close()
