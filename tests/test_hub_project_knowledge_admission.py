"""Archive admission ordering across real, separate Hub and knowledge ledgers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

from agent_message_bus.knowledge import KnowledgeRepository, KnowledgeProjectArchived
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubRepository


def setup(tmp_path):
    hub = HubRepository(tmp_path / 'hub.sqlite3')
    projects = ProjectRepository(hub)
    projects.create_project('human:123', 'landscript', 'LandScript', 'first')
    notes = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    return hub, projects, notes


def put(notes, project='landscript', version=0):
    return notes.put(project=project, path='meeting.md', title='Meeting',
                     content='Discussion, not execution authority',
                     author='codex', expected_version=version)


def test_archived_knowledge_reads_and_legacy_namespaces_survive(tmp_path):
    hub, projects, notes = setup(tmp_path)
    notes.configure_write_admission(projects.knowledge_write_admission)
    saved = put(notes)
    projects.archive_project('human:123', 'landscript', 1)
    with pytest.raises(KnowledgeProjectArchived, match='project_archived'):
        put(notes, version=1)
    assert notes.get('landscript', 'meeting.md') == saved
    assert notes.search(project='landscript', query='Discussion')[0] == saved
    for namespace in ('agent-bus', 'vaultbridge', 'Historical_Namespace'):
        assert put(notes, project=namespace).version == 1
    assert len(projects.list_projects(True)) == 6  # No inferred project creation.


def test_invalid_registry_record_fails_closed_before_knowledge_mutation(tmp_path):
    hub, projects, notes = setup(tmp_path)
    notes.configure_write_admission(projects.knowledge_write_admission)
    with hub._transaction() as db:
        db.execute("UPDATE hub_projects SET data=? WHERE id='landscript'",
                   ('{"archived":"false"}',))
    with pytest.raises(RuntimeError, match='Invalid project admission record'):
        put(notes)
    assert notes.list(project='landscript') == []


def test_archive_waits_for_admitted_note_then_rejects_next_write(tmp_path, monkeypatch):
    hub, projects, notes = setup(tmp_path)
    # A separately constructed registry uses a separate connection/instance.
    other = ProjectRepository(HubRepository(hub.path))
    admitted = threading.Event()
    release = threading.Event()
    archive_attempted = threading.Event()
    archive_finished = threading.Event()
    transaction = other._transaction

    @contextmanager
    def admission(project):
        with projects.knowledge_write_admission(project):
            admitted.set()
            assert release.wait(3)
            yield

    @contextmanager
    def archive_transaction():
        archive_attempted.set()
        with transaction() as db:
            yield db

    def archive():
        try:
            return other.archive_project('human:123', 'landscript', 1)
        finally:
            archive_finished.set()

    monkeypatch.setattr(other, '_transaction', archive_transaction)
    notes.configure_write_admission(admission)
    with ThreadPoolExecutor(max_workers=2) as workers:
        write = workers.submit(put, notes)
        try:
            assert admitted.wait(3)
            archived = workers.submit(archive)
            assert archive_attempted.wait(3)
            assert not archive_finished.wait(0.1)
        finally:
            release.set()
        saved = write.result(timeout=3)
        assert archived.result(timeout=3)['archived'] is True
    assert notes.get('landscript', 'meeting.md') == saved
    with pytest.raises(KnowledgeProjectArchived):
        put(notes, version=1)
