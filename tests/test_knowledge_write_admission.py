"""Trusted application write admission, not arbitrary SQLite writer exclusion."""
from contextlib import closing, contextmanager
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest

from agent_message_bus.knowledge import (
    KnowledgeConflict, KnowledgeError, KnowledgeNotFound, KnowledgeProjectArchived,
    KnowledgeRepository,
)


def put(repo, path='notes.md', expected_version=0, project='agent-bus'):
    return repo.put(project=project, path=path, title='Notes', content='Durable notes',
                    author='codex', expected_version=expected_version)


def test_standalone_put_cas_and_reopen_are_unchanged(tmp_path):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    item = put(repo)
    with pytest.raises(KnowledgeConflict):
        put(repo)
    assert KnowledgeRepository(repo.db_path).get('agent-bus', 'notes.md') == item
    assert put(repo, expected_version=1).version == 2


def test_one_time_configuration_and_late_binding_refused(tmp_path):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    projects = []
    @contextmanager
    def guard(project):
        projects.append(project)
        yield
    with pytest.raises(ValueError):
        repo.configure_write_admission(None)
    repo.configure_write_admission(guard)
    with pytest.raises(ValueError, match='already fixed'):
        repo.configure_write_admission(guard)
    put(repo)
    assert projects == ['agent-bus']
    with pytest.raises(ValueError, match='already fixed'):
        repo.configure_write_admission(guard)
    unguarded = KnowledgeRepository(tmp_path / 'other.sqlite3')
    put(unguarded)
    with pytest.raises(ValueError, match='already fixed'):
        unguarded.configure_write_admission(guard)


def test_even_invalid_first_put_freezes_configuration(tmp_path):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    with pytest.raises(KnowledgeError):
        put(repo, project='../bad')
    with pytest.raises(ValueError, match='already fixed'):
        repo.configure_write_admission(lambda _: None)


def test_concurrent_configuration_has_exactly_one_winner(tmp_path):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    barrier = threading.Barrier(3)
    results = []
    @contextmanager
    def guard(project):
        yield
    def configure():
        barrier.wait(timeout=3)
        try:
            repo.configure_write_admission(guard)
            results.append('bound')
        except ValueError:
            results.append('refused')
    threads = [threading.Thread(target=configure, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=3)
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert sorted(results) == ['bound', 'refused']


def test_guard_entry_failure_never_mutates_or_acquires_bus_writer(tmp_path, monkeypatch):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    @contextmanager
    def guard(project):
        assert project == 'landscript'
        raise KnowledgeProjectArchived()
        yield  # pragma: no cover - defines a context manager whose entry fails
    repo.configure_write_admission(guard)
    def unexpected_connection():
        pytest.fail('Admission failure reached the bus connection')
    with monkeypatch.context() as patch:
        patch.setattr(repo, '_connection', unexpected_connection)
        with pytest.raises(KnowledgeProjectArchived, match='^project_archived$'):
            put(repo, project='landscript')
    with pytest.raises(KnowledgeNotFound):
        repo.get('landscript', 'notes.md')


def test_guard_covers_commit_and_post_commit_readback(tmp_path, monkeypatch):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    events = []
    @contextmanager
    def guard(project):
        events.append('entered')
        try:
            yield
        finally:
            events.append('exited')
    repo.configure_write_admission(guard)
    original_get = repo.get
    def checked_get(project, path):
        assert events == ['entered']
        # A separate handle sees the committed row before the guard releases.
        with closing(sqlite3.connect(repo.db_path)) as connection:
            assert connection.execute('SELECT version FROM knowledge').fetchone()[0] == 1
        events.append('committed-readback')
        return original_get(project, path)
    monkeypatch.setattr(repo, 'get', checked_get)
    assert put(repo).version == 1
    assert events == ['entered', 'committed-readback', 'exited']


def test_guard_releases_on_cas_conflict_and_does_not_overwrite(tmp_path):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    active = []
    @contextmanager
    def guard(project):
        active.append(project)
        try:
            yield
        finally:
            active.pop()
    repo.configure_write_admission(guard)
    original = put(repo)
    with pytest.raises(KnowledgeConflict):
        put(repo)
    assert active == [] and repo.get('agent-bus', 'notes.md') == original


def test_admission_is_before_instance_lock_avoiding_inverse_lock_deadlock(tmp_path):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    outer = threading.RLock()
    second_waiting = threading.Event()
    errors = []
    @contextmanager
    def guard(project):
        if threading.current_thread().name == 'second-writer':
            second_waiting.set()
        with outer:
            yield
    repo.configure_write_admission(guard)
    def second():
        try:
            put(repo, path='second.md')
        except BaseException as error:
            errors.append(error)
    second_thread = threading.Thread(target=second, name='second-writer', daemon=True)
    def first():
        try:
            with outer:
                second_thread.start()
                assert second_waiting.wait(timeout=3)
                put(repo, path='first.md')
        except BaseException as error:
            errors.append(error)
    first_thread = threading.Thread(target=first, daemon=True)
    first_thread.start()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    assert not first_thread.is_alive() and not second_thread.is_alive(), 'Inverted admission/instance lock order'
    assert errors == []
    assert {item.path for item in repo.list(project='agent-bus')} == {'first.md', 'second.md'}


def test_first_put_freezes_configuration_while_guard_entry_waits(tmp_path):
    repo = KnowledgeRepository(tmp_path / 'bus.sqlite3')
    entered, release = threading.Event(), threading.Event()
    errors = []
    @contextmanager
    def guard(project):
        entered.set()
        assert release.wait(timeout=3)
        yield
    repo.configure_write_admission(guard)
    def writer():
        try:
            put(repo)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        assert entered.wait(timeout=3)
        with pytest.raises(ValueError, match='already fixed'):
            repo.configure_write_admission(guard)
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and errors == []


@pytest.mark.skipif(sys.platform == 'win32', reason='Real bus MCP imports Linux fcntl')
def test_bus_composition_reuses_exact_repository_and_maps_archived_error(tmp_path):
    from starlette.requests import Request
    from mcp.server.fastmcp.exceptions import ToolError
    from agent_message_bus.api import Service
    from agent_message_bus.audit import AuditLog
    from agent_message_bus.auth import AuthSettings
    from agent_message_bus.database import MessageRepository
    from agent_message_bus.mcp_server import build_mcp
    from agent_message_bus.retention import RetentionPolicy
    service = Service(MessageRepository(tmp_path / 'bus.sqlite3'), AuditLog(tmp_path / 'audit.jsonl'),
                      AuthSettings('h', 'c'), RetentionPolicy())
    mcp = build_mcp(service)
    repository = service.knowledge
    assert type(repository) is KnowledgeRepository
    build_mcp(service)
    assert service.knowledge is repository
    @contextmanager
    def denied(project):
        raise KnowledgeProjectArchived()
        yield  # pragma: no cover
    repository.configure_write_admission(denied)
    request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer c')], 'state': {}})
    context = SimpleNamespace(request_context=SimpleNamespace(request=request))
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'knowledge_put')
    assert 'context_factory' not in tool.parameters['properties']
    with pytest.raises(ToolError, match='^project_archived$'):
        tool.fn(project='landscript', path='notes.md', title='PRIVATE TITLE', content='PRIVATE CONTENT',
                expected_version=0, ctx=context)
    with pytest.raises(KnowledgeNotFound):
        repository.get('landscript', 'notes.md')
    service.knowledge = KnowledgeRepository(tmp_path / 'wrong.sqlite3')
    with pytest.raises(ValueError, match='identity must be preserved'):
        build_mcp(service)
    assert service.knowledge.db_path == tmp_path / 'wrong.sqlite3'
