"""Current reopen parity and a frozen old-contract downgrade counterexample.

Never opens live state, invokes Git, or simulates a lossless legacy downgrade.
"""
from pathlib import Path
import hashlib
import runpy

import pytest
from pydantic import ValidationError

from engineering_hub.dto import DecisionDTO, TaskDTO
from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubRepository


FIXTURE = Path(__file__).parent / 'fixtures' / 'a333_record_contract.py'
OLD = runpy.run_path(str(FIXTURE))


def test_frozen_baseline_contract_provenance_and_semantics_are_pinned():
    source = FIXTURE.read_text(encoding='utf-8')
    assert 'a333704dcf8c14ef071d4af434928e657e5baed0' in source
    assert 'd45fce484f6a83ac6a5a545e314954b8035e44be' in source
    # read_text normalizes platform line endings. Pin the complete source,
    # not ast.dump, whose representation changes between Python versions.
    assert hashlib.sha256(source.encode('utf-8')).hexdigest() == (
        'a5aeb4c6306252182ad6071b99a1bfd42fcc0b32cbd61ea2e55bebd522b56346')


def test_current_reopen_preserves_complete_new_records_and_history(tmp_path):
    path = tmp_path / 'hub.sqlite3'
    repo = HubRepository(path)
    projects = ProjectRepository(repo)
    project = projects.create_project('human:123', 'client-notes', 'Client notes', 'project-request')
    links = [{'label': 'Reference', 'url': 'https://example.com/reference'}]
    task = repo.create_task('human:123', project['id'], 'Retain context', 'task-request',
                            details='Do not drop this context', links=links)
    question = repo.create_decision('claude', project['id'], 'Explain?', ['Now', 'Later'],
                                    details='Explain the tradeoff', links=links, allow_text=True)
    answered = repo.answer_decision('human:123', question['id'], question['revision'],
                                    'Keep the complete records')
    audit, outbox = repo.list_audit(), repo.list_outbox()

    reopened = HubRepository(path)
    assert ProjectRepository(reopened).get_project(project['id']) == project
    assert reopened.get_task(task['id']) == task
    assert reopened.list_decisions() == [answered]
    assert TaskDTO.model_validate(task).model_dump(exclude_unset=True) == task
    assert DecisionDTO.model_validate(answered).model_dump(exclude_unset=True) == answered
    assert reopened.list_audit() == audit
    assert reopened.list_outbox() == outbox
    assert answered['execution_authorization'] is False


@pytest.mark.parametrize('kind', ['task', 'decision'])
@pytest.mark.parametrize('feature', ['context', 'dynamic_project'])
def test_frozen_a333_reader_rejects_new_format_but_accepts_clean_control(tmp_path, kind, feature):
    repo = HubRepository(tmp_path / 'hub.sqlite3')
    projects = ProjectRepository(repo)
    fields = {'details': 'Preserve context', 'links': [{'label': 'Ref', 'url': 'https://example.com'}]}
    project = 'treedar'
    if feature == 'dynamic_project':
        project = projects.create_project('human:123', 'client-notes', 'Client notes', 'project')['id']
        fields = {}
    if kind == 'task':
        control = repo.create_task('human:123', 'treedar', 'Control', 'control')
        candidate = repo.create_task('human:123', project, 'New', 'new', **fields)
        current = TaskDTO
    else:
        control = repo.create_decision('claude', 'treedar', 'Control', ['Yes', 'No'])
        candidate = repo.create_decision('claude', project, 'New', ['Yes', 'No'],
                                         **fields, **({'allow_text': True} if feature == 'context' else {}))
        current = DecisionDTO
    old = OLD['TaskDTO' if kind == 'task' else 'DecisionDTO']
    assert old.model_validate(control).model_dump(exclude_unset=True) == control
    assert current.model_validate(candidate).model_dump(exclude_unset=True) == candidate
    with pytest.raises(ValidationError) as failure:
        old.model_validate(candidate)
    errors = failure.value.errors()
    if feature == 'dynamic_project':
        assert any(e['loc'] == ('project',) and e['type'] == 'literal_error' for e in errors)
    else:
        assert any(e['loc'] == ('details',) and e['type'] == 'extra_forbidden' for e in errors)
        assert any(e['loc'] == ('links',) and e['type'] == 'extra_forbidden' for e in errors)
        if kind == 'decision':
            assert any(e['loc'] == ('allow_text',) and e['type'] == 'extra_forbidden' for e in errors)
