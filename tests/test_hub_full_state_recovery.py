"""Ordinary UNENROLLED full-state recovery using real repositories and backups.

Disposable local fixtures only: no server, network, live credentials or executor
effects. Logical parity is not byte equality of SQLite backup files. This does
not authorize transfer/reenrollment of inode-bound protected ownership, prove an
installed image, validate Access JWTs, or establish NAS/reboot acceptance.
"""
from contextlib import closing
from io import BytesIO
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import uuid

from PIL import Image
import pytest

from agent_message_bus.database import MessageRepository
from agent_message_bus.files import FileRepository
from agent_message_bus.knowledge import KnowledgeRepository, KnowledgeProjectArchived
from engineering_hub.attachments import EntityImageRepository
from engineering_hub.auth import SessionStore
from engineering_hub.backup import backup_hub
from engineering_hub.dto import DecisionDTO, TaskDTO, RunDTO
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Real file repository requires POSIX dir_fd')
DATABASES = ('hub.sqlite3', 'agent-message-bus/messages.sqlite3')
EXECUTOR = 'executor:recovery-fixture'


def inventory(root):
    """Hash every ordinary fixture file, including immutable file revisions."""
    found = {}
    for path in sorted(root.rglob('*')):
        assert not path.is_symlink()
        if path.is_file():
            found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def logical_database(path):
    """Compare all tables/rows/schema, including blobs, without JSON projection."""
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        rows = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            escaped = name.replace('"', '""')
            rows[name] = sorted(db.execute(f'SELECT * FROM "{escaped}"').fetchall(), key=repr)
        return schema, rows


def open_state(root):
    hub = HubRepository(root / DATABASES[0], execution_principal=EXECUTOR)
    projects = ProjectRepository(hub)
    bus = MessageRepository(root / DATABASES[1])
    notes = KnowledgeRepository(root / DATABASES[1])
    notes.configure_write_admission(projects.knowledge_write_admission)
    files = FileRepository(root / DATABASES[1], root / 'agent-message-bus/files')
    return hub, projects, bus, notes, files


def seed(root):
    hub, projects, bus, notes, files = open_state(root)
    project = projects.create_project('human:123', 'client-notes', 'Client notes', 'project-request')
    legacy = hub.create_task('human:123', 'treedar', 'Legacy control', 'legacy-request')
    links = [{'label': 'Reference', 'url': 'https://example.invalid/reference'}]
    task_args = dict(actor='human:123', project=project['id'], title='New context',
                     client_request_id='context-request', details='Keep complete context', links=links)
    task = hub.create_task(**task_args)
    decision = hub.create_decision('claude', project['id'], 'Explain?', ['Now', 'Later'],
                                    details='Explain tradeoffs', links=links, allow_text=True)
    image_id = str(uuid.uuid4())
    output = BytesIO()
    with Image.new('RGB', (4, 4), 'green') as image:
        image.save(output, format='PNG')
    image_args = dict(image_id=image_id, kind='decision', entity_id=decision['id'],
                      expected_revision=decision['revision'], content=output.getvalue(),
                      media_type='image/png', title='Synthetic reference', actor='codex')
    images = EntityImageRepository(hub)
    attachment = images.put(**image_args)
    stored_image = images.image(image_id=image_id, actor='human:123')
    answered = hub.answer_decision('human:123', decision['id'], attachment['entity_revision'],
                                   'Retain the complete state')
    note = notes.put(project=project['id'], path='meeting.md', title='Meeting', content='Original note',
                     author='codex', expected_version=0)
    note = notes.put(project=project['id'], path='meeting.md', title='Meeting', content='Updated note',
                     author='claude', expected_version=note.version)
    archived = projects.archive_project('human:123', project['id'], project['revision'])
    reviewed = hub.create_task('human:123', 'engineering-hub', 'Review fixture only', 'review-request')
    reviewed = hub.claim_task('codex', reviewed['id'], reviewed['revision'])
    reviewed = hub.set_candidate('codex', reviewed['id'], reviewed['revision'], 'a' * 40)
    reviewed = hub.review_task('claude', reviewed['id'], reviewed['revision'], 'a' * 40, 'approve')
    run = hub.create_run('codex', reviewed['id'], reviewed['revision'], 'sha256:' + 'b' * 64,
                         'engineering-hub:staging', client_request_id='run-request')
    # Recorded fixture events only: no driver, executor command, or real job.
    run = hub.append_run_event(EXECUTOR, run['id'], 'running', 'fixture-command', 1, run['revision'])
    run = hub.append_run_event(EXECUTOR, run['id'], 'held', 'fixture-command', 1, run['revision'])
    message_args = dict(sender='hermes', recipient='codex', message_type='info',
                        body={'topic': 'recovery-fixture', 'text': 'Synthetic only'},
                        client_request_id='message-request')
    message = bus.insert(**message_args)
    bus.claim(message.id, 'codex')
    acknowledged = bus.acknowledge(message.id, 'codex')
    pending = bus.insert(sender='claude', recipient='codex', message_type='info', body={'pending': True})
    claimed = bus.insert(sender='hermes', recipient='claude', message_type='info', body={'claimed': True})
    claimed = bus.claim(claimed.id, 'claude')
    first_file = files.put_bytes('reports/result.txt', b'First revision\n', 'hermes', 0, 'text/plain')
    second_file = files.put_bytes('reports/result.txt', b'Second revision\n', 'claude', 1, 'text/plain')
    sqlite_named_file = files.put_bytes('reports/reference.sqlite3', b'Ordinary shared file, not a ledger',
                                        'codex', 0)
    session = SessionStore(hub.path, repository=hub, clock=lambda: 1000).create_session(123)
    # Synthetic non-secret sidecar state; actual secret provenance is out of scope.
    (root / 'settings.json').write_text(json.dumps({'schema_version': 1, 'fixture_only': True}))
    (root / 'agent-message-bus/audit.jsonl').write_text('{"fixture":"retained-audit"}\n')
    hub.close()
    return locals()


def test_complete_unenrolled_recovery_preserves_all_rows_files_and_repository_semantics(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    saved = seed(source)
    # All repositories scope connections per operation; no services/threads or
    # open fixture transactions remain. Checkpoint every ledger before freezing.
    for relative in DATABASES:
        with closing(sqlite3.connect(source / relative)) as db:
            assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        assert not any((source / (relative + suffix)).exists() for suffix in ('-wal', '-shm', '-journal'))
    expected_rows = {p: logical_database(source / p) for p in DATABASES}
    assert 'hub_pilot_owner' not in expected_rows[DATABASES[0]][1]
    frozen_inventory = inventory(source)
    restored = tmp_path / 'restored'
    # Copy non-database state exactly; actual SQLite backup primitive creates
    # both complete DB copies. No constructor creates a substitute empty ledger.
    def ignore_database(parent, names):
        return [name for name in names if (parent / name).relative_to(source).as_posix() in DATABASES]

    # shutil supplies the parent as a string, so adapt only this fixture-owned
    # path; a legitimate file-repository object named *.sqlite3 is not ignored.
    shutil.copytree(source, restored, ignore=lambda parent, names: ignore_database(Path(parent), names))
    for relative in DATABASES:
        report = backup_hub(source / relative, restored / relative)
        assert report['quick_check'] == 'ok'
        assert report['sha256'] == inventory(restored)[relative]
        assert logical_database(restored / relative) == expected_rows[relative]
    assert {p: h for p, h in inventory(restored).items() if p not in DATABASES} == {
        p: h for p, h in frozen_inventory.items() if p not in DATABASES}
    assert inventory(source) == frozen_inventory

    hub, projects, bus, notes, files = open_state(restored)
    assert {p: logical_database(restored / p) for p in DATABASES} == expected_rows
    assert projects.get_project('client-notes') == saved['archived']
    assert hub.get_task(saved['legacy']['id']) == saved['legacy']
    assert hub.get_task(saved['task']['id']) == saved['task']
    assert saved['answered'] in hub.list_decisions()
    assert hub.get_run(saved['run']['id']) == saved['run']
    TaskDTO.model_validate(saved['task'])
    DecisionDTO.model_validate(saved['answered'])
    RunDTO.model_validate(saved['run'])
    assert notes.get('client-notes', 'meeting.md') == saved['note']
    with pytest.raises(KnowledgeProjectArchived):
        notes.put(project='client-notes', path='meeting.md', title='Meeting', content='Rejected',
                  author='codex', expected_version=2)
    assert EntityImageRepository(hub).image(image_id=saved['image_id'], actor='human:123') == saved['stored_image']
    with pytest.raises(HubError, match='project_archived'):
        EntityImageRepository(hub).put(**saved['image_args'])
    assert hub.create_task('human:123', 'treedar', 'Legacy control', 'legacy-request') == saved['legacy']
    assert hub.create_run('codex', saved['reviewed']['id'], saved['reviewed']['revision'],
                          'sha256:' + 'b' * 64, 'engineering-hub:staging',
                          client_request_id='run-request') == saved['run']
    assert bus.get(saved['message'].id) == saved['acknowledged']
    assert bus.get(saved['pending'].id) == saved['pending']
    assert bus.get(saved['claimed'].id) == saved['claimed']
    assert bus.insert(**saved['message_args']) == saved['acknowledged']
    assert bus.get_send_receipt('hermes', 'message-request')['message_id'] == saved['message'].id
    assert files.read_bytes('reports/result.txt', version=1) == (saved['first_file'], b'First revision\n')
    assert files.read_bytes('reports/result.txt') == (saved['second_file'], b'Second revision\n')
    assert files.read_bytes('reports/reference.sqlite3') == (
        saved['sqlite_named_file'], b'Ordinary shared file, not a ledger')
    session = SessionStore(hub.path, repository=hub, clock=lambda: 1000)
    assert session.session_for(saved['session']['cookie_token'])['github_id'] == 123
    with pytest.raises(HubError, match='project_archived'):
        hub.create_task(**saved['task_args'])
    # Supported write remains possible on an unarchived project, then replay is
    # idempotent. The held run was never resumed or submitted to an executor.
    next_task = hub.create_task('human:123', 'treedar', 'After recovery', 'after-recovery')
    assert hub.create_task('human:123', 'treedar', 'After recovery', 'after-recovery') == next_task
    assert hub.get_run(saved['run']['id']) == saved['run']
    assert inventory(source) == frozen_inventory
