import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from engineering_hub.modular_staging_profile import canonical_profile, from_worker_recipe, profile_bytes, profile_digest
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.staging_resources import StagingResourceStore


@pytest.fixture
def profile():
    return {'schema': 'modular-staging-profile-v1', 'image_id': 'sha256:' + 'a' * 64,
            'build_argv': ['npm', 'run', 'build'], 'test_argv': ['npm', 'test'],
            'timeout_seconds': 30, 'memory_mb': 128, 'pids': 32, 'output_bytes': 4096,
            'log_bytes': 1024, 'workspace_bytes': 65536, 'cpu_millis': 250}


def test_profile_digest_is_versioned_canonical_and_worker_equivalent(profile):
    from hub_staging_service.core import recipe
    worker = dict(profile)
    worker.pop('schema'); worker.pop('cpu_millis'); worker['cpus'] = 0.25
    assert from_worker_recipe(recipe(worker)) == profile
    reordered = dict(reversed(list(profile.items())))
    raw = json.dumps(profile, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    expected = 'sha256:' + hashlib.sha256(b'engineering-hub:modular-staging-profile:v1\x00' + raw).hexdigest()
    assert expected == 'sha256:b838ef601b4af707a876738fbbfbf65d094d36a1e8e3138339fc0fb5bf9b0f31'
    assert profile_digest(reordered) == expected and profile_bytes(reordered) == raw
    worker['cpus'] = 0.2501
    with pytest.raises(ValueError): from_worker_recipe(worker)
    worker['cpus'] = 0.25; worker['unexpected'] = 'ignored by legacy worker'
    with pytest.raises(ValueError): from_worker_recipe(worker)


@pytest.mark.parametrize('key,value', [
    ('cpu_millis', True), ('cpu_millis', 1001), ('memory_mb', 31),
    ('workspace_bytes', 999999999), ('image_id', 'sha256:' + 'A' * 64),
    ('build_argv', []), ('test_argv', ['ok\x00no']), ('schema', 'v2'),
])
def test_profile_rejects_invalid_and_noncanonical_inputs(profile, key, value):
    profile[key] = value
    with pytest.raises(ValueError): canonical_profile(profile)


def test_owner_enrollment_cas_audit_scoped_reads_and_history(tmp_path, profile):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}), clock=lambda: 1800000000)
    store.set_policy('human:1', 'engineering-hub', 0, {'codex': ['read']})
    with pytest.raises(HubError, match='staging_admin_required'):
        store.enroll_profile('codex', 'engineering-hub', 'static', 0, profile)
    one = store.enroll_profile('human:1', 'engineering-hub', 'static', 0, profile)
    assert one['generation'] == 1 and one['execution_authorization'] is False
    with pytest.raises(HubError, match='revision_conflict'):
        store.enroll_profile('human:1', 'engineering-hub', 'static', 0, profile)
    changed = copy.deepcopy(profile); changed['build_argv'].append('--release')
    two = store.enroll_profile('human:1', 'engineering-hub', 'static', 1, changed)
    assert two['generation'] == 2 and two['digest'] != one['digest']
    assert store.get_profile('codex', 'engineering-hub', 'static', 1) == one
    assert store.get_profile('codex', 'engineering-hub', 'static') == two
    with pytest.raises(HubError, match='staging_forbidden'):
        store.get_profile('claude', 'engineering-hub', 'static')
    with repo._connection() as db:
        rows = db.execute("SELECT actor,action,entity_id,revision FROM hub_audit WHERE action='staging.profile_enrolled' ORDER BY revision").fetchall()
        assert [tuple(row) for row in rows] == [('human:1', 'staging.profile_enrolled', 'staging-profile:engineering-hub:static', 1),
                                                ('human:1', 'staging.profile_enrolled', 'staging-profile:engineering-hub:static', 2)]
    store.set_policy('human:1', 'engineering-hub', 1, {})
    with pytest.raises(HubError, match='staging_forbidden'):
        store.get_profile('codex', 'engineering-hub', 'static')


def test_owner_admission_recheck_rolls_back_profile_and_audit(tmp_path, profile):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}))
    def deny(*_): raise HubError('session_revoked', 403)
    with pytest.raises(HubError, match='session_revoked'):
        store.enroll_profile('human:1', 'engineering-hub', 'static', 0, profile, admission_guard=deny)
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_profile_enrollments').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM hub_audit WHERE action='staging.profile_enrolled'").fetchone()[0] == 0


def test_competing_owner_writes_have_one_cas_winner(tmp_path, profile):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    store = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}))
    def attempt():
        try:
            return store.enroll_profile('human:1', 'engineering-hub', 'static', 0, profile)['generation']
        except HubError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert sorted(results, key=str) == [1, 'revision_conflict']
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM hub_staging_profile_enrollments').fetchone()[0] == 1
