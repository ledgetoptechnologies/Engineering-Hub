"""Candidate evidence for the Hub control image.

``write`` runs at the end of a successful main publish job in hub-control.yml
and records which commit, digest and run produced the tested candidate.
``verify`` runs in hub-promote.yml before `latest` is moved and refuses any
evidence that does not describe exactly the requested main candidate.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PACKAGE = 'ghcr.io/ledgetoptechnologies/engineering-hub-control'
WORKFLOW = '.github/workflows/hub-control.yml'
SMOKE_EVIDENCE = tuple(
    f'hub-{stage}-{name}.json'
    for stage in ('local', 'registry')
    for name in ('startup', 'split-startup', 'unified-startup', 'settings-startup', 'protected-startup')
)
COMMIT_RE = re.compile(r'[0-9a-f]{40}')
DIGEST_RE = re.compile(r'sha256:[0-9a-f]{64}')


class EvidenceError(Exception):
    pass


def _require(env: dict, name: str) -> str:
    value = env.get(name, '')
    if not value:
        raise EvidenceError(f'missing {name}')
    return value


def build_record(env: dict, directory: Path) -> dict:
    commit = _require(env, 'GITHUB_SHA')
    digest = _require(env, 'IMAGE_DIGEST')
    repository = _require(env, 'GITHUB_REPOSITORY')
    if not COMMIT_RE.fullmatch(commit):
        raise EvidenceError('invalid commit')
    if not DIGEST_RE.fullmatch(digest):
        raise EvidenceError('invalid digest')
    if _require(env, 'PACKAGE') != PACKAGE:
        raise EvidenceError('unexpected package')
    if _require(env, 'GITHUB_REF') != 'refs/heads/main':
        raise EvidenceError('candidate evidence is written only on main')
    for name in SMOKE_EVIDENCE:
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise EvidenceError(f'missing smoke evidence {name}')
        json.loads(path.read_text(encoding='utf-8'))
    return {
        'evidenceSchemaVersion': 1,
        'repository': repository,
        'commit': commit,
        'package': PACKAGE,
        'digest': digest,
        'shaTag': f'{PACKAGE}:sha-{commit}',
        'run': {
            'id': _require(env, 'GITHUB_RUN_ID'),
            'attempt': _require(env, 'GITHUB_RUN_ATTEMPT'),
            'event': _require(env, 'GITHUB_EVENT_NAME'),
            'ref': 'refs/heads/main',
            'workflowRef': _require(env, 'GITHUB_WORKFLOW_REF'),
        },
        'smokeEvidence': list(SMOKE_EVIDENCE),
        'latest': 'unchanged',
    }


def verify_record(record: object, expected: dict, directory: Path | None = None) -> dict:
    def fail(message: str):
        raise EvidenceError(f'promotion refused: {message}')

    commit, digest = expected.get('commit', ''), expected.get('digest', '')
    repository, run_id = expected.get('repository', ''), expected.get('run_id', '')
    if not COMMIT_RE.fullmatch(commit):
        fail('commit must be a full lowercase 40-character SHA')
    if not DIGEST_RE.fullmatch(digest):
        fail('digest must be sha256:<64 hex>')
    if not re.fullmatch(r'\d+', run_id):
        fail('run id must be numeric')
    if not isinstance(record, dict):
        fail('evidence must be a JSON object')
    if record.get('evidenceSchemaVersion') != 1:
        fail('unsupported evidence schema')
    if record.get('repository') != repository:
        fail('evidence repository mismatch')
    if record.get('commit') != commit:
        fail('evidence commit mismatch')
    if record.get('package') != PACKAGE:
        fail('evidence package mismatch')
    if record.get('digest') != digest:
        fail('evidence digest mismatch')
    if record.get('shaTag') != f'{PACKAGE}:sha-{commit}':
        fail('evidence sha tag mismatch')
    run = record.get('run')
    if not isinstance(run, dict):
        fail('evidence run missing')
    if run.get('id') != run_id:
        fail('evidence run mismatch')
    if run.get('ref') != 'refs/heads/main':
        fail('candidate was not built from main')
    if run.get('event') not in ('push', 'workflow_dispatch'):
        fail('unexpected candidate event')
    if run.get('workflowRef') != f'{repository}/{WORKFLOW}@refs/heads/main':
        fail('candidate was not built by hub-control.yml on main')
    if record.get('smokeEvidence') != list(SMOKE_EVIDENCE):
        fail('smoke evidence list mismatch')
    if record.get('latest') != 'unchanged':
        fail('unexpected latest state')
    if directory is not None:
        for name in SMOKE_EVIDENCE:
            path = directory / name
            if not path.is_file() or path.stat().st_size == 0:
                fail(f'missing smoke evidence {name}')
    return {'commit': commit, 'digest': digest}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ('write', 'verify'):
        print('usage: hub_candidate_evidence.py write <out.json> | verify <evidence.json>', file=sys.stderr)
        return 2
    path = Path(argv[2])
    try:
        if argv[1] == 'write':
            record = build_record(dict(os.environ), Path.cwd())
            with path.open('x', encoding='utf-8') as handle:
                json.dump(record, handle, indent=2)
                handle.write('\n')
        else:
            env = os.environ
            verify_record(json.loads(path.read_text(encoding='utf-8')), {
                'commit': env.get('PROMOTE_COMMIT', ''),
                'digest': env.get('PROMOTE_DIGEST', ''),
                'repository': env.get('GITHUB_REPOSITORY', ''),
                'run_id': env.get('CANDIDATE_RUN_ID', ''),
            }, path.parent)
            print('candidate evidence verified')
    except (EvidenceError, ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
