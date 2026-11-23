"""Print advisory branch, pull request and worktree hygiene findings.

Read-only: it runs only git and gh listing commands and never fetches, prunes,
deletes, closes or merges. Fetch first if remote refs may be out of date.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from engineering_hub.code_ledger import (  # noqa: E402
    Branch, PullRequest, STALE_DAYS, Worktree, sweep,
)

PR_LIMIT = 1000


def run(*argv, cwd):
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True,
                          encoding='utf-8', timeout=120).stdout


def changed_paths(repo, base, head):
    """Missing history is unknown, not evidence of an empty diff."""
    if not re.fullmatch(r'[0-9a-f]{40}', head):
        return None
    try:
        names = run('git', 'diff', '--name-only', '--no-renames', '-z',
                    f'origin/{base}...{head}', '--', cwd=repo)
    except subprocess.CalledProcessError:
        return None
    return frozenset(names.split('\0')) - {''}


def read_refs(repo, base, namespace, strip):
    fields = f'%(refname:lstrip={strip})%09%(objectname)%09%(committerdate:iso-strict)'
    rows = run('git', 'for-each-ref', namespace, f'--format={fields}', cwd=repo)
    merged = set(run('git', 'for-each-ref', namespace, f'--merged=origin/{base}',
                     f'--format=%(refname:lstrip={strip})', cwd=repo).split())
    base_head = run('git', 'rev-parse', f'origin/{base}', cwd=repo).strip()
    branches = {}
    for row in rows.splitlines():
        name, head, committed = row.split('\t')
        if name not in {'HEAD', base}:
            landed = name in merged and head != base_head  # A brand-new branch has landed nothing.
            branches[name] = Branch(name, head, datetime.fromisoformat(committed), landed)
    return branches


def read_branches(repo, base):
    """Local refs win: unpushed commits on a branch whose remote head merged have not landed."""
    remote = read_refs(repo, base, 'refs/remotes/origin', 3)
    return list({**remote, **read_refs(repo, base, 'refs/heads', 2)}.values())


def read_prs(repo, base):
    rows = json.loads(run('gh', 'pr', 'list', '--state', 'all', '--limit', str(PR_LIMIT),
                          '--json', 'number,headRefName,headRefOid,state,baseRefName', cwd=repo))
    if len(rows) >= PR_LIMIT:
        raise SystemExit('Pull request list hit its limit; findings would be incomplete')
    return [PullRequest(row['number'], row['headRefName'], row['headRefOid'], row['state'],
                        paths=changed_paths(repo, base, row['headRefOid'])
                        if row['state'] == 'OPEN' and row['baseRefName'] == base else None,
                        comparable=row['baseRefName'] == base)
            for row in rows]


def read_worktrees(repo):
    trees = []
    for block in run('git', 'worktree', 'list', '--porcelain', cwd=repo).strip().split('\n\n'):
        fields = dict((line.split(' ', 1) + [''])[:2] for line in block.splitlines())
        branch = fields.get('branch', '').removeprefix('refs/heads/') or None
        trees.append(Worktree(fields['worktree'], branch, 'prunable' in fields))
    return trees


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--repo', default='.')
    parser.add_argument('--base', default='main')
    parser.add_argument('--stale-days', type=int, default=STALE_DAYS)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if run('git', 'rev-parse', '--is-shallow-repository', cwd=args.repo).strip() == 'true':
        # Reachability and merge bases stop at the graft, so only PR-head matches are reliable.
        print('warning: shallow clone; merged and overlap findings may be incomplete', file=sys.stderr)
    prs = read_prs(args.repo, args.base)
    findings = sweep(now=datetime.now(timezone.utc), branches=read_branches(args.repo, args.base),
                     prs=prs, worktrees=read_worktrees(args.repo), stale_days=args.stale_days)
    if args.json:
        print(json.dumps([asdict(item) for item in findings], indent=2))
        return 0
    for item in findings:
        print(f'{item.kind}\t{item.owner or "-"}\t{item.subject}\t{item.detail}')
    print(f'{len(findings)} findings (advisory; nothing was changed)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
