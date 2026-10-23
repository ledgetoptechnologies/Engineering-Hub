"""Keyset paging of the shared knowledge list: path order, no gaps, no repeats."""
import pytest

from agent_message_bus.knowledge import KnowledgeError, KnowledgeRepository


@pytest.fixture
def repo(tmp_path):
    return KnowledgeRepository(tmp_path / 'bus.sqlite3')


def put(repo, path, project='engineering-hub'):
    repo.put(project=project, path=path, title=path, content='text', author='codex', expected_version=0)


def paths(entries):
    return [entry.path for entry in entries]


def walk(repo, limit, **kwargs):
    """Page through like the Notes UI: ask for limit + 1, continue after the last path shown."""
    seen, after = [], None
    while True:
        page = repo.list(project='engineering-hub', limit=limit + 1, after=after, **kwargs)
        seen += paths(page[:limit])
        if len(page) <= limit:
            return seen
        after = page[limit - 1].path


def test_fifty_one_notes_page_at_the_fifty_boundary_without_gaps_or_repeats(repo):
    expected = [f'notes/{index:03}.md' for index in range(51)]
    for path in reversed(expected):
        put(repo, path)
    first = repo.list(project='engineering-hub', limit=51)
    assert paths(first[:50]) == expected[:50] and len(first) == 51
    rest = repo.list(project='engineering-hub', limit=51, after=first[49].path)
    assert paths(rest) == expected[50:]
    assert walk(repo, 50) == expected


def test_after_the_last_path_is_an_empty_final_page(repo):
    for index in range(3):
        put(repo, f'a/{index}.md')
    assert repo.list(project='engineering-hub', after='a/2.md') == []
    assert repo.list(project='engineering-hub', after='zzz') == []


def test_paging_stays_inside_the_prefix_and_the_project(repo):
    for path in ('plans/a.md', 'plans/b.md', 'plans/c.md', 'plansx/d.md', 'runbooks/e.md'):
        put(repo, path)
    put(repo, 'plans/b2.md', project='treedar')
    assert walk(repo, 1, path_prefix='plans') == ['plans/a.md', 'plans/b.md', 'plans/c.md']
    assert paths(repo.list(project='treedar', after='plans/a.md')) == ['plans/b2.md']


def test_notes_written_between_pages_never_repeat_earlier_ones(repo):
    for path in ('b.md', 'd.md', 'f.md'):
        put(repo, path)
    first = repo.list(project='engineering-hub', limit=2)
    assert paths(first) == ['b.md', 'd.md']
    put(repo, 'a.md')          # before the cursor: not repeated or shown on later pages
    put(repo, 'e.md')          # after the cursor: picked up by the next page
    repo.put(project='engineering-hub', path='b.md', title='edited', content='new', author='codex', expected_version=1)
    assert paths(repo.list(project='engineering-hub', after='d.md')) == ['e.md', 'f.md']


@pytest.mark.parametrize('after', ['../escape', '/absolute', '', 'a\x00b'])
def test_invalid_after_is_rejected(repo, after):
    with pytest.raises(KnowledgeError):
        repo.list(project='engineering-hub', after=after)
