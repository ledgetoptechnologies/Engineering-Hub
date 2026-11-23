from datetime import datetime, timedelta, timezone

from engineering_hub.code_ledger import Branch, PullRequest, Worktree, owner_hint, sweep


NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
FRESH = NOW - timedelta(days=1)
OLD = NOW - timedelta(days=8)


def run(branches=(), prs=(), worktrees=()):
    return sweep(now=NOW, branches=list(branches), prs=list(prs), worktrees=list(worktrees))


def kinds(findings):
    return [item.kind for item in findings]


def test_main_is_never_reported():
    assert run([Branch('main', 'a', OLD, merged=True)]) == []


def test_reachable_branch_is_merged_not_deleted():
    [item] = run([Branch('claude/x', 'a', FRESH, merged=True)])
    assert (item.kind, item.subject, item.owner) == ('merged_not_deleted', 'claude/x', 'claude')


def test_squash_merge_needs_the_exact_head():
    assert kinds(run([Branch('codex/x', 'a', FRESH)], [PullRequest(1, 'codex/x', 'a', 'MERGED')])) == ['merged_not_deleted']


def test_commits_after_a_merged_pr_are_not_reported_as_landed():
    # New work on a reused branch must not be offered for deletion.
    assert run([Branch('codex/x', 'b', FRESH)], [PullRequest(1, 'codex/x', 'a', 'MERGED')]) == []


def test_open_pr_is_never_cleanup():
    assert kinds(run([Branch('codex/x', 'a', OLD)],
                     [PullRequest(1, 'codex/x', 'a', 'OPEN')])) == ['comparison_unavailable']


def test_open_pr_wins_over_an_older_merged_pr():
    prs = [PullRequest(1, 'codex/x', 'a', 'MERGED'), PullRequest(2, 'codex/x', 'a', 'OPEN')]
    assert kinds(run([Branch('codex/x', 'a', FRESH)], prs)) == ['comparison_unavailable']


def test_closed_unmerged_pr_branch():
    [item] = run([Branch('claude/x', 'a', FRESH)], [PullRequest(7, 'claude/x', 'a', 'CLOSED')])
    assert item.kind == 'closed_pr_branch' and '#7' in item.detail


def test_stale_only_after_the_window():
    assert run([Branch('claude/x', 'a', FRESH)]) == []
    [item] = run([Branch('claude/x', 'a', OLD)])
    assert item.kind == 'no_pr_stale' and '8 days' in item.detail


def test_worktree_on_a_landed_branch():
    branch = Branch('claude/x', 'a', FRESH, merged=True)
    found = run([branch], worktrees=[Worktree('C:/w', 'claude/x')])
    assert kinds(found) == ['merged_not_deleted', 'worktree_merged']


def test_active_and_detached_worktrees_are_quiet():
    assert run([Branch('claude/x', 'a', FRESH)],
               worktrees=[Worktree('C:/w', 'claude/x'), Worktree('C:/d', None)]) == []


def test_missing_worktree():
    assert kinds(run(worktrees=[Worktree('C:/gone', 'claude/x', prunable=True)])) == ['worktree_missing']


def test_overlap_between_owners_only():
    prs = [PullRequest(0, 'claude/a', 'a', 'OPEN', frozenset({'x.py', 'y.py'})),
           PullRequest(1, 'codex/b', 'b', 'OPEN', frozenset({'y.py'})),
           PullRequest(2, 'claude/c', 'c', 'OPEN', frozenset({'x.py'}))]
    found = run(prs=prs)
    assert [item.subject for item in found] == ['#0 claude/a + #1 codex/b']
    assert 'y.py' in found[0].detail


def test_landed_branches_do_not_cause_overlap():
    left = Branch('claude/a', 'a', FRESH, merged=True)
    right = Branch('codex/b', 'b', FRESH)
    assert kinds(run([left, right], [PullRequest(1, 'codex/b', 'b', 'OPEN', frozenset({'x.py'}))])) == ['merged_not_deleted']


def test_stacked_prs_do_not_compare_against_a_different_target_base():
    prs = [PullRequest(1, 'claude/a', 'a', 'OPEN', frozenset({'x.py'})),
           PullRequest(2, 'codex/b', 'b', 'OPEN', comparable=False)]
    assert run(prs=prs) == []


def test_owner_is_only_a_prefix_hint():
    assert owner_hint('claude/x') == 'claude'
    assert owner_hint('feature/x') is None
    assert owner_hint('claude') is None


def test_pr_heads_not_stale_or_unpushed_local_refs_drive_overlap():
    branches = [Branch('claude/a', 'old', FRESH), Branch('codex/b', 'unpushed', FRESH)]
    prs = [PullRequest(1, 'claude/a', 'head-a', 'OPEN', paths=frozenset({'shared.py'})),
           PullRequest(2, 'codex/b', 'head-b', 'OPEN', paths=frozenset({'shared.py'})),
           PullRequest(3, 'claude/a', 'fork-head', 'OPEN', paths=frozenset({'fork.py'})),
           PullRequest(4, 'hermes/unknown', 'missing', 'OPEN')]
    overlap = [item for item in run(branches, prs) if item.kind == 'overlap']
    assert len(overlap) == 1 and '#1' in overlap[0].subject and '#2' in overlap[0].subject
    assert 'shared.py' in overlap[0].detail
    assert 'comparison_unavailable' in kinds(run(branches, prs))


def test_findings_are_ordered_by_kind_then_subject():
    found = run([Branch('claude/b', 'b', FRESH, merged=True), Branch('claude/a', 'a', FRESH, merged=True),
                 Branch('claude/c', 'c', OLD)])
    assert [item.subject for item in found] == ['claude/a', 'claude/b', 'claude/c']
