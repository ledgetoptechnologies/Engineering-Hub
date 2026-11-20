"""Advisory hygiene findings over branches, pull requests and worktrees.

Pure and read-only: facts come in, findings come out. A finding is a prompt for
the owning agent, never authority to delete, close or merge anything. Owner is a
hint taken from the branch prefix, not an authenticated identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

STALE_DAYS = 7
PROTECTED = frozenset({'main', 'master'})
OWNER_PREFIXES = ('claude', 'codex', 'hermes')
KINDS = ('merged_not_deleted', 'closed_pr_branch', 'no_pr_stale', 'worktree_merged',
         'worktree_missing', 'overlap')


@dataclass(frozen=True)
class Branch:
    name: str
    head: str
    committed_at: datetime
    merged: bool = False  # Head is reachable from the default branch.
    paths: frozenset[str] = frozenset()  # Changed since the merge base.


@dataclass(frozen=True)
class PullRequest:
    number: int
    branch: str
    head: str
    state: str  # OPEN, MERGED or CLOSED.


@dataclass(frozen=True)
class Worktree:
    path: str
    branch: str | None  # None when detached.
    prunable: bool = False


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str
    owner: str | None
    detail: str


def owner_hint(branch):
    prefix = branch.partition('/')[0]
    return prefix if '/' in branch and prefix in OWNER_PREFIXES else None


def _landed(branch, prs):
    """Squash merges leave the head off main, so a merged PR must match the exact head."""
    return branch.merged or any(pr.state == 'MERGED' and pr.head == branch.head for pr in prs)


def sweep(*, now, branches, prs, worktrees, stale_days=STALE_DAYS):
    by_branch = {}
    for pr in prs:
        by_branch.setdefault(pr.branch, []).append(pr)
    live = [branch for branch in branches if branch.name not in PROTECTED]
    landed = {branch.name for branch in live if _landed(branch, by_branch.get(branch.name, ()))}
    cutoff = now - timedelta(days=stale_days)
    findings = []
    in_review = []
    for branch in live:
        mine = by_branch.get(branch.name, ())
        owner = owner_hint(branch.name)
        if any(pr.state == 'OPEN' for pr in mine):
            in_review.append(branch)
        elif branch.name in landed:
            findings.append(Finding('merged_not_deleted', branch.name, owner,
                                    'Work is on the default branch; the branch can be cleaned up.'))
        elif any(pr.head == branch.head for pr in mine):
            numbers = ', '.join(f'#{pr.number}' for pr in mine if pr.head == branch.head)
            findings.append(Finding('closed_pr_branch', branch.name, owner,
                                    f'{numbers} closed without merging; keep with a reason or delete.'))
        elif branch.committed_at < cutoff:
            age = (now - branch.committed_at).days
            findings.append(Finding('no_pr_stale', branch.name, owner,
                                    f'No pull request and {age} days old; keep with a reason, '
                                    'open a PR or delete.'))
    for tree in worktrees:
        if tree.prunable:
            findings.append(Finding('worktree_missing', tree.path, None,
                                    'Directory is gone; `git worktree prune` would drop this record.'))
        elif tree.branch in landed:
            findings.append(Finding('worktree_merged', tree.path, owner_hint(tree.branch),
                                    f'{tree.branch} has landed; check nothing is unsaved, then remove.'))
    # Open PRs are what will actually meet at merge time, and only different
    # owners can collide unknowingly; one owner already sees both branches.
    for index, left in enumerate(in_review):
        for right in in_review[index + 1:]:
            shared = left.paths & right.paths
            if shared and owner_hint(left.name) != owner_hint(right.name):
                sample = ', '.join(sorted(shared)[:3])
                findings.append(Finding('overlap', f'{left.name} + {right.name}', None,
                                        f'{len(shared)} shared paths, including {sample}.'))
    return sorted(findings, key=lambda item: (KINDS.index(item.kind), item.subject))
