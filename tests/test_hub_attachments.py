from io import BytesIO
import uuid

import pytest
from PIL import Image, PngImagePlugin

from engineering_hub.attachments import EntityImageRepository
from engineering_hub import attachments
from engineering_hub.repository import HubError, HubRepository


def png():
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('PRIVATE_MARKER', 'must not survive')
    with Image.new('RGB', (3, 3), 'green') as image:
        image.save(output, format='PNG', pnginfo=metadata)
    return output.getvalue() + b'TRAILING_PRIVATE_PAYLOAD'


@pytest.fixture
def fixture(tmp_path):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    images = EntityImageRepository(repo)
    decision = repo.create_decision('claude', 'treedar', 'Identify this', ['Yes', 'No'])
    return repo, images, decision


def add(images, item, **kwargs):
    values = dict(image_id=str(uuid.uuid4()), kind='decision', entity_id=item['id'],
                  expected_revision=item['revision'], content=png(), media_type='image/png',
                  title='Reference', actor='codex')
    values.update(kwargs)
    return images.put(**values)


def test_sanitized_image_revision_audit_and_replay(fixture):
    repo, images, decision = fixture
    image_id = str(uuid.uuid4())
    first = add(images, decision, image_id=image_id)
    assert first['project'] == 'treedar'
    assert first['entity_revision'] == decision['revision'] + 1
    assert first['url'] == f'/api/hub/v1/attachments/{image_id}/image'
    data, media_type = images.image(image_id=image_id, actor='human:42')
    assert media_type == 'image/png'
    assert b'PRIVATE' not in data
    with Image.open(BytesIO(data)) as clean:
        assert not clean.info
    assert add(images, decision, image_id=image_id) == first
    audit = [item for item in repo.list_audit() if item['action'] == 'decision.attachment_added']
    assert len(audit) == 1 and audit[0]['actor'] == 'codex'
    assert len(images.list(kind='decision', entity_id=decision['id'], actor='claude')['items']) == 1
    repo.answer_decision('human:42', decision['id'], first['entity_revision'], 'Yes')
    assert add(images, decision, image_id=image_id) == first
    with pytest.raises(HubError, match='decision_closed'):
        add(images, decision, expected_revision=first['entity_revision'] + 1)


def test_cross_actor_or_target_id_reuse_and_revision_conflicts(fixture):
    repo, images, decision = fixture
    image_id = str(uuid.uuid4())
    add(images, decision, image_id=image_id)
    other = repo.create_decision('claude', 'engineering-hub', 'Other', ['A', 'B'])
    for item, kwargs in [(decision, {'actor': 'claude'}), (other, {}), (decision, {'title': 'Changed'})]:
        with pytest.raises(HubError, match='attachment_id_conflict'):
            add(images, item, image_id=image_id, **kwargs)
    with pytest.raises(HubError, match='revision_conflict'):
        add(images, decision)
    assert images.list(kind='decision', entity_id=other['id'], actor='claude')['items'] == []


@pytest.mark.parametrize('kwargs', [
    {'actor': 'executor:container-pilot'}, {'actor': ''},
    {'image_id': '../photo'}, {'kind': 'run'}, {'expected_revision': True},
    {'title': 'bad\x00title'}, {'content': b'<svg onload="x"/>'},
    {'media_type': 'image/svg+xml'}, {'media_type': 'image/jpeg'},
    {'content': b'x' * (256 * 1024 + 1)},
])
def test_invalid_uploads(fixture, kwargs):
    _, images, decision = fixture
    with pytest.raises(HubError):
        add(images, decision, **kwargs)


def test_read_actor_and_target_validation(fixture):
    _, images, decision = fixture
    first = add(images, decision)
    with pytest.raises(HubError, match='forbidden'):
        images.image(image_id=first['id'], actor='unknown')
    with pytest.raises(HubError, match='forbidden'):
        images.list(kind='decision', entity_id=decision['id'], actor='unknown')
    with pytest.raises(HubError, match='not_found'):
        add(images, decision, entity_id=str(uuid.uuid4()))
    with pytest.raises(HubError, match='not_found'):
        images.list(kind='task', entity_id=decision['id'], actor='human:42')


def test_task_owner_proposer_human_only_and_candidate_locked(fixture):
    repo, images, _ = fixture
    task = repo.propose_staging_task('claude', 'treedar', 'Review', 'proposal')
    with pytest.raises(HubError, match='forbidden'):
        add(images, task, kind='task')
    first = add(images, task, kind='task', actor='claude')
    claimed = repo.claim_task('codex', task['id'], first['entity_revision'])
    with pytest.raises(HubError, match='forbidden'):
        add(images, claimed, kind='task', actor='claude')
    second = add(images, claimed, kind='task', actor='codex')
    candidate = repo.set_candidate('codex', task['id'], second['entity_revision'], 'a' * 40)
    with pytest.raises(HubError, match='task_context_locked'):
        add(images, candidate, kind='task', actor='human:42')
    assert repo.get_task(task['id']) == candidate


def test_queued_pilot_and_active_jobs_refuse_context_mutation(fixture):
    repo, images, _ = fixture
    task = repo.create_task('human:42', 'treedar', 'Task', 'task')
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_work_jobs(target TEXT,state TEXT)')
        db.execute('INSERT INTO hub_work_jobs VALUES(?,?)', ('{"task_id":"' + task['id'] + '"}', 'held'))
    with pytest.raises(HubError, match='task_context_locked'):
        add(images, task, kind='task', actor='human:42')
    with repo._transaction() as db:
        db.execute('DELETE FROM hub_work_jobs')
        db.execute('CREATE TABLE hub_queued_pilot_policy(id INTEGER)')
    with pytest.raises(HubError, match='queued_pilot_admission_required'):
        add(images, task, kind='task', actor='human:42')


def test_entity_and_shared_storage_quota(fixture, monkeypatch):
    repo, images, decision = fixture
    current = dict(decision)
    for _ in range(8):
        saved = add(images, current)
        current['revision'] = saved['entity_revision']
    with pytest.raises(HubError, match='attachment_quota'):
        add(images, current)
    other = repo.create_decision('claude', 'engineering-hub', 'Other', ['A', 'B'])
    with repo._transaction() as db:
        db.execute('CREATE TABLE hub_label_images(data BLOB)')
        db.execute('INSERT INTO hub_label_images VALUES(?)', (b'x' * 1000,))
    monkeypatch.setattr(attachments, 'MAX_TOTAL_BYTES', 1000)
    with pytest.raises(HubError, match='attachment_quota'):
        add(images, other)
    assert images.list(kind='decision', entity_id=other['id'], actor='codex')['items'] == []


def test_atomic_rollback_on_emit_failure(fixture, monkeypatch):
    repo, images, decision = fixture
    def fail(*args):
        raise RuntimeError('test-only fault')
    monkeypatch.setattr(repo, '_emit', fail)
    with pytest.raises(RuntimeError, match='test-only fault'):
        add(images, decision)
    assert images.list(kind='decision', entity_id=decision['id'], actor='codex') == {
        'project': 'treedar', 'entity_revision': 1, 'items': []}
    assert not [row for row in repo.list_audit() if row['action'] == 'decision.attachment_added']
