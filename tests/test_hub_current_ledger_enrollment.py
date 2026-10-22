"""Current composed ordinary app -> cold copy -> initial enrollment only.

No services, live state, protected restore/rebinding, NAS or workflow execution.
"""
from contextlib import closing
from io import BytesIO
import shutil
import sqlite3
import sys
import uuid

from PIL import Image
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.attachments import EntityImageRepository
from engineering_hub.label_sets import LabelSetRepository
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.repository import HubRepository
from engineering_hub import pilot_enrollment as enrollment
from tests.test_hub_pilot_enrollment import ledger, snapshot


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux initial offline enrollment')


def compose(path, repo):
    settings = Settings(path, AuthSettings('synthetic-h', 'synthetic-c', claude_token='synthetic-a'),
                        AuthConfig('https://fixture.invalid', beau_github_id=42))
    # Construct the real app and its repositories, but never start its lifespan
    # or listener. Independent legacy writers are not excluded by this fixture.
    return create_app(settings, repository=repo)


def test_current_composed_app_cold_copy_initial_enrollment_preserves_native_state(ledger):
    source, repo, *_ = ledger
    app = compose(source, repo)
    projects = app.state.project_registry
    project = projects.create_project('human:42', 'client-notes', 'Client notes', 'project-request')
    links = [{'label': 'Reference', 'url': 'https://example.invalid/reference'}]
    task = repo.create_task('human:42', project['id'], 'Keep context', 'context-request', details='Full context', links=links)
    question = repo.create_decision('claude', project['id'], 'Explain?', ['Yes', 'No'],
                                     details='Reasoning', links=links, allow_text=True)
    output = BytesIO()
    with Image.new('RGB', (4, 4), 'green') as image:
        image.save(output, format='PNG')
    images = EntityImageRepository(repo)
    image_id = str(uuid.uuid4())
    attached = images.put(actor='codex', image_id=image_id, kind='decision', entity_id=question['id'],
                          expected_revision=question['revision'], content=output.getvalue(),
                          media_type='image/png', title='Synthetic reference')
    answered = repo.answer_decision('human:42', question['id'], attached['entity_revision'], 'Full explanation')
    expected_image = images.image(image_id=image_id, actor='human:42')
    labels = LabelSetRepository(repo)
    labels.create(actor='claude', set_id='fixture', project=project['id'], title='Review',
                   choices=[{'key': 'yes', 'label': 'Yes'}, {'key': 'no', 'label': 'No'}],
                   items=[{'id': 'item-1', 'meta': {'fixture': 'synthetic'}}])
    labels.upload_image('fixture', 'item-1', content=output.getvalue(), content_type='image/png', actor='codex')
    labels.label('fixture', 'item-1', choice='yes', expected_revision=0, actor='human:42')
    expected_labels, expected_label_image = labels.results('fixture'), labels.image('fixture', 'item-1')
    archived = projects.archive_project('human:42', project['id'], project['revision'])
    # All operations above use scoped connections. No active app/queue exists.
    repo.close()
    assert not any(source.with_name(source.name + suffix).exists() for suffix in ('-wal', '-shm', '-journal'))
    original = source.read_bytes()
    before = snapshot(source)
    with closing(sqlite3.connect(source)) as db:
        assert set(enrollment._INDEX_DDL) <= {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    private = source.parent.parent / 'current-copy-private'
    private.mkdir(mode=0o700)
    copied = private / 'hub.sqlite3'
    shutil.copyfile(source, copied)
    copied.chmod(0o600)
    plan = enrollment.inspect_plan(copied)
    assert plan['snapshot'] == before and source.read_bytes() == original
    result = enrollment.apply_plan(plan, enrollment.plan_digest(plan), writers_stopped=True, backup_verified=True)
    with PilotOwner(copied, require_existing=True, allow_ephemeral_test_storage=True) as owner:
        assert owner.previous_epoch == result['epoch']
        owned = HubRepository(copied, pilot_owner=owner)
        current_app = compose(copied, owned)
        assert current_app.state.project_registry.get_project(project['id']) == archived
        assert owned.get_task(task['id']) == task
        assert answered in owned.list_decisions()
        assert EntityImageRepository(owned).image(image_id=image_id, actor='human:42') == expected_image
        assert LabelSetRepository(owned).results('fixture') == expected_labels
        assert LabelSetRepository(owned).image('fixture', 'item-1') == expected_label_image
        with closing(sqlite3.connect(copied)) as db:
            assert enrollment._snapshot(db, enrolled=True) == before
    assert source.read_bytes() == original and snapshot(source) == before
    # Existing ownership is not converted into permission to enroll again.
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(copied)


@pytest.mark.parametrize('hazard', [
    'partial-project', 'partial-label', 'project-shape', 'image-shape',
    'unknown-index', 'index-wrong-columns', 'missing-native-index', 'native-trigger',
])
def test_current_optional_schema_admission_remains_exact_and_read_only(ledger, hazard):
    path, repo, *_ = ledger
    compose(path, repo)
    with closing(sqlite3.connect(path)) as db, db:
        if hazard == 'partial-project':
            db.execute('DROP TABLE hub_project_requests')
        elif hazard == 'partial-label':
            db.execute('DROP TABLE hub_label_items')
        elif hazard == 'project-shape':
            db.execute('ALTER TABLE hub_projects ADD COLUMN extra TEXT')
        elif hazard == 'image-shape':
            db.execute('ALTER TABLE hub_entity_images ADD COLUMN extra TEXT')
        elif hazard == 'unknown-index':
            db.execute('CREATE INDEX extra_index ON hub_projects(data)')
        elif hazard == 'index-wrong-columns':
            db.execute('DROP INDEX hub_label_sets_project')
            db.execute('CREATE INDEX hub_label_sets_project ON hub_label_sets(id)')
        elif hazard == 'missing-native-index':
            db.execute('DROP INDEX hub_entity_images_entity')
        else:
            db.execute('CREATE TRIGGER extra AFTER INSERT ON hub_projects BEGIN SELECT 1; END')
    before = path.read_bytes()
    listing = sorted(p.name for p in path.parent.iterdir())
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.inspect_plan(path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == listing
